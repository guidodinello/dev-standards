"""sync.py --apply: sync PRs, through sync_pr.github() replaced by an in-memory GitHub.

The sync results are real (scratch git repos, `copier update`); only the GitHub
REST/GraphQL calls are faked. FakeGitHub implements just the endpoints sync_pr
uses and enforces what GitHub would (a missing ref is a 422, createCommitOnBranch
checks expectedHeadOid), so a wrong request fails here.
"""

import base64
import hashlib
import json
import re

import pytest
from conftest import (
    CACHE_PIN,
    CACHE_PIN_B,
    CACHE_PIN_C,
    CI,
    ZIZMOR,
    ZIZMOR_NEXT,
    commit_all,
    edit,
    edit_template,
    git,
    rendered,
)

import sync
import sync_pr
from sync_pr import SCRATCH_BRANCH, SYNC_BRANCH, GitHubError


def _git_blob_sha(contents: bytes) -> str:
    """What GitHub reports as a commit file's `sha`: git's blob id. Computed here,
    not with sync_pr's helper, so the fake doesn't borrow the code under test."""
    return hashlib.sha1(b"blob %d\0" % len(contents) + contents).hexdigest()


class FakeGitHub:
    """The endpoints sync_pr uses, over in-memory refs, commits and PRs. Commits
    made through createCommitOnBranch are GitHub's: committer `web-flow`, verified,
    one parent; `add_commit` stands in for anyone else's push."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.refs: dict[tuple[str, str], str] = {}  # (repo, branch) -> sha
        self.commits: dict[str, dict] = {}  # sha -> {message, parents, committer, ...}
        self.pulls: dict[str, list[dict]] = {}
        self.drafts_supported = True
        self.fail_commit_for: set[str] = set()
        self.fail_once: dict[tuple[str, str], GitHubError] = {}  # (method, path suffix)

    @property
    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [call for call in self.calls if call[0] != "GET"]

    def head(self, repo: str, branch: str = SYNC_BRANCH) -> dict:
        return self.commits[self.refs[(repo, branch)]]

    def add_commit(
        self,
        repo: str,
        branch: str,
        message: str,
        *,
        committer: str = "a-human",
        verified: bool = True,
        parents: list[str] | None = None,
        files: dict[str, str | None] | None = None,
    ) -> str:
        """A commit pushed by someone other than sync.py (by default on top of the
        branch head)."""
        sha = hashlib.sha1(f"{repo}{branch}{message}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {
            "message": message,
            "parents": parents if parents is not None else [self.refs.get((repo, branch))],
            "committer": committer,
            "verified": verified,
            "files": files or {},
        }
        self.refs[(repo, branch)] = sha
        return sha

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        for (fail_method, suffix), error in list(self.fail_once.items()):
            if method == fail_method and path.endswith(suffix):
                del self.fail_once[(fail_method, suffix)]
                raise error
        if path == "graphql":
            return self._create_commit(payload["variables"]["input"])
        _org, repo, rest = re.fullmatch(r"repos/([^/]+)/([^/]+)/(.+)", path).groups()
        if method == "GET" and rest.startswith("pulls?"):
            for pr in self.pulls.get(repo, []):
                if pr["state"] == "open":  # a closed PR keeps the head it closed with
                    pr["head"]["sha"] = self.refs.get((repo, SYNC_BRANCH))
            return self.pulls.get(repo, [])
        if method == "GET" and rest.startswith("commits/"):
            return self._commit_json(rest.removeprefix("commits/"))
        if method == "GET" and rest.startswith("git/ref/heads/"):
            sha = self.refs.get((repo, rest.removeprefix("git/ref/heads/")))
            if sha is None:
                raise GitHubError(404, "Not Found")
            return {"object": {"sha": sha}}
        if rest.startswith("git/refs/heads/"):
            branch = rest.removeprefix("git/refs/heads/")
            if (repo, branch) not in self.refs:
                raise GitHubError(422, "Reference does not exist")
            if method == "DELETE":
                del self.refs[(repo, branch)]
                return None
            self.refs[(repo, branch)] = payload["sha"]
            return {}
        if method == "POST" and rest == "git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            if (repo, branch) in self.refs:
                raise GitHubError(422, "Reference already exists")
            self.refs[(repo, branch)] = payload["sha"]
            return {}
        if method == "POST" and rest == "pulls":
            if payload.get("draft") and not self.drafts_supported:
                raise GitHubError(422, "Validation Failed")
            pulls = self.pulls.setdefault(repo, [])
            pr = {
                "number": len(pulls) + 1,
                "state": "open",
                "merged_at": None,
                "draft": bool(payload.get("draft")),
                "title": payload["title"],
                "body": payload["body"],
                "base": {"ref": payload["base"]},
                "head": {"ref": payload["head"], "sha": self.refs.get((repo, payload["head"]))},
                "html_url": f"https://github.test/{repo}/pull/{len(pulls) + 1}",
            }
            pulls.append(pr)
            return pr
        if method == "PATCH" and rest.startswith("pulls/"):
            pr = self.pulls[repo][int(rest.removeprefix("pulls/")) - 1]
            pr.update(payload)
            return pr
        raise AssertionError(f"unexpected call {method} {path}")

    def _commit_json(self, sha):
        commit = self.commits[sha]
        return {
            "sha": sha,
            "parents": [{"sha": parent} for parent in commit["parents"]],
            "committer": {"login": commit["committer"]},
            "commit": {
                "message": commit["message"],
                "verification": {"verified": commit["verified"]},
            },
            "files": [
                {"filename": path, "status": "removed", "sha": None}
                if contents is None
                else {
                    "filename": path,
                    "status": "modified",
                    "sha": _git_blob_sha(contents.encode()),
                }
                for path, contents in commit["files"].items()
            ],
        }

    def _create_commit(self, data):
        repo = data["branch"]["repositoryNameWithOwner"].split("/")[1]
        branch = data["branch"]["branchName"]
        if repo in self.fail_commit_for:
            raise GitHubError(502, "Bad Gateway")
        if self.refs.get((repo, branch)) != data["expectedHeadOid"]:
            raise GitHubError(None, "expectedHeadOid does not match the branch head")
        files = {
            a["path"]: base64.b64decode(a["contents"]).decode()
            for a in data["fileChanges"]["additions"]
        } | {d["path"]: None for d in data["fileChanges"]["deletions"]}
        message = data["message"]["headline"] + "\n\n" + data["message"]["body"]
        return {
            "data": {
                "createCommitOnBranch": {
                    "commit": {
                        "oid": self.add_commit(
                            repo,
                            branch,
                            message,
                            committer="web-flow",
                            parents=[data["expectedHeadOid"]],
                            files=files,
                        )
                    }
                }
            }
        }


@pytest.fixture
def fake(monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(sync_pr, "github", gh)
    monkeypatch.setattr(sync_pr, "token_scopes", lambda: {"repo", "workflow"})
    return gh


@pytest.fixture
def config(tmp_path):
    def write(*repos):
        path = tmp_path / "github-standard.json"
        path.write_text(json.dumps({"org": "org", "repos": {name: {} for name in repos}}))
        return str(path)

    return write


def apply(config_path, capsys):
    code = sync.main(["--apply", "--config", config_path])
    return code, capsys.readouterr().out


def the_pr(fake, repo="repo"):
    (pr,) = fake.pulls[repo]
    return pr


def test_clean_change_opens_a_signed_pr_on_the_default_branch(onboarded, fake, config, capsys):
    template, origin = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    base_sha = git(origin, "rev-parse", "HEAD")
    old_commit = git(template, "rev-parse", "--short", "HEAD~1")
    new_commit = git(template, "rev-parse", "--short", "HEAD")

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert "APPLY" in out
    pr = the_pr(fake)
    assert pr["base"]["ref"] == git(origin, "symbolic-ref", "--short", "HEAD")
    assert pr["head"]["ref"] == SYNC_BRANCH
    assert not pr["draft"]
    assert pr["title"] == f"chore(dev-standards): sync templates to {new_commit}"
    assert f"`{old_commit}`..`{new_commit}`" in pr["body"]
    assert "guidodinello/dev-standards#53" in pr["body"]
    assert f"- `{CI}`" in pr["body"]
    assert f"apply: opened PR: {pr['html_url']}" in out
    # One GitHub-signed commit on top of the default branch, carrying the trailers.
    (commit_call,) = (c for c in fake.calls if c[1] == "graphql")
    assert commit_call[2]["variables"]["input"]["expectedHeadOid"] == base_sha
    commit = fake.commits[fake.refs[("repo", SYNC_BRANCH)]]
    assert commit["parents"] == [base_sha]
    assert ZIZMOR_NEXT in commit["files"][str(CI)]
    assert f"Dev-Standards-Template: {new_commit}" in commit["message"]
    assert f"Dev-Standards-Base: {base_sha}" in commit["message"]
    assert ("repo", SCRATCH_BRANCH) not in fake.refs


def test_conflicts_open_a_draft_listing_the_conflicted_files(onboarded, fake, config, capsys):
    template, origin = onboarded
    edit(origin, CI, "--format=github .github", "--format=plain .github", "local edit")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)

    _, out = apply(config("repo"), capsys)

    pr = the_pr(fake)
    assert pr["draft"]
    assert "do not merge as is" in pr["body"]
    assert f"- `{CI}`" in pr["body"].split("### Conflicts")[1]
    assert "apply: opened draft PR" in out
    assert (
        "<<<<<<< before updating"
        in fake.commits[fake.refs[("repo", SYNC_BRANCH)]]["files"][str(CI)]
    )


def test_without_draft_prs_conflicts_get_a_flagged_normal_pr(onboarded, fake, config, capsys):
    """GitHub Free has no draft PRs on private repos."""
    template, origin = onboarded
    fake.drafts_supported = False
    edit(origin, CI, "--format=github .github", "--format=plain .github", "local edit")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)

    code, out = apply(config("repo"), capsys)

    assert code == 0
    pr = the_pr(fake)
    assert not pr["draft"]
    assert pr["title"].startswith("[conflicts] chore(dev-standards): sync templates to ")
    assert "apply: opened [conflicts] PR" in out


def test_resolved_pins_are_listed_in_the_body(onboarded, fake, config, capsys):
    template, origin = onboarded
    edit(origin, CI, CACHE_PIN, CACHE_PIN_B, "dependabot bump")
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)  # so ci.yml changes beyond the pin

    apply(config("repo"), capsys)

    body = the_pr(fake)["body"]
    assert "### Pin conflicts resolved to the newer version" in body
    kept = CACHE_PIN_C.split("@")[1]
    assert f"`actions/cache`: repo `{CACHE_PIN_B.split('@')[1]}`" in body
    assert f"→ kept `{kept}`" in body


@pytest.mark.parametrize("template_change", ["none", "answers-only"])
def test_unchanged_and_answers_only_repos_never_touch_github(
    onboarded, fake, config, capsys, template_change
):
    """Nothing to sync, or only `_commit` moved (a template commit that renders
    the same files): no PR, and no GitHub call at all."""
    template, _ = onboarded
    if template_change == "answers-only":
        edit(template, "copier.yml", "# Copier template", "# The Copier template", "comment")

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert fake.calls == []
    assert ("[unchanged] repo" if template_change == "none" else "answers only") in out
    assert "sync PRs: nothing to do" in out


def test_rerun_with_the_same_result_writes_nothing(onboarded, fake, config, capsys):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    writes_before = len(fake.writes)

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert len(fake.writes) == writes_before
    assert len(fake.pulls["repo"]) == 1
    assert "apply: skipped: already up to date" in out


def test_rerun_after_the_template_moved_updates_the_same_pr(onboarded, fake, config, capsys):
    template, origin = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    new_commit = git(template, "rev-parse", "--short", "HEAD")

    _, out = apply(config("repo"), capsys)

    pr = the_pr(fake)
    assert pr["title"].endswith(new_commit)
    assert "apply: updated PR" in out
    commit = fake.commits[fake.refs[("repo", SYNC_BRANCH)]]
    assert commit["parents"] == [git(origin, "rev-parse", "HEAD")]  # rebuilt, not stacked
    assert CACHE_PIN_C in commit["files"][str(CI)] and ZIZMOR_NEXT in commit["files"][str(CI)]


def test_a_human_commit_on_the_sync_branch_is_never_overwritten(onboarded, fake, config, capsys):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    fake.add_commit("repo", SYNC_BRANCH, "fix: resolve the sync conflicts")
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    writes_before = len(fake.writes)

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert len(fake.writes) == writes_before
    assert f"{SYNC_BRANCH} has a commit sync.py didn't make" in out


def test_a_human_commit_on_a_branch_without_an_open_pr_is_never_overwritten(
    onboarded, fake, config, capsys
):
    """Someone pushes a conflict fix, closes the PR unmerged keeping the branch, and
    the template moves: the next run must not reset the branch."""
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    fake.add_commit("repo", SYNC_BRANCH, "fix: resolve the sync conflicts")
    the_pr(fake)["state"] = "closed"
    human_head = fake.refs[("repo", SYNC_BRANCH)]
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    writes_before = len(fake.writes)

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert len(fake.writes) == writes_before
    assert fake.refs[("repo", SYNC_BRANCH)] == human_head
    assert f"{SYNC_BRANCH} has a commit sync.py didn't make (no open PR)" in out


def _replace_head(fake, *, committer="a-human", verified=True, files=None, parents=None):
    """Swap the sync commit for one with the same message (an amend keeps trailers)."""
    ours = fake.head("repo")
    fake.add_commit(
        "repo",
        SYNC_BRANCH,
        ours["message"],
        committer=committer,
        verified=verified,
        parents=parents if parents is not None else ours["parents"],
        files=files if files is not None else {**ours["files"], str(CI): "resolved by hand\n"},
    )


@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param({}, id="amended-locally"),  # human committer, trailers kept
        pytest.param(
            {"committer": "web-flow", "files": {str(CI): "edited\n"}},
            id="github-made-different-content",
        ),
        pytest.param({"committer": "web-flow", "verified": False}, id="unverified"),
    ],
)
def test_a_commit_with_copied_trailers_is_not_taken_for_ours(
    onboarded, fake, config, capsys, replacement
):
    """Trailers alone aren't proof: the head must be GitHub-made, verified, on the
    base commit, with exactly the content the trailers describe."""
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    _replace_head(fake, **replacement)
    head = fake.refs[("repo", SYNC_BRANCH)]
    edit_template(template, CACHE_PIN, CACHE_PIN_C)

    _, out = apply(config("repo"), capsys)

    assert fake.refs[("repo", SYNC_BRANCH)] == head
    assert f"{SYNC_BRANCH} has a commit sync.py didn't make" in out


def test_our_own_commit_without_a_pr_is_reused(onboarded, fake, config, capsys):
    """A run that committed but failed to open the PR: the next run opens it without
    a second commit."""
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    fake.fail_once[("POST", "/pulls")] = GitHubError(502, "Bad Gateway")
    assert apply(config("repo"), capsys)[0] == 1
    head = fake.refs[("repo", SYNC_BRANCH)]

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert fake.refs[("repo", SYNC_BRANCH)] == head  # no new commit
    assert the_pr(fake)["state"] == "open"
    assert "apply: opened PR" in out


# ── fix 2: a failed PR update is repaired by the next run ──


def test_a_failed_pr_update_is_repaired_by_the_next_run(onboarded, fake, config, capsys):
    """The branch moved but PATCHing the PR failed: the next run must not call it
    "already up to date" while the PR shows the old title and body."""
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    new_commit = git(template, "rev-parse", "--short", "HEAD")
    fake.fail_once[("PATCH", "/pulls/1")] = GitHubError(502, "Bad Gateway")
    assert apply(config("repo"), capsys)[0] == 1
    assert not the_pr(fake)["title"].endswith(new_commit)
    commits_before = len(fake.commits)

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert the_pr(fake)["title"].endswith(new_commit)
    assert new_commit in the_pr(fake)["body"]
    assert len(fake.commits) == commits_before  # the branch was already right
    assert "apply: updated PR" in out


def test_a_failed_scratch_cleanup_is_not_a_failure(onboarded, fake, config, capsys):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    fake.fail_once[("DELETE", f"/git/refs/heads/{SCRATCH_BRANCH}")] = GitHubError(502, "x")

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert "apply: opened PR" in out


def test_a_declined_sync_for_the_same_template_commit_is_not_reopened(
    onboarded, fake, config, capsys
):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    the_pr(fake)["state"] = "closed"
    the_pr(fake)["title"] = "chore: renamed by a human"  # matched by trailer, not title

    _, out = apply(config("repo"), capsys)

    assert len(fake.pulls["repo"]) == 1
    assert "closed without merging" in out


def test_a_declined_older_sync_does_not_block_a_newer_template_commit(
    onboarded, fake, config, capsys
):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    the_pr(fake)["state"] = "closed"
    edit_template(template, CACHE_PIN, CACHE_PIN_C)

    _, out = apply(config("repo"), capsys)

    assert [pr["state"] for pr in fake.pulls["repo"]] == ["closed", "open"]
    assert "apply: opened PR" in out


def test_a_failure_in_one_repo_does_not_stop_the_others(
    onboarded, origins, fake, config, capsys, tmp_path
):
    template, origin = onboarded
    (tmp_path / "healthy").mkdir()
    git(tmp_path / "healthy", "clone", "-q", str(origin), ".")
    origins["healthy"] = tmp_path / "healthy"
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    fake.fail_commit_for.add("repo")

    code, out = apply(config("repo", "healthy"), capsys)

    assert code == 1
    assert "apply: failed: GitHubError: HTTP 502: Bad Gateway" in out
    assert len(fake.pulls["healthy"]) == 1
    assert "sync PRs: 1 failed, 1 opened PR" in out


def test_the_dry_run_never_calls_github(onboarded, fake, config, capsys):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    assert sync.main(["--config", config("repo")]) == 0
    assert fake.calls == []
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_without_the_workflow_scope_stops_before_any_write(
    onboarded, fake, config, capsys, monkeypatch
):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    monkeypatch.setattr(sync_pr, "token_scopes", lambda: {"repo"})

    assert sync.main(["--apply", "--config", config("repo")]) == 1
    assert fake.calls == []
    assert "gh auth refresh -s workflow" in capsys.readouterr().err


def test_apply_stops_when_the_scope_check_itself_fails(
    onboarded, fake, config, capsys, monkeypatch
):
    """Fail closed: a scope lookup that can't complete is not a pass."""
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)

    def lookup_failed():
        raise GitHubError(None, "couldn't read gh's token scopes: HTTP 502")

    monkeypatch.setattr(sync_pr, "token_scopes", lookup_failed)

    assert sync.main(["--apply", "--config", config("repo")]) == 1
    assert fake.calls == []
    assert "Stopping before any write" in capsys.readouterr().err


