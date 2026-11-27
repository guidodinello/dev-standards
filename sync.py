#!/usr/bin/env -S uv run
"""sync — bring the current dev-standards template into every repo rendered from it
(issue #53). Dry run by default: it clones, reports and pushes nothing. With
--apply it opens (or updates) one sync PR per repo with a real change; see
sync_pr.py for how.

For each repo in github-standard.json (read, never written) it makes a fresh,
shallow clone of the default branch into a temp dir. A repo without a
.copier-answers.yml there isn't onboarded and is skipped. Otherwise it runs
`copier update` (a 3-way merge from the template commit the repo was rendered
from to dev-standards' main HEAD), then resolves pin-only conflicts and reports:

    skipped     opted out in github-standard.json (`"template_sync": {"skip":
                "<reason>"}`); never cloned
    unchanged   the repo already matches the template
    clean       the update applies without conflicts ("answers only" when nothing
                but `_commit` in .copier-answers.yml changed)
    conflicts   some hunks need a human; Copier's inline markers are left in the diff
    error       the clone or the update failed (the other repos still run)

Pin conflicts (decided in docs/template-sync.md): when the repo's Dependabot and
the template both bumped the same pin, the conflict hunk is resolved to the newer
version, but only when the two sides differ in nothing but pins (a pre-commit
`rev`, or a SHA-pinned `uses:` with its `# vX` comment). First, when the template
changed nothing but pins in a conflicted file, the file is the repo's own with
those pins bumped (`bump_template_pins`). Anything else, or a version it can't
compare, stays a conflict.

--apply skips `unchanged` and "answers only" repos (no PR for a `_commit` that
moved without changing any file), turns on auto-merge for a pin-bumps-only sync,
and opens a draft for `conflicts`. A file mode the
API commit can't carry (executable, symlink) is reported here and refused by --apply.

Usage:
    ./sync.py                       # dry run, every registered repo
    ./sync.py my-repo               # dry run, just one
    ./sync.py my-repo --apply       # open/update that repo's sync PR
    ./sync.py --config path/to/github-standard.json
    ./sync.py adopt my-repo         # onboard a repo without .copier-answers.yml
                                    # (a repo named `adopt` is synced as
                                    # ./sync.py --config <path> adopt)
                                    # (dry run; --apply opens its PR): sync_adopt.py

Requires: `uv` (run it from the dev-standards checkout, like init.py) and the
`gh` CLI, authenticated for the repos' org (clones use gh's auth, so private
repos work).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NamedTuple

try:
    import pathspec
    import yaml
    from copier import run_copy, run_update
    from packaging.version import InvalidVersion, Version
except ImportError:
    sys.exit(
        "sync.py needs Copier: run it from the dev-standards checkout as ./sync.py "
        "(its shebang is `uv run`) or `uv run ./sync.py`."
    )

import sync_pr
from sync_self_host import ACTION_USES, PRE_COMMIT_REV, apply_pins, extract_pins, sync_pins

DEFAULT_CONFIG = Path.home() / "projects" / "dev-standards" / "github-standard.json"
ANSWERS_FILE = ".copier-answers.yml"
# Shallow and blobless: enough to read the default branch's tree (is there an
# answers file?) and check it out, without downloading the repo's history.
CLONE_FLAGS = ("--depth", "1", "--filter=blob:none", "--no-checkout")

# Copier's inline conflict markers (`copier update --conflict inline`).
CONFLICT_START = "<<<<<<< before updating"
CONFLICT_MID = "======="
CONFLICT_END = ">>>>>>> after updating"

_SHA = re.compile(r"[0-9a-f]{40}")


class Status(Enum):
    SKIPPED = "skipped"
    NOT_ONBOARDED = "not onboarded"
    UNCHANGED = "unchanged"
    CLEAN = "clean"
    CONFLICTS = "conflicts"
    ERROR = "error"


class ResolvedPin(NamedTuple):
    """One pin conflict resolved to the newer version: `ours` is the repo's value,
    `theirs` the template's, `kept` the one that won."""

    file: str
    name: str
    ours: str
    theirs: str
    kept: str


