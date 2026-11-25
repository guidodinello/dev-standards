"""sync_adopt — `sync.py adopt <repo>`: onboard a registered repo that was bootstrapped
before Copier (no .copier-answers.yml), so `sync.py` can sync it (issue #53,
decision 2 in docs/template-sync.md).

1. Clone the repo (as sync.py does) and detect its answers with init.py's own
   detection (`init.detect_answers`). `branch` is the clone's default branch;
   `release` and `locked` are init.py flags, not detected, so they are read from
   the files the repo already has (a release workflow; `uv sync --locked` in CI).
2. Pick `_commit`: render every dev-standards `main` commit that changed the
   template with those answers, and keep the one whose render differs least from
   the repo's files (ties: the newest). A commit that renders a different set of
   files than the current template is no candidate: a file added later, which the
   repo already has, would come back from the first sync as a whole-file conflict.
3. Report the answers, each candidate's score, the residual (what still differs
   from the chosen render, which `copier update` keeps as the repo's own edits from
   then on) and the diff the first sync would make.
4. With --apply, open a PR that adds only .copier-answers.yml, through the same
   signed-commit path and branch as a sync PR (sync_pr.apply_repo). Once it merges,
   `sync.py --apply` opens the first sync PR.
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import init
import sync
import sync_pr
from sync import ANSWERS_FILE, Result, Status

CI_WORKFLOW = ".github/workflows/ci.yml"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
TEMPLATE_PATHS = ("copier.yml", "templates/copier")  # what makes a commit a candidate
# The clone's commit of the answers file, for the first-sync preview only: never pushed.
_LOCAL_COMMIT = (
    "-c",
    "user.name=dev-standards adopt",
    "-c",
    "user.email=adopt@dev-standards.invalid",
    "-c",
    "commit.gpgsign=false",
)


class AdoptError(Exception):
    """This repo can't be adopted (already onboarded, no usable candidate)."""


def template_source() -> str:
    """The dev-standards repo to render from and record as `_src_path`. Tests point
    it at a scratch template repo."""
    return init.TEMPLATE_URL


@dataclass(frozen=True, slots=True)
class Candidate:
    sha: str
    ref: str  # its `_commit` in the answers file
    score: int  # lines that differ from the repo's files
    answers_text: str  # .copier-answers.yml as rendered at this commit
    residual: tuple[tuple[str, int, str], ...]  # (path, lines that differ, unified diff)


@dataclass(frozen=True, slots=True)
class Adoption:
    repo: str
    answers: dict[str, object]
    candidates: tuple[Candidate, ...]  # newest first
    chosen: Candidate
    preview: Result  # the first sync after the answers file lands
    base_branch: str
    base_sha: str

    def as_result(self) -> Result:
        """The change sync_pr commits: the answers file alone."""
        return Result(
            self.repo,
            Status.CLEAN,
            base_branch=self.base_branch,
            base_sha=self.base_sha,
            new_commit=self.chosen.ref,
            template_url=template_source(),
            changes=((ANSWERS_FILE, self.chosen.answers_text.encode()),),
        )


# ── answers ─────────────────────────────────────────────────────────────────


def detect(dest: Path) -> dict[str, object]:
    """The Copier answers for a checked-out clone: init.py's detection plus what the
    existing files say about init.py's flags."""
    ci = dest / CI_WORKFLOW
    return {
        **init.detect_answers(dest),
        "locked": ci.is_file() and "uv sync --locked" in ci.read_text(),
        "branch": sync._run(["git", "symbolic-ref", "--short", "HEAD"], cwd=dest).strip(),
        "release": (dest / RELEASE_WORKFLOW).is_file(),
    }


# ── choosing `_commit` ──────────────────────────────────────────────────────


