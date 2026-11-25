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
    edit,
    edit_template,
    git,
)

import sync
import sync_pr
from sync_pr import SCRATCH_BRANCH, SYNC_BRANCH, GitHubError


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.refs: dict[tuple[str, str], str] = {}  # (repo, branch) -> sha
        self.commits: dict[str, dict] = {}  # sha -> {message, parent, files}
        self.pulls: dict[str, list[dict]] = {}
        self.drafts_supported = True
        self.fail_commit_for: set[str] = set()

    @property
    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [call for call in self.calls if call[0] != "GET"]

    def add_commit(self, repo: str, branch: str, message: str) -> str:
        """A commit pushed by someone other than sync.py."""
        sha = hashlib.sha1(f"{repo}{branch}{message}".encode()).hexdigest()
        self.commits[sha] = {"message": message, "parent": self.refs.get((repo, branch))}
        self.refs[(repo, branch)] = sha
        return sha

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "graphql":
            return self._create_commit(payload["variables"]["input"])
        _org, repo, rest = re.fullmatch(r"repos/([^/]+)/([^/]+)/(.+)", path).groups()
        if method == "GET" and rest.startswith("pulls?"):
            for pr in self.pulls.get(repo, []):
                pr["head"]["sha"] = self.refs.get((repo, SYNC_BRANCH))
            return self.pulls.get(repo, [])
        if method == "GET" and rest.startswith("commits/"):
            return {"commit": {"message": self.commits[rest.removeprefix("commits/")]["message"]}}
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
                raise GitHubError(422, "Draft pull requests are not supported in this repository.")
            pulls = self.pulls.setdefault(repo, [])
            pr = {
                "number": len(pulls) + 1,
                "state": "open",
                "merged_at": None,
                "draft": bool(payload.get("draft")),
                "title": payload["title"],
                "body": payload["body"],
                "base": {"ref": payload["base"]},
                "head": {"ref": payload["head"], "sha": None},
                "html_url": f"https://github.test/{repo}/pull/{len(pulls) + 1}",
            }
            pulls.append(pr)
            return pr
        if method == "PATCH" and rest.startswith("pulls/"):
            pr = self.pulls[repo][int(rest.removeprefix("pulls/")) - 1]
            pr.update(payload)
            return pr
        raise AssertionError(f"unexpected call {method} {path}")

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
        }
        message = data["message"]["headline"] + "\n\n" + data["message"]["body"]
        sha = hashlib.sha1(json.dumps([data, len(self.commits)]).encode()).hexdigest()
        self.commits[sha] = {"message": message, "parent": data["expectedHeadOid"], "files": files}
        self.refs[(repo, branch)] = sha
        return {"data": {"createCommitOnBranch": {"commit": {"oid": sha}}}}


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
    assert commit["parent"] == base_sha
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
    assert commit["parent"] == git(origin, "rev-parse", "HEAD")  # rebuilt, not stacked
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


def test_a_leftover_branch_without_a_pr_is_reused(onboarded, fake, config, capsys):
    """A run that pushed the commit but died before opening the PR."""
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    fake.refs[("repo", SYNC_BRANCH)] = "0" * 40
    fake.refs[("repo", SCRATCH_BRANCH)] = "1" * 40

    code, _ = apply(config("repo"), capsys)

    assert code == 0
    assert the_pr(fake)["state"] == "open"
    assert ("repo", SCRATCH_BRANCH) not in fake.refs


def test_a_declined_sync_for_the_same_template_commit_is_not_reopened(
    onboarded, fake, config, capsys
):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    apply(config("repo"), capsys)
    the_pr(fake)["state"] = "closed"

    _, out = apply(config("repo"), capsys)

    assert len(fake.pulls["repo"]) == 1
    assert "closed without merging" in out


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


def test_a_draft_refused_for_another_reason_is_a_failure(
    onboarded, fake, config, capsys, monkeypatch
):
    """Only GitHub's "drafts not supported" refusal falls back to a [conflicts] PR;
    any other 422 on opening the PR is that repo's failure."""
    template, origin = onboarded
    edit(origin, CI, "--format=github .github", "--format=plain .github", "local edit")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)

    def refuse_pulls(method, path, payload=None):
        if method == "POST" and path.endswith("/pulls"):
            fake.calls.append((method, path, payload))
            raise GitHubError(422, "A pull request already exists")
        return fake(method, path, payload)

    monkeypatch.setattr(sync_pr, "github", refuse_pulls)
    code, out = apply(config("repo"), capsys)

    assert code == 1
    assert "apply: failed: GitHubError: HTTP 422: A pull request already exists" in out
    assert len([c for c in fake.calls if c[0] == "POST" and c[1].endswith("/pulls")]) == 1