@dataclass(frozen=True, slots=True)
class Result:
    repo: str
    status: Status
    diff: str = ""
    answers_only: bool = False
    # Every changed file differs only in pins (a pre-commit `rev`, a SHA-pinned
    # `uses:`), and the answers file only in `_commit`: --apply auto-merges it.
    pin_only: bool = False
    resolved_pins: tuple[ResolvedPin, ...] = ()
    conflicted_files: tuple[str, ...] = field(default_factory=tuple)
    message: str = ""
    # What --apply needs to turn the result into a PR (sync_pr.py).
    base_branch: str = ""
    base_sha: str = ""
    old_commit: str = ""  # template commit the repo was on (.copier-answers.yml)
    new_commit: str = ""  # template commit it was updated to
    template_url: str = ""  # the answers' _src_path
    changes: tuple[tuple[str, bytes | None], ...] = ()  # (path, contents | None = deleted)
    # Paths whose file mode the API commit can't carry (executable, symlink, mode
    # change): reported by the dry run, refused by --apply.
    unsupported_modes: tuple[str, ...] = ()
    # Files the template renders for this repo that the repo doesn't have. Copier's
    # update reads one as deleted on purpose and never adds it back, so it's reported.
    missing_files: tuple[str, ...] = ()

    @property
    def pins_resolved(self) -> int:
        return len(self.resolved_pins)


# ── pin-conflict resolution (pure) ──────────────────────────────────────────


def pin_version(pin: str) -> Version | None:
    """The comparable version of a pin value as sync_self_host's patterns capture
    it: a pre-commit `rev` (`v0.17.5`), or a `uses:` ref with an optional tag
    comment (`<sha> # v6.2.0`, `v7`). None when it can't be compared: a SHA with no
    tag comment, or a tag that isn't a PEP 440 version."""
    ref, hash_mark, comment = pin.partition("#")
    raw = comment.strip() if hash_mark else ref.strip()
    if not hash_mark and _SHA.fullmatch(raw):
        return None
    try:
        return Version(raw.removeprefix("v"))
    except InvalidVersion:
        return None


def merge_pin_only_hunk(
    context: str, ours: str, theirs: str
) -> tuple[str, list[tuple[str, str, str, str]]] | None:
    """Resolve one conflict hunk, or return None to leave it as a conflict.

    `ours` is the repo's side, `theirs` the template's; `context` is the line just
    above the hunk, prepended to both so a pre-commit `rev:` is seen together with
    its `repo:` line. The hunk is pin-only when putting our pin values into theirs
    gives back ours exactly. Each differing pin then takes the newer version.
    Fails closed: any version it can't compare, or equal versions with different
    SHAs, leaves the conflict. Returns the merged hunk and, per differing pin,
    (name, ours, theirs, kept)."""
    ours_text, theirs_text = context + ours, context + theirs
    if ours_text == theirs_text:
        return ours, []
    if sync_pins(ours_text, theirs_text) != ours_text:
        return None  # differs in more than pins
    merged = ours_text
    pins = []
    for pattern in (PRE_COMMIT_REV, ACTION_USES):
        our_pins, their_pins = extract_pins(ours_text, pattern), extract_pins(theirs_text, pattern)
        winners = {}
        for key, our_pin in our_pins.items():
            their_pin = their_pins.get(key)
            if their_pin is None or their_pin == our_pin:
                continue
            our_version, their_version = pin_version(our_pin), pin_version(their_pin)
            if our_version is None or their_version is None or our_version == their_version:
                return None
            winners[key] = their_pin if their_version > our_version else our_pin
            pins.append((key, our_pin, their_pin, winners[key]))
        merged = apply_pins(merged, pattern, winners)
    return merged.removeprefix(context), pins


def _all_pins(text: str, pattern: re.Pattern) -> dict[str, set[str]]:
    """Every distinct pin per name. extract_pins keeps only the last one, which hides
    a file that pins the same action at two different versions."""
    pins: dict[str, set[str]] = {}
    for m in pattern.finditer(text):
        pins.setdefault(m.group(2), set()).add(m.group(3))
    return pins