def test_the_compare_link_comes_from_the_template_url(onboarded):
    result = sync.Result(
        "repo",
        sync.Status.CLEAN,
        old_commit="aaaaaaa",
        new_commit="bbbbbbb",
        template_url="https://github.com/guidodinello/dev-standards.git",
    )
    assert (
        "(https://github.com/guidodinello/dev-standards/compare/aaaaaaa...bbbbbbb)"
        in sync_pr.pr_body(result)
    )


@pytest.mark.parametrize(
    "message", ["Draft pull requests are not supported in this repository.", "Validation Failed"]
)
def test_any_422_on_the_draft_falls_back_to_a_conflicts_pr(
    onboarded, fake, config, capsys, message
):
    """Whatever GitHub's wording, a refused draft still gets a (flagged) PR."""
    template, origin = onboarded
    edit(origin, CI, "--format=github .github", "--format=plain .github", "local edit")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    fake.fail_once[("POST", "/pulls")] = GitHubError(422, message)

    code, out = apply(config("repo"), capsys)

    assert code == 0
    assert the_pr(fake)["title"].startswith("[conflicts] ")
    assert "apply: opened [conflicts] PR" in out


def test_a_draft_refused_with_a_non_422_is_a_failure(onboarded, fake, config, capsys):
    template, origin = onboarded
    edit(origin, CI, "--format=github .github", "--format=plain .github", "local edit")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    fake.fail_once[("POST", "/pulls")] = GitHubError(500, "Server Error")

    code, out = apply(config("repo"), capsys)

    assert code == 1
    assert "apply: failed: GitHubError: HTTP 500: Server Error" in out
    assert "repo" not in fake.pulls


# ── fix 5: file modes ──


def test_a_new_executable_is_reported_by_the_dry_run_and_refused_by_apply(
    onboarded, fake, config, capsys
):
    template, _ = onboarded
    script = template / "templates" / "copier" / "bin" / "check.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    commit_all(template, "add a script")

    assert sync.main(["--config", config("repo")]) == 0  # the dry run only reports
    out = capsys.readouterr().out
    assert "[clean] repo" in out
    assert "file mode the API commit can't carry (--apply refuses this repo): bin/check.sh" in out

    code, out = apply(config("repo"), capsys)
    assert code == 1
    assert "apply: failed: ValueError" in out
    assert fake.calls == []


# ── fix 3: conflict markers fail CI in any file ──


def test_the_rendered_pre_commit_config_fails_on_conflict_markers_anywhere():
    config = rendered()[".pre-commit-config.yaml"]
    hook = config[config.index("- id: check-merge-conflict") :].split("\n- ")[0]
    assert "args: [--assume-in-merge]" in hook  # outside a git merge it checks nothing
