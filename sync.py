#!/usr/bin/env -S uv run
"""sync — preview what the current dev-standards template would change in every repo
rendered from it (issue #53). DRY RUN ONLY: it clones, never pushes.

For each repo in github-standard.json (read, never written) it makes a fresh,
shallow clone of the default branch into a temp dir. A repo without a
.copier-answers.yml there isn't onboarded and is skipped. Otherwise it runs
`copier update` (a 3-way merge from the template commit the repo was rendered
from to dev-standards' main HEAD), then resolves pin-only conflicts and reports:

    unchanged   the repo already matches the template
    clean       the update applies without conflicts ("answers only" when nothing
                but `_commit` in .copier-answers.yml changed)
    conflicts   some hunks need a human; Copier's inline markers are left in the diff
    error       the clone or the update failed (the other repos still run)

Pin conflicts (decided in docs/template-sync.md): when the repo's Dependabot and
the template both bumped the same pin, the conflict hunk is resolved to the newer
version, but only when the two sides differ in nothing but pins (a pre-commit
`rev`, or a SHA-pinned `uses:` with its `# vX` comment). Anything else, or a
version it can't compare, stays a conflict.

Usage:
    ./sync.py                       # every registered repo
    ./sync.py my-repo               # just one
    ./sync.py --config path/to/github-standard.json

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

try:
    from copier import run_update
    from copier.errors import CopierError
    from packaging.version import InvalidVersion, Version
except ImportError:
    sys.exit(
        "sync.py needs Copier: run it from the dev-standards checkout as ./sync.py "
        "(its shebang is `uv run`) or `uv run ./sync.py`."
    )

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
    NOT_ONBOARDED = "not onboarded"
    UNCHANGED = "unchanged"
    CLEAN = "clean"
    CONFLICTS = "conflicts"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Result:
    repo: str
    status: Status
    diff: str = ""
    answers_only: bool = False
    pins_resolved: int = 0
    conflicted_files: tuple[str, ...] = field(default_factory=tuple)
    message: str = ""


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


def merge_pin_only_hunk(context: str, ours: str, theirs: str) -> str | None:
    """Resolve one conflict hunk, or return None to leave it as a conflict.

    `ours` is the repo's side, `theirs` the template's; `context` is the line just
    above the hunk, prepended to both so a pre-commit `rev:` is seen together with
    its `repo:` line. The hunk is pin-only when putting our pin values into theirs
    gives back ours exactly. Each differing pin then takes the newer version.
    Fails closed: any version it can't compare, or equal versions with different
    SHAs, leaves the conflict."""
    ours_text, theirs_text = context + ours, context + theirs
    if ours_text == theirs_text:
        return ours
    if sync_pins(ours_text, theirs_text) != ours_text:
        return None  # differs in more than pins
    merged = ours_text
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
        merged = apply_pins(merged, pattern, winners)
    return merged.removeprefix(context)


def _find(lines: list[str], start: int, marker: str) -> int | None:
    return next((j for j in range(start, len(lines)) if lines[j].rstrip("\n") == marker), None)


def resolve_pin_conflicts(text: str) -> tuple[str, int, int]:
    """(text with pin-only hunks resolved, hunks resolved, hunks left)."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    resolved = left = 0
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
            out.extend(merged.splitlines(keepends=True))
            resolved += 1
        i = end + 1
    return "".join(out), resolved, left


# ── one repo ────────────────────────────────────────────────────────────────


def clone_command(org: str, repo: str, dest: Path) -> list[str]:
    """The only network call per repo, read-only. Tests swap the source for a
    local file:// repo and keep CLONE_FLAGS."""
    return ["gh", "repo", "clone", f"{org}/{repo}", str(dest), "--", *CLONE_FLAGS]


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def sync_repo(org: str, repo: str, workdir: Path) -> Result:
    dest = workdir / repo
    try:
        _run(clone_command(org, repo, dest))
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{ANSWERS_FILE}"], cwd=dest, capture_output=True
        )
        if probe.returncode != 0:
            return Result(repo, Status.NOT_ONBOARDED)
        _run(["git", "checkout", "-q"], cwd=dest)
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
        pins_resolved = 0
        conflicted = []
        for rel in unmerged:
            path = dest / rel
            text, resolved, left = resolve_pin_conflicts(path.read_text())
            path.write_text(text)
            pins_resolved += resolved
            if left:
                conflicted.append(rel)
        _run(["git", "add", "-A"], cwd=dest)
        diff = _run(["git", "diff", "--cached", "HEAD"], cwd=dest)
        changed = _run(["git", "diff", "--cached", "--name-only", "HEAD"], cwd=dest).split()
    except subprocess.CalledProcessError as e:
        return Result(repo, Status.ERROR, message=(e.stderr or str(e)).strip())
    except CopierError as e:
        return Result(repo, Status.ERROR, message=str(e))

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
        pins_resolved=pins_resolved,
        conflicted_files=tuple(conflicted),
    )


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
    if result.pins_resolved:
        notes.append(f"{result.pins_resolved} pin conflict(s) auto-resolved to the newer version")
    if result.conflicted_files:
        notes.append(f"conflicts left in {', '.join(result.conflicted_files)}")
    if result.message:
        notes.append(result.message)
    line = f"[{result.status.value}] {result.repo}"
    return f"{line}: {'; '.join(notes)}" if notes else line


def load_repos(config_path: Path) -> tuple[str, list[str]]:
    config = json.loads(config_path.read_text())
    return config["org"], [name for name in config["repos"] if not name.startswith("_")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", nargs="?", help="Only this registered repo.")
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
        repos = [args.repo]

    print("dev-standards template sync: DRY RUN (clones only; nothing is pushed)")
    results = []
    with tempfile.TemporaryDirectory(prefix="dev-standards-sync-") as tmp:
        for repo in repos:
            result = sync_repo(org, repo, Path(tmp))
            results.append(result)
            print(describe(result))
            if result.status in (Status.CLEAN, Status.CONFLICTS):
                print(result.diff, end="" if result.diff.endswith("\n") else "\n")

    counts = {status: sum(r.status is status for r in results) for status in Status}
    print(
        f"{len(results)} repos: "
        + ", ".join(f"{count} {status.value}" for status, count in counts.items())
    )
    return 1 if counts[Status.ERROR] else 0


if __name__ == "__main__":
    sys.exit(main())