def bump_template_pins(
    repo_text: str, old_template: str, new_template: str
) -> tuple[str, list[tuple[str, str, str, str]]] | None:
    """The repo's file with the template's pin bumps applied, when the template
    changed nothing but pins between the two commits; None to keep Copier's merge.

    Copier's 3-way text merge misaligns a repo that moved or reshaped template blocks
    (a job kept elsewhere, an extra `setup-uv` step in a job of its own): a one-line
    pin bump then lands in a conflict with a whole job. When the template's change is
    pin-only, the exact merge is the repo's file plus those pins, so take that.
    Only the names the template bumped move, each to the newer version. Fails closed
    (None): a template change beyond pins, a name pinned at two versions in the
    template or the repo, or a version it can't compare. A name the repo no longer
    pins is skipped."""
    if sync_pins(old_template, new_template) != old_template:
        return None
    merged = repo_text
    pins = []
    for pattern in (PRE_COMMIT_REV, ACTION_USES):
        old_pins, new_pins = _all_pins(old_template, pattern), _all_pins(new_template, pattern)
        repo_pins = _all_pins(repo_text, pattern)
        winners = {}
        for key in sorted(new_pins):
            if new_pins[key] == old_pins.get(key):
                continue
            if len(new_pins[key]) != 1 or len(repo_pins.get(key, ())) > 1:
                return None
            if key not in repo_pins:
                continue
            (theirs,), (ours,) = new_pins[key], repo_pins[key]
            if ours == theirs:
                continue
            our_version, their_version = pin_version(ours), pin_version(theirs)
            if our_version is None or their_version is None or our_version == their_version:
                return None
            winners[key] = theirs if their_version > our_version else ours
            pins.append((key, ours, theirs, winners[key]))
        merged = apply_pins(merged, pattern, winners)
    return merged, pins


def _git_out(cwd: Path, *args: str) -> str | None:
    """A git command's stdout, or None when it fails (callers fail closed)."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def template_clone(src_path: str, workdir: Path) -> Path | None:
    """A clone of the template (no checkout: Copier and `git show` read commits), made
    once per workdir (one sync run). Not bare: Copier copies a bare repo as a plain
    directory. Not partial (--filter): Copier clones it again, and that fails on the
    blobs a partial clone lacks."""
    dest = workdir / ".dev-standards-template"
    if not dest.exists():
        cmd = ["clone", "-q", "--no-checkout", src_path, str(dest)]
        if _git_out(workdir, *cmd) is None:
            return None
    return dest


def template_settings(template: Path, commit: str) -> dict | None:
    """The template's copier.yml at `commit`, or None when it can't be read."""
    copier_yml = _git_out(template, "show", f"{commit}:copier.yml")
    return None if copier_yml is None else (yaml.safe_load(copier_yml) or {})


def missing_template_files(
    template: Path, commit: str, answers_text: str, dest: Path
) -> tuple[str, ...]:
    """What the template at `commit` renders with this repo's answers that the repo
    doesn't have, except `_skip_if_exists` files (the repo's own after the first
    render). Rendered into a scratch dir next to the clone."""
    settings = template_settings(template, commit)
    if settings is None:
        raise ValueError(f"can't read copier.yml at template commit {commit}")
    answers = yaml.safe_load(answers_text) or {}
    data = {key: value for key, value in answers.items() if not key.startswith("_")}
    repo_owned = pathspec.PathSpec.from_lines("gitignore", settings.get("_skip_if_exists", []))
    with tempfile.TemporaryDirectory(dir=dest.parent) as scratch:
        out = Path(scratch)
        run_copy(str(template), out, data=data, vcs_ref=commit, defaults=True, quiet=True)
        rendered = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    return tuple(
        rel for rel in rendered if not (dest / rel).exists() and not repo_owned.match_file(rel)
    )


def resolve_pin_only_file(
    template: Path, old_commit: str, new_commit: str, dest: Path, rel: str
) -> tuple[str, list[tuple[str, str, str, str]]] | None:
    """bump_template_pins for one conflicted file: the repo's committed version and
    the template source (`rel` or `rel.jinja`) at both commits. None when copier.yml
    changed in between (questions or settings can change what renders) or any
    of them can't be read."""
    if _git_out(template, "diff", "--quiet", old_commit, new_commit, "--", "copier.yml") is None:
        return None
    settings = template_settings(template, new_commit)
    repo_text = _git_out(dest, "show", f"HEAD:{rel}")
    if settings is None or repo_text is None:
        return None
    subdir = settings.get("_subdirectory", "").strip("/")
    prefix = f"{subdir}/" if subdir else ""
    for name in (rel, rel + settings.get("_templates_suffix", ".jinja")):
        old = _git_out(template, "show", f"{old_commit}:{prefix}{name}")
        new = _git_out(template, "show", f"{new_commit}:{prefix}{name}")
        if old is not None and new is not None:
            return bump_template_pins(repo_text, old, new)
    return None


