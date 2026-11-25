"""sync_pr — turn one repo's `sync.py` result into a pull request (`sync.py --apply`).

Every GitHub call goes through `github()`, one `gh api` invocation, so tests can
replace that single function with an in-memory fake. Per repo:

1. If the stable branch `dev-standards/sync` exists, its head must be a commit
   sync.py made, or the repo is skipped: a human's work there (a conflict fix,
   GitHub's "Update branch", an amended sync commit) is never overwritten, with or
   without an open PR. "Made by sync.py" needs all of: the `Dev-Standards-*`
   trailers; a single parent equal to the base commit the trailers name;
   committed by GitHub itself (`web-flow`, verified signature), as
   `createCommitOnBranch` commits are; and a file list matching the
   `Dev-Standards-Content` digest. An amend keeps the trailers but not the
   committer, and a GitHub-made edit keeps neither the parent nor the content.
2. A sync PR closed without merging, whose head names this template commit, was
   declined by a human: not reopened. A newer template commit opens a new one.
3. When the branch head already is this exact result (same template, base and
   content), no commit is made. Otherwise the change set is committed with GraphQL
   `createCommitOnBranch`, which GitHub signs (rulesets that require signed
   commits accept it). The commit is built on a scratch branch at the base commit,
   then `dev-standards/sync` is moved straight onto it, so the sync branch never
   points at the base commit and an open PR never goes empty mid-update.
4. The PR is always reconciled, even when no commit was needed: opened if missing,
   its title and body rewritten if they differ. A run that failed after moving the
   branch is repaired by the next one.
5. A result with conflicts left asks for a draft. If GitHub refuses the draft (for
   any reason; draft availability on private repos has depended on the plan), it
   falls back to a normal PR titled `[conflicts] ...` whose body says not to merge.
   The rendered pre-commit config's `check-merge-conflict` hook keeps CI red while
   any conflict marker remains, in any file.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
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
TRAILER_CONTENT = "Dev-Standards-Content"
GITHUB_COMMITTER = "web-flow"  # the committer of commits GitHub makes (createCommitOnBranch)
DECLINED_LOOKBACK = 5  # closed sync PRs checked for a declined template commit

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
    OPENED_CONFLICTS = "opened [conflicts] PR (draft refused)"
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


def token_scopes() -> set[str]:
    """The classic OAuth scopes of gh's token. Read-only. Raises GitHubError when
    they can't be read: the lookup failed, or the token reports none (fine-grained),
    so `workflow` can't be verified."""
    proc = subprocess.run(["gh", "api", "-i", "user"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(None, f"couldn't read gh's token scopes: {proc.stderr.strip()}")
    match = re.search(r"^x-oauth-scopes:(.*)$", proc.stdout, re.M | re.I)
    if match is None:
        raise GitHubError(
            None,
            "gh's token reports no OAuth scopes (a fine-grained token?), so the "
            "`workflow` scope can't be verified; use a classic token with it",
        )
    return {scope.strip() for scope in match.group(1).split(",") if scope.strip()}


def preflight() -> str | None:
    """An error message when --apply can't safely run with this token, else None.
    Fails closed: a scope check that can't complete stops the run. A sync almost
    always edits .github/workflows/, which GitHub rejects without `workflow`."""
    try:
        scopes = token_scopes()
    except GitHubError as e:
        return f"{e.message}. Stopping before any write."
    if "workflow" not in scopes:
        return (
            "gh's token lacks the `workflow` scope, so GitHub would reject every commit "
            "that touches .github/workflows/. Run: gh auth refresh -s workflow"
        )
    return None


# ── what a sync commit says about itself ────────────────────────────────────


def title_for(result: Result) -> str:
    return f"chore(dev-standards): sync templates to {result.new_commit}"


def blob_sha(contents: bytes) -> str:
    """Git's blob id for `contents` (what GitHub reports as a commit file's sha)."""
    return hashlib.sha1(b"blob %d\0" % len(contents) + contents).hexdigest()


def _digest(entries: list[tuple[str, str]]) -> str:
    lines = "".join(f"{path}\t{blob}\n" for path, blob in sorted(entries))
    return hashlib.sha256(lines.encode()).hexdigest()


def content_digest(result: Result) -> str:
    """The change set as (path, blob id | "-" for a deletion), hashed."""
    return _digest(
        [
            (path, "-" if contents is None else blob_sha(contents))
            for path, contents in result.changes
        ]
    )


def _commit_files_digest(files: list[dict[str, Any]]) -> str:
    entries = []
    for f in files:
        if f["status"] == "removed":
            entries.append((f["filename"], "-"))
            continue
        if f["status"] == "renamed":  # GitHub pairs a delete + add it sees as a rename
            entries.append((f["previous_filename"], "-"))
        entries.append((f["filename"], f["sha"]))
    return _digest(entries)


def _trailers(message: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z-]+): *(.+)$", message, re.M))


def commit_message(result: Result) -> tuple[str, str]:
    body = (
        f"Template {result.old_commit}..{result.new_commit}, applied with copier update.\n\n"
        f"{TRAILER_SYNC}: 1\n"
        f"{TRAILER_TEMPLATE}: {result.new_commit}\n"
        f"{TRAILER_BASE}: {result.base_sha}\n"
        f"{TRAILER_CONTENT}: {content_digest(result)}\n"
    )
    return title_for(result), body


def sync_commit_trailers(repo_path: str, sha: str) -> dict[str, str] | None:
    """The trailers of `sha` if sync.py made that commit, else None (see the module
    docstring, step 1, for what that takes)."""
    commit = github("GET", f"{repo_path}/commits/{sha}")
    trailers = _trailers(commit["commit"]["message"])
    if TRAILER_SYNC not in trailers or TRAILER_CONTENT not in trailers:
        return None
    if [parent["sha"] for parent in commit["parents"]] != [trailers.get(TRAILER_BASE)]:
        return None
    if (commit.get("committer") or {}).get("login") != GITHUB_COMMITTER:
        return None
    if not commit["commit"].get("verification", {}).get("verified"):
        return None
    if _commit_files_digest(commit.get("files", [])) != trailers[TRAILER_CONTENT]:
        return None
    return trailers


# ── PR text ─────────────────────────────────────────────────────────────────


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
            "Resolve them on this branch; CI (pre-commit's `check-merge-conflict`) fails "
            "while the markers remain.",
        ]
    lines += [
        "",
        "---",
        f"Re-running `sync.py --apply` rebuilds `{SYNC_BRANCH}` on the current default "
        "branch, unless it has a commit `sync.py` didn't make (e.g. a conflict fix): "
        "then this PR is left alone.",
    ]
    return "\n".join(lines) + "\n"


