"""sync_pr — turn one repo's `sync.py` result into a pull request (`sync.py --apply`).

Every GitHub call goes through `github()`, one `gh api` invocation, so tests can
replace that single function with an in-memory fake. Per repo:

1. Look for the sync PR, on the stable branch `dev-standards/sync`.
   - An open sync PR whose head commit lacks the `Dev-Standards-Sync` trailer has
     a commit someone else pushed (a conflict fix, GitHub's "Update branch"): it
     is left alone, never force-overwritten.
   - An open sync PR whose trailers already name this template commit and this
     base commit holds this exact result: nothing to do.
   - A sync PR for this template commit that was closed without merging was
     declined by a human: not reopened. A newer template commit opens a new one.
2. Commit the change set with GraphQL `createCommitOnBranch`, which GitHub signs
   (rulesets that require signed commits accept it). The commit is built on a
   scratch branch at the base commit, then `dev-standards/sync` is moved straight
   onto it. The sync branch never points at the base commit itself, so an open
   PR never goes empty mid-update.
3. Open the PR against the default branch, or update the open one's title and
   body. A result with conflicts left asks for a draft. GitHub Free has no draft
   PRs on private repos, so when the draft is refused it falls back to a normal
   PR titled `[conflicts] ...` whose body says not to merge (Copier's markers keep
   CI red anyway).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sync import Result

SYNC_BRANCH = "dev-standards/sync"
SCRATCH_BRANCH = "dev-standards/sync-next"
CONFLICTS_PREFIX = "[conflicts] "
ISSUE = "guidodinello/dev-standards#53"
TRAILER_SYNC = "Dev-Standards-Sync"
TRAILER_TEMPLATE = "Dev-Standards-Template"
TRAILER_BASE = "Dev-Standards-Base"

_CREATE_COMMIT = (
    "mutation($input: CreateCommitOnBranchInput!) {"
    " createCommitOnBranch(input: $input) { commit { oid } } }"
)


class GitHubError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


class Action(Enum):
    OPENED = "opened PR"
    OPENED_DRAFT = "opened draft PR"
    OPENED_CONFLICTS = "opened [conflicts] PR (draft PRs not available)"
    UPDATED = "updated PR"
    SKIPPED = "skipped"


def github(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """The only GitHub I/O for --apply: one `gh api` call (REST path or `graphql`)."""
    cmd = ["gh", "api", "-X", method, path]
    if payload is not None:
        cmd += ["--input", "-"]
    proc = subprocess.run(
        cmd,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        status = re.search(r"\(HTTP (\d{3})\)", proc.stderr)
        raise GitHubError(int(status.group(1)) if status else None, proc.stderr.strip())
    data = json.loads(proc.stdout) if proc.stdout.strip() else None
    if isinstance(data, dict) and data.get("errors"):
        raise GitHubError(None, json.dumps(data["errors"]))
    return data


def token_scopes() -> set[str] | None:
    """The classic OAuth scopes of gh's token, or None when GitHub doesn't report
    them (fine-grained tokens). Read-only."""
    proc = subprocess.run(["gh", "api", "-i", "user"], capture_output=True, text=True)
    match = re.search(r"^x-oauth-scopes:(.*)$", proc.stdout, re.M | re.I)
    if proc.returncode != 0 or match is None:
        return None
    return {scope.strip() for scope in match.group(1).split(",") if scope.strip()}


def preflight() -> str | None:
    """An error message when --apply can't work with this token, else None. A sync
    almost always edits .github/workflows/, which GitHub rejects without `workflow`."""
    scopes = token_scopes()
    if scopes is not None and "workflow" not in scopes:
        return (
            "gh's token lacks the `workflow` scope, so GitHub would reject every commit "
            "that touches .github/workflows/. Run: gh auth refresh -s workflow"
        )
    return None


def title_for(result: Result) -> str:
    return f"chore(dev-standards): sync templates to {result.new_commit}"


def _trailers(message: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z-]+): *(.+)$", message, re.M))


def commit_message(result: Result) -> tuple[str, str]:
    body = (
        f"Template {result.old_commit}..{result.new_commit}, applied with copier update.\n\n"
        f"{TRAILER_SYNC}: 1\n"
        f"{TRAILER_TEMPLATE}: {result.new_commit}\n"
        f"{TRAILER_BASE}: {result.base_sha}\n"
    )
    return title_for(result), body


def _compare_link(result: Result) -> str:
    span = f"`{result.old_commit}`..`{result.new_commit}`"
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?", result.template_url)
    if not match:
        return span
    url = f"https://github.com/{match.group(1)}/compare/{result.old_commit}...{result.new_commit}"
    return f"[{span}]({url})"


def pr_body(result: Result) -> str:
    lines = [
        f"Syncs this repo with the dev-standards template: {_compare_link(result)}.",
        "",
        f"Generated by `sync.py --apply` (`copier update`). Part of {ISSUE}.",
        "",
        "### Files",
        *(
            f"- `{path}`" + (" (deleted)" if contents is None else "")
            for path, contents in result.changes
        ),
    ]
    if result.resolved_pins:
        lines += [
            "",
            "### Pin conflicts resolved to the newer version",
            "This repo's Dependabot and the template both bumped these pins:",
            *(
                f"- `{pin.file}`: `{pin.name}`: repo `{pin.ours}`, template `{pin.theirs}`"
                f" → kept `{pin.kept}`"
                for pin in result.resolved_pins
            ),
        ]
    if result.conflicted_files:
        lines += [
            "",
            "### Conflicts to resolve — do not merge as is",
            "Copier left inline conflict markers (`<<<<<<< before updating` … "
            "`>>>>>>> after updating`) in:",
            *(f"- `{path}`" for path in result.conflicted_files),
            "",
            "Resolve them on this branch; CI fails while the markers remain.",
        ]
    lines += [
        "",
        "---",
        f"Re-running `sync.py --apply` rebuilds `{SYNC_BRANCH}` on the current default "
        "branch, unless it has a commit `sync.py` didn't make (e.g. a conflict fix): "
        "then this PR is left alone.",
    ]
    return "\n".join(lines) + "\n"


def _set_ref(repo_path: str, branch: str, sha: str) -> None:
    """Point `branch` at `sha`, creating it if needed."""
    try:
        github("PATCH", f"{repo_path}/git/refs/heads/{branch}", {"sha": sha, "force": True})
    except GitHubError as e:
        if e.status != 422:  # 422: the reference doesn't exist yet
            raise
        github("POST", f"{repo_path}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})


def push_commit(org: str, repo: str, result: Result) -> str:
    """Commit the change set on top of the base commit and point the sync branch at
    it. Returns the new commit's oid."""
    repo_path = f"repos/{org}/{repo}"
    _set_ref(repo_path, SCRATCH_BRANCH, result.base_sha)
    headline, body = commit_message(result)
    data = github(
        "POST",
        "graphql",
        {
            "query": _CREATE_COMMIT,
            "variables": {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": f"{org}/{repo}",
                        "branchName": SCRATCH_BRANCH,
                    },
                    "expectedHeadOid": result.base_sha,
                    "message": {"headline": headline, "body": body},
                    "fileChanges": {
                        "additions": [
                            {"path": path, "contents": base64.b64encode(contents).decode()}
                            for path, contents in result.changes
                            if contents is not None
                        ],
                        "deletions": [
                            {"path": path} for path, contents in result.changes if contents is None
                        ],
                    },
                }
            },
        },
    )
    oid = data["data"]["createCommitOnBranch"]["commit"]["oid"]
    _set_ref(repo_path, SYNC_BRANCH, oid)
    github("DELETE", f"{repo_path}/git/refs/heads/{SCRATCH_BRANCH}")
    return oid