def _find(lines: list[str], start: int, marker: str) -> int | None:
    return next((j for j in range(start, len(lines)) if lines[j].rstrip("\n") == marker), None)


def resolve_pin_conflicts(text: str) -> tuple[str, list[tuple[str, str, str, str]], int]:
    """(text with pin-only hunks resolved, the pins resolved, hunks left)."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    resolved: list[tuple[str, str, str, str]] = []
    left = 0
    i = 0
    while i < len(lines):
        if lines[i].rstrip("\n") != CONFLICT_START:
            out.append(lines[i])
            i += 1
            continue
        mid = _find(lines, i + 1, CONFLICT_MID)
        end = _find(lines, mid + 1, CONFLICT_END) if mid is not None else None
        if end is None:  # malformed: leave the rest untouched
            out.extend(lines[i:])
            left += 1
            break
        merged = merge_pin_only_hunk(
            out[-1] if out else "", "".join(lines[i + 1 : mid]), "".join(lines[mid + 1 : end])
        )
        if merged is None:
            out.extend(lines[i : end + 1])
            left += 1
        else:
            out.extend(merged[0].splitlines(keepends=True))
            resolved.extend(merged[1])
        i = end + 1
    return "".join(out), resolved, left


# ── one repo ────────────────────────────────────────────────────────────────


def clone_command(org: str, repo: str, dest: Path) -> list[str]:
    """The only network call per repo, read-only. Tests swap the source for a
    local file:// repo and keep CLONE_FLAGS."""
    return ["gh", "repo", "clone", f"{org}/{repo}", str(dest), "--", *CLONE_FLAGS]


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def clone(org: str, repo: str, dest: Path) -> bool:
    """Clone `repo`'s default branch into `dest`, not checked out yet (a repo that
    isn't onboarded needs no blobs). Returns whether it has a .copier-answers.yml."""
    _run(clone_command(org, repo, dest))
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{ANSWERS_FILE}"], cwd=dest, capture_output=True
    )
    return probe.returncode == 0


def checkout(dest: Path) -> None:
    _run(["git", "checkout", "-q"], cwd=dest)


def sync_repo(org: str, repo: str, workdir: Path) -> Result:
    dest = workdir / repo
    try:
        if not clone(org, repo, dest):
            return Result(repo, Status.NOT_ONBOARDED)
        checkout(dest)
        return update_clone(repo, dest)
    except subprocess.CalledProcessError as e:
        return Result(repo, Status.ERROR, message=(e.stderr or str(e)).strip())
    # The per-repo boundary: one repo's failure must not stop the others. Copier
    # raises more than CopierError here (ValueError for a `_commit` the template
    # doesn't have, plumbum's ProcessExecutionError when `_src_path` can't be
    # cloned), so catch any Exception. KeyboardInterrupt and SystemExit aren't
    # Exceptions: Ctrl-C still stops the run.
    except Exception as e:
        return Result(repo, Status.ERROR, message=f"{type(e).__name__}: {e}".strip())


def update_clone(repo: str, dest: Path) -> Result:
    """`copier update` on an onboarded, checked-out clone, pin conflicts resolved,
    and the change staged. Raises on failure (the caller reports it). `sync.py
    adopt` previews a first sync with it too."""
    base_branch = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=dest).strip()
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=dest).strip()
    old_answers = (dest / ANSWERS_FILE).read_text()
    run_update(
        str(dest),
        defaults=True,
        overwrite=True,
        conflict="inline",
        quiet=True,
        # Decision 3 in docs/template-sync.md: sync to main HEAD, never to a tag.
        vcs_ref="HEAD",
    )
    unmerged = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=dest).split()
    old_commit = _answer(old_answers, "_commit")
    new_commit = _answer((dest / ANSWERS_FILE).read_text(), "_commit")
    template = template_clone(_answer(old_answers, "_src_path"), dest.parent)
    resolved_pins = []
    conflicted = []
    for rel in unmerged:
        path = dest / rel
        whole_file = (
            resolve_pin_only_file(template, old_commit, new_commit, dest, rel)
            if template is not None
            else None
        )
        if whole_file is not None:
            text, resolved, left = *whole_file, 0
        else:
            text, resolved, left = resolve_pin_conflicts(path.read_text())
        path.write_text(text)
        resolved_pins += [ResolvedPin(rel, *pin) for pin in resolved]
        if left:
            conflicted.append(rel)
    _run(["git", "add", "-A"], cwd=dest)
    diff = _run(["git", "diff", "--cached", "HEAD"], cwd=dest)
    changed = _run(["git", "diff", "--cached", "--name-only", "HEAD"], cwd=dest).split()
    changes, unsupported_modes = _staged_changes(dest)
    new_answers = (dest / ANSWERS_FILE).read_text()
    missing = (
        missing_template_files(template, new_commit, new_answers, dest)
        if template is not None
        else ()
    )

    if conflicted:
        status = Status.CONFLICTS
    elif not changed:
        status = Status.UNCHANGED
    else:
        status = Status.CLEAN
    return Result(
        repo,
        status,
        diff=diff,
        answers_only=changed == [ANSWERS_FILE] and _only_commit_changed(diff),
        pin_only=status is Status.CLEAN and _pin_only(dest, changed),
        resolved_pins=tuple(resolved_pins),
        conflicted_files=tuple(conflicted),
        base_branch=base_branch,
        base_sha=base_sha,
        old_commit=_answer(old_answers, "_commit"),
        new_commit=_answer(new_answers, "_commit"),
        template_url=_answer(new_answers, "_src_path"),
        changes=changes,
        unsupported_modes=unsupported_modes,
        missing_files=missing,
    )