# ── writes ──────────────────────────────────────────────────────────────────


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
    # Cleanup only: a leftover scratch ref is reset by the next run.
    with contextlib.suppress(GitHubError):
        github("DELETE", f"{repo_path}/git/refs/heads/{SCRATCH_BRANCH}")
    return oid


def _branch_head(repo_path: str, branch: str) -> str | None:
    try:
        return github("GET", f"{repo_path}/git/ref/heads/{branch}")["object"]["sha"]
    except GitHubError as e:
        if e.status == 404:
            return None
        raise


def _declined(repo_path: str, pulls: list[dict[str, Any]], result: Result) -> str | None:
    """The URL of a sync PR closed without merging whose head names this template
    commit, if any. Matched by the head commit's trailer, not the (editable) title."""
    closed = [pr for pr in pulls if pr["state"] == "closed" and not pr.get("merged_at")]
    for pr in closed[:DECLINED_LOOKBACK]:
        commit = github("GET", f"{repo_path}/commits/{pr['head']['sha']}")
        if _trailers(commit["commit"]["message"]).get(TRAILER_TEMPLATE) == result.new_commit:
            return pr["html_url"]
    return None


def apply_repo(org: str, repo: str, result: Result) -> tuple[Action, str]:
    """Open or update this repo's sync PR. Returns (action, detail: URL or reason)."""
    if result.unsupported_modes:
        raise ValueError(
            "the API commit can't carry these files' modes (executable, symlink or mode "
            f"change): {', '.join(result.unsupported_modes)}; sync this repo by hand"
        )
    repo_path = f"repos/{org}/{repo}"
    pulls = github("GET", f"{repo_path}/pulls?state=all&head={org}:{SYNC_BRANCH}&per_page=100")
    open_pr = next((pr for pr in pulls if pr["state"] == "open"), None)

    head = _branch_head(repo_path, SYNC_BRANCH)
    current = False
    if head is not None:
        trailers = sync_commit_trailers(repo_path, head)
        if trailers is None:
            where = open_pr["html_url"] if open_pr else "no open PR"
            return Action.SKIPPED, (
                f"{SYNC_BRANCH} has a commit sync.py didn't make ({where}); merge or close "
                "that work and delete the branch to sync again"
            )
        current = (
            trailers.get(TRAILER_TEMPLATE) == result.new_commit
            and trailers.get(TRAILER_BASE) == result.base_sha
            and trailers.get(TRAILER_CONTENT) == content_digest(result)
        )

    if open_pr is None:
        declined = _declined(repo_path, pulls, result)
        if declined:
            return Action.SKIPPED, (
                f"a sync PR for this template commit was closed without merging ({declined})"
            )

    if not current:
        push_commit(org, repo, result)
    title = title_for(result)
    body = pr_body(result)
    conflicts = bool(result.conflicted_files)

    if open_pr is not None:
        want_title = CONFLICTS_PREFIX + title if conflicts and not open_pr.get("draft") else title
        if current and open_pr["title"] == want_title and open_pr.get("body") == body:
            return Action.SKIPPED, f"already up to date ({open_pr['html_url']})"
        github(
            "PATCH",
            f"{repo_path}/pulls/{open_pr['number']}",
            {"title": want_title, "body": body},
        )
        return Action.UPDATED, open_pr["html_url"]

    new_pr = {"title": title, "head": SYNC_BRANCH, "base": result.base_branch, "body": body}
    if not conflicts:
        return Action.OPENED, github("POST", f"{repo_path}/pulls", new_pr)["html_url"]
    try:
        pr = github("POST", f"{repo_path}/pulls", {**new_pr, "draft": True})
        return Action.OPENED_DRAFT, pr["html_url"]
    except GitHubError as e:
        if e.status != 422:
            raise
    pr = github("POST", f"{repo_path}/pulls", {**new_pr, "title": CONFLICTS_PREFIX + title})
    return Action.OPENED_CONFLICTS, pr["html_url"]