def _diff(path: str, rendered: str, current: str | None) -> tuple[int, str]:
    lines = list(
        difflib.unified_diff(
            rendered.splitlines(keepends=True),
            (current or "").splitlines(keepends=True),
            f"template/{path}",
            f"repo/{path}" if current is not None else "/dev/null",
        )
    )
    changed = sum(1 for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---")))
    return changed, "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _score(files: dict[str, str], dest: Path) -> tuple[int, tuple[tuple[str, int, str], ...]]:
    """Lines that differ between a render and the repo, per rendered file. The
    answers file and the repo-owned `_skip_if_exists` files don't count."""
    owned = init.skip_if_exists_patterns()
    residual = []
    for path, text in sorted(files.items()):
        if path == ANSWERS_FILE or any(fnmatch(path, pattern) for pattern in owned):
            continue
        current = dest / path
        changed, diff = _diff(path, text, current.read_text() if current.is_file() else None)
        if changed:
            residual.append((path, changed, diff))
    return sum(changed for _, changed, _ in residual), tuple(residual)


def candidates(template: Path, answers: dict[str, object], dest: Path) -> list[Candidate]:
    """Every first-parent commit that changed the template, newest first, rendered
    with `answers` and scored against the repo. Only commits that render the same
    set of files as the newest one count; a commit that can't render is dropped."""
    shas = sync._run(
        ["git", "rev-list", "--first-parent", "HEAD", "--", *TEMPLATE_PATHS], cwd=template
    ).split()
    found: list[Candidate] = []
    head_paths: set[str] | None = None
    for sha in shas:
        try:
            files = {
                path.as_posix(): text
                for path, text in init.render(
                    Path(), answers, src=template, vcs_ref=sha, src_url=template_source()
                ).items()
            }
        except Exception:  # an old commit the current answers can't render
            if head_paths is None:
                raise
            continue
        if head_paths is None:
            head_paths = set(files)
        elif set(files) != head_paths:
            continue
        score, residual = _score(files, dest)
        answers_text = files[ANSWERS_FILE]
        found.append(
            Candidate(sha, sync._answer(answers_text, "_commit"), score, answers_text, residual)
        )
    return found


def adopt_repo(org: str, repo: str, workdir: Path) -> Adoption:
    """Clone, detect, choose `_commit`, preview the first sync. Raises AdoptError,
    init.DetectionError, or whatever git or Copier raise."""
    dest = workdir / repo
    if sync.clone(org, repo, dest):
        raise AdoptError(f"already onboarded: it has {ANSWERS_FILE}; sync it with sync.py")
    sync.checkout(dest)
    answers = detect(dest)
    template = workdir / "_dev-standards-template"
    # Full clone: rendering an older commit needs history.
    sync._run(["git", "clone", "-q", template_source(), str(template)])
    found = candidates(template, answers, dest)
    if not found:
        raise AdoptError("no template commit renders for these answers")
    chosen = min(found, key=lambda c: c.score)  # min keeps the first (newest) on a tie

    base_branch = str(answers["branch"])
    base_sha = sync._run(["git", "rev-parse", "HEAD"], cwd=dest).strip()
    (dest / ANSWERS_FILE).write_text(chosen.answers_text)
    sync._run(["git", "add", ANSWERS_FILE], cwd=dest)
    sync._run(["git", *_LOCAL_COMMIT, "commit", "-qm", "adopt (local preview)"], cwd=dest)
    return Adoption(
        repo,
        answers,
        tuple(found),
        chosen,
        sync.update_clone(repo, dest),
        base_branch,
        base_sha,
    )


# ── reporting ───────────────────────────────────────────────────────────────


def _answers_line(answers: dict[str, object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in answers.items())


def _missing(diff: str) -> bool:
    """The residual diff of a rendered file the repo doesn't have. Copier takes it as
    deleted on purpose: `copier update` doesn't add it back."""
    return "\n+++ /dev/null\n" in diff


def report(adoption: Adoption) -> str:
    chosen = adoption.chosen
    lines = [
        f"[adopt] {adoption.repo}: template commit {chosen.ref} "
        f"({chosen.score} line(s) differ from it)",
        f"  answers: {_answers_line(adoption.answers)}",
        "  candidates (newest first, lines that differ): "
        + ", ".join(f"{c.ref} {c.score}" for c in adoption.candidates),
    ]
    if chosen.residual:
        lines.append(
            "  kept as this repo's own edits by every later sync (fix by hand to take "
            "the template's version):"
        )
        lines += [
            f"    {path}: {changed} line(s)"
            + (" (missing here: no sync will add it)" if _missing(diff) else "")
            for path, changed, diff in chosen.residual
        ]
        lines += [diff.rstrip("\n") for _, _, diff in chosen.residual]
    lines.append(f"  first sync after the adopt PR merges: {sync.describe(adoption.preview)}")
    if adoption.preview.diff:
        lines.append(adoption.preview.diff.rstrip("\n"))
    return "\n".join(lines) + "\n"


def pr_text(adoption: Adoption) -> sync_pr.PrText:
    chosen, preview = adoption.chosen, adoption.preview
    files = [path for path, _ in preview.changes if path != ANSWERS_FILE]
    if preview.status is Status.UNCHANGED or preview.answers_only:
        first_sync = "nothing to change: the repo is on the current template."
    else:
        first_sync = f"{len(files)} file(s) to update" + (
            f", conflicts in {len(preview.conflicted_files)}."
            if preview.conflicted_files
            else ", no conflicts."
        )
    body = "\n".join(
        [
            f"Onboards this repo onto dev-standards template sync at `{chosen.ref}`. "
            f"Part of {sync_pr.ISSUE}.",
            "",
            f"Adds only `{ANSWERS_FILE}`, generated by `sync.py adopt`: the answers come "
            "from `init.py`'s detection, and `_commit` is the template commit whose "
            "render differs least from this repo's files "
            f"({chosen.score} line(s) in {len(chosen.residual)} file(s)).",
            "",
            "### Answers",
            *(f"- `{key}`: `{value}`" for key, value in adoption.answers.items()),
            "",
            "### After merging",
            f"`sync.py --apply` opens the first sync PR: {first_sync}",
            "Lines that differ from the chosen template commit stay as this repo's own "
            "edits in every later sync; to take the template's version of a line, edit "
            "it by hand.",
            "",
            "---",
            f"Re-running `sync.py adopt --apply` rebuilds `{sync_pr.SYNC_BRANCH}` on the "
            "current default branch, unless it has a commit `sync.py` didn't make.",
        ]
    )
    return sync_pr.PrText(
        f"chore(dev-standards): onboard template sync at {chosen.ref}",
        body + "\n",
        f"Adds {ANSWERS_FILE} at template commit {chosen.ref} (sync.py adopt).",
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sync.py adopt",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repo", help="The registered repo to onboard.")
    parser.add_argument(
        "--apply", action="store_true", help="Open (or update) its adopt PR. Default: dry run."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=sync.DEFAULT_CONFIG,
        help=f"github-standard.json to read the repo from (default: {sync.DEFAULT_CONFIG}).",
    )
    args = parser.parse_args(argv)

    org, repos = sync.load_repos(args.config.expanduser())
    if args.repo not in repos:
        print(f"{args.repo} is not registered in {args.config}", file=sys.stderr)
        return 1
    gate = sync.config_gate(args.repo, repos[args.repo])
    if gate is not None:
        print(sync.describe(gate), file=sys.stderr)
        return 1
    if args.apply:
        problem = sync_pr.preflight()
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print("dev-standards adopt: APPLY (opens or updates the adopt PR)")
    else:
        print("dev-standards adopt: DRY RUN (clones only; nothing is pushed)")

    with tempfile.TemporaryDirectory(prefix="dev-standards-adopt-") as tmp:
        try:
            adoption = adopt_repo(org, args.repo, Path(tmp))
        except subprocess.CalledProcessError as e:
            print(f"[error] {args.repo}: {(e.stderr or str(e)).strip()}")
            return 1
        except Exception as e:  # one repo: report it rather than a traceback
            print(f"[error] {args.repo}: {type(e).__name__}: {e}")
            return 1
    print(report(adoption), end="")
    if not args.apply:
        return 0
    try:
        action, detail = sync_pr.apply_repo(org, args.repo, adoption.as_result(), pr_text(adoption))
    except Exception as e:
        print(f"  apply: {sync.APPLY_FAILED}: {type(e).__name__}: {e}")
        return 1
    print(f"  apply: {action.value}: {detail}")
    return 0