def _answer(answers_text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}: *(.+)$", answers_text, re.M)
    return match.group(1).strip().strip("'\"") if match else ""


_REGULAR_FILE = "100644"
_ABSENT = "000000"


def _staged_changes(
    dest: Path,
) -> tuple[tuple[tuple[str, bytes | None], ...], tuple[str, ...]]:
    """The staged change set as (path, new contents | None for a deletion), which is
    what a `createCommitOnBranch` call carries, plus the paths it can't carry: that
    API writes every file as a regular 100644 file, so a new executable or symlink,
    or a mode change, would be committed wrong. Those are reported, not read."""
    raw = _run(["git", "diff", "--cached", "--raw", "--no-renames", "HEAD"], cwd=dest)
    changes: list[tuple[str, bytes | None]] = []
    unsupported: list[str] = []
    for line in raw.splitlines():
        meta, path = line.split("\t", 1)
        old_mode, new_mode, _old_sha, _new_sha, status = meta.lstrip(":").split()
        if status == "D":
            changes.append((path, None))
        elif new_mode != _REGULAR_FILE or old_mode not in (_ABSENT, _REGULAR_FILE):
            unsupported.append(path)
        else:
            changes.append((path, (dest / path).read_bytes()))
    return tuple(changes), tuple(unsupported)


def _pin_only(dest: Path, changed: list[str]) -> bool:
    """Whether the staged change is nothing but pin bumps, plus `_commit` in the
    answers file. Fails closed: an added or deleted file, or one it can't read,
    is not pin-only."""
    for rel in changed:
        old = _git_out(dest, "show", f"HEAD:{rel}")
        new = _git_out(dest, "show", f":{rel}")
        if old is None or new is None:
            return False
        if rel == ANSWERS_FILE:
            if _without_commit(old) != _without_commit(new):
                return False
        elif sync_pins(old, new) != old:
            return False
    return bool(changed)


def _without_commit(answers_text: str) -> list[str]:
    return [line for line in answers_text.splitlines() if not line.startswith("_commit:")]