def apply_repo(org: str, repo: str, result: Result) -> tuple[Action, str]:
    """Open or update this repo's sync PR. Returns (action, detail: URL or reason)."""
    repo_path = f"repos/{org}/{repo}"
    title = title_for(result)
    pulls = github("GET", f"{repo_path}/pulls?state=all&head={org}:{SYNC_BRANCH}&per_page=100")
    open_pr = next((pr for pr in pulls if pr["state"] == "open"), None)

    if open_pr is None:
        declined = [
            pr
            for pr in pulls
            if pr["state"] == "closed"
            and not pr.get("merged_at")
            and pr["title"].removeprefix(CONFLICTS_PREFIX) == title
        ]
        if declined:
            return Action.SKIPPED, (
                f"a sync PR for this template commit was closed without merging "
                f"({declined[0]['html_url']})"
            )
    else:
        head = github("GET", f"{repo_path}/commits/{open_pr['head']['sha']}")
        trailers = _trailers(head["commit"]["message"])
        if TRAILER_SYNC not in trailers:
            return Action.SKIPPED, (
                f"{SYNC_BRANCH} has a commit sync.py didn't make ({open_pr['html_url']}); "
                "merge or close that PR and delete the branch to sync again"
            )
        if (
            trailers.get(TRAILER_TEMPLATE) == result.new_commit
            and trailers.get(TRAILER_BASE) == result.base_sha
        ):
            return Action.SKIPPED, f"already up to date ({open_pr['html_url']})"

    push_commit(org, repo, result)
    body = pr_body(result)
    conflicts = bool(result.conflicted_files)

    if open_pr is not None:
        flagged = conflicts and not open_pr.get("draft")
        github(
            "PATCH",
            f"{repo_path}/pulls/{open_pr['number']}",
            {"title": CONFLICTS_PREFIX + title if flagged else title, "body": body},
        )
        return Action.UPDATED, open_pr["html_url"]

    new_pr = {"title": title, "head": SYNC_BRANCH, "base": result.base_branch, "body": body}
    if not conflicts:
        return Action.OPENED, github("POST", f"{repo_path}/pulls", new_pr)["html_url"]
    try:
        pr = github("POST", f"{repo_path}/pulls", {**new_pr, "draft": True})
        return Action.OPENED_DRAFT, pr["html_url"]
    except GitHubError as e:
        if e.status != 422 or "draft" not in e.message.lower():
            raise
    pr = github("POST", f"{repo_path}/pulls", {**new_pr, "title": CONFLICTS_PREFIX + title})
    return Action.OPENED_CONFLICTS, pr["html_url"]