def _only_commit_changed(diff: str) -> bool:
    changes = [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return bool(changes) and all(line[1:].startswith("_commit:") for line in changes)


# ── reporting / CLI ─────────────────────────────────────────────────────────


def describe(result: Result) -> str:
    notes = []
    if result.answers_only:
        notes.append("answers only")
    elif result.pin_only:
        notes.append("pin bumps only (--apply auto-merges)")
    if result.pins_resolved:
        notes.append(f"{result.pins_resolved} pin conflict(s) auto-resolved to the newer version")
    if result.conflicted_files:
        notes.append(f"conflicts left in {', '.join(result.conflicted_files)}")
    if result.unsupported_modes:
        notes.append(
            "file mode the API commit can't carry (--apply refuses this repo): "
            + ", ".join(result.unsupported_modes)
        )
    if result.missing_files:
        notes.append(
            "template files missing (a sync never adds them back): "
            + ", ".join(result.missing_files)
        )
    if result.message:
        notes.append(result.message)
    line = f"[{result.status.value}] {result.repo}"
    return f"{line}: {'; '.join(notes)}" if notes else line


def load_repos(config_path: Path) -> tuple[str, dict[str, dict]]:
    """(org, repo name -> its github-standard.json entry), `_comment` keys dropped."""
    config = json.loads(config_path.read_text())
    return config["org"], {
        name: entry for name, entry in config["repos"].items() if not name.startswith("_")
    }


def skip_reason(entry: object) -> str | None:
    """The reason a repo opted out of template sync (`"template_sync": {"skip":
    "<reason>"}` in its github-standard.json entry), or None. Fails closed: a
    malformed entry raises ValueError, and the caller leaves the repo alone."""
    if not isinstance(entry, dict) or "template_sync" not in entry:
        return None
    section = entry["template_sync"]
    if not isinstance(section, dict):
        raise ValueError("template_sync must be an object")
    if set(section) != {"skip"}:  # `{}` or a misspelled `skpi`: meant to skip, written wrong
        raise ValueError(
            f"template_sync must have exactly the key `skip`, got {sorted(section) or 'none'}"
        )
    reason = section["skip"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("template_sync.skip must be a non-empty reason")
    return reason.strip()


def config_gate(repo: str, entry: object) -> Result | None:
    """A Result for a repo the config says not to touch (skipped, or a malformed
    skip entry), else None. Such a repo is never cloned."""
    try:
        reason = skip_reason(entry)
    except ValueError as e:
        return Result(repo, Status.ERROR, message=f"github-standard.json: {e}")
    return Result(repo, Status.SKIPPED, message=reason) if reason else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["adopt"]:
        import sync_adopt  # imports this module: imported here, not at the top

        return sync_adopt.main(argv[1:])
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", nargs="?", help="Only this registered repo.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Open (or update) a sync PR per repo with a real change. Default: dry run.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"github-standard.json to read the repos from (default: {DEFAULT_CONFIG}).",
    )
    args = parser.parse_args(argv)

    org, repos = load_repos(args.config.expanduser())
    if args.repo is not None:
        if args.repo not in repos:
            print(f"{args.repo} is not registered in {args.config}", file=sys.stderr)
            return 1
        repos = {args.repo: repos[args.repo]}

    if args.apply:
        problem = sync_pr.preflight()
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print("dev-standards template sync: APPLY (opens or updates sync PRs)")
    else:
        print("dev-standards template sync: DRY RUN (clones only; nothing is pushed)")
    results = []
    actions: dict[str, int] = {}
    apply_failed = 0
    with tempfile.TemporaryDirectory(prefix="dev-standards-sync-") as tmp:
        for repo, entry in repos.items():
            result = config_gate(repo, entry) or sync_repo(org, repo, Path(tmp))
            results.append(result)
            print(describe(result))
            if result.status in (Status.CLEAN, Status.CONFLICTS):
                print(result.diff, end="" if result.diff.endswith("\n") else "\n")
            if args.apply and wants_pr(result):
                label, detail = apply(org, repo, result)
                print(f"  apply: {label}: {detail}")
                actions[label] = actions.get(label, 0) + 1
                apply_failed += label == APPLY_FAILED

    counts = {status: sum(r.status is status for r in results) for status in Status}
    print(
        f"{len(results)} repos: "
        + ", ".join(f"{count} {status.value}" for status, count in counts.items())
    )
    if args.apply:
        done = ", ".join(f"{count} {action}" for action, count in actions.items())
        print(f"sync PRs: {done or 'nothing to do'}")
    return 1 if counts[Status.ERROR] or apply_failed else 0


def wants_pr(result: Result) -> bool:
    """A real change: clean (beyond `_commit` moving) or with conflicts left."""
    return result.status is Status.CONFLICTS or (
        result.status is Status.CLEAN and not result.answers_only
    )


APPLY_FAILED = "failed"


def apply(org: str, repo: str, result: Result) -> tuple[str, str]:
    """One repo's PR step as (label, detail); any failure is that repo's, and the
    other repos continue (KeyboardInterrupt still stops the run)."""
    try:
        action, detail = sync_pr.apply_repo(org, repo, result)
    except Exception as e:  # the per-repo boundary, as in sync_repo
        return APPLY_FAILED, f"{type(e).__name__}: {e}"
    return action.value, detail


if __name__ == "__main__":
    sys.exit(main())
