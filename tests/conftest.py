import base64
import hashlib
import importlib.util
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
from copier import run_copy

import sync
import sync_pr
import sync_self_host
from sync_pr import SYNC_BRANCH, GitHubError

REPO_ROOT = Path(__file__).parent.parent


def import_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init = import_module_from_path("dev_standards_init", REPO_ROOT / "init.py")

# dev-standards' own answers: what its checked-in self-host files are rendered with.
SELF_HOST_ANSWERS = {
    "python_version": "3.13",
    "dev_style": "groups",
    "locked": False,
    "include_tests": True,
    "branch": "main",
    "release": True,
}


@cache
def rendered(**overrides: object) -> dict[str, str]:
    """init.render() over SELF_HOST_ANSWERS + overrides, keyed by repo-relative path.
    Cached: each render clones this repo through Copier. Call it only inside a test,
    so the git identity below is in place."""
    out = Path("/rendered")
    files = init.render(out, {**SELF_HOST_ANSWERS, **overrides})
    return {path.relative_to(out).as_posix(): text for path, text in files.items()}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Copier clones this repo to render it, and commits a dirty working tree into
    that clone; tests also commit in tmp repos. CI has no git identity and no GPG
    key, while a dev machine may sign every commit. Copier's own commits pass
    --no-gpg-sign; this covers the identity and our own commits."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


# ── scratch template + rendered repos, for `copier update` tests ─────────────

CI = Path(".github/workflows/ci.yml")

# Read from the template, never written out: sync_self_host.py rewrites these pins
# on every Dependabot bump, and a literal here would turn the update tests red.
_CI_TEMPLATE = (REPO_ROOT / "templates" / "copier" / f"{CI}.jinja").read_text()
# actions/cache's pin in the lint job, the one pin that appears once in ci.yml.
CACHE_PIN = (
    "actions/cache@"
    + sync_self_host.extract_pins(_CI_TEMPLATE, sync_self_host.ACTION_USES)["actions/cache"]
)
CACHE_PIN_B = "actions/cache@" + "b" * 40 + " # v99.0.0"
CACHE_PIN_C = "actions/cache@" + "c" * 40 + " # v99.1.0"
ZIZMOR = re.search(r"zizmor@\S+", _CI_TEMPLATE).group()
ZIZMOR_NEXT = "zizmor@99.0.0"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def copy_template(dst: Path) -> Path:
    """The template alone, outside this repo's git history, so a render reads the
    working tree (not HEAD) and works in CI's shallow checkout."""
    shutil.copy(REPO_ROOT / "copier.yml", dst / "copier.yml")
    shutil.copytree(REPO_ROOT / "templates" / "copier", dst / "templates" / "copier")
    return dst


@pytest.fixture
def project(tmp_path):
    """(template git repo, destination git repo rendered from it and committed)."""
    (tmp_path / "template").mkdir()
    template = copy_template(tmp_path / "template")
    git(template, "init", "-q")
    commit_all(template, "v1")
    dst = tmp_path / "dst"
    dst.mkdir()
    git(dst, "init", "-q")
    run_copy(str(template), dst, defaults=True, quiet=True)
    commit_all(dst, "render")
    return template, dst


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def edit(repo: Path, rel: Path, old: str, new: str, message: str) -> None:
    path = repo / rel
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))
    commit_all(repo, message)


def edit_template(template: Path, old: str, new: str) -> None:
    edit(template, Path("templates/copier") / f"{CI}.jinja", old, new, "template change")


# ── sync.py: local origins instead of GitHub ────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_github(monkeypatch):
    """gh is authenticated on dev machines: no test may reach GitHub through
    sync_pr's seam unless it installs a fake."""

    def refuse(*args, **kwargs):
        raise AssertionError("test reached the real GitHub seam; install a fake")

    monkeypatch.setattr(sync_pr, "github", refuse)
    monkeypatch.setattr(sync_pr, "token_scopes", refuse)


@pytest.fixture
def origins(monkeypatch):
    """Repo name -> local origin repo; sync.clone_command clones from it."""
    repos = {}

    def clone_command(org, repo, dest):
        return ["git", "clone", *sync.CLONE_FLAGS, f"file://{repos[repo]}", str(dest)]

    monkeypatch.setattr(sync, "clone_command", clone_command)
    return repos


@pytest.fixture
def onboarded(project, origins):
    """(template, origin): a repo rendered from the template, registered as `repo`."""
    template, origin = project
    git(origin, "config", "uploadpack.allowFilter", "true")  # honour --filter=blob:none
    origins["repo"] = origin
    return template, origin


# ── a fake GitHub for --apply (sync_pr.github replaced) ─────────────────────


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
        self.refuse_graphql: set[str] = set()  # mutation names GitHub refuses

    def _pr_by_node(self, node_id: str) -> dict:
        return next(pr for pulls in self.pulls.values() for pr in pulls if pr["node_id"] == node_id)

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
            for mutation, value in (
                ("enablePullRequestAutoMerge", {"merge_method": "squash"}),
                ("disablePullRequestAutoMerge", None),
            ):
                if mutation in payload["query"]:
                    if mutation in self.refuse_graphql:
                        raise GitHubError(None, "auto-merge is not allowed for this repository")
                    pr = self._pr_by_node(payload["variables"]["id"])
                    pr["auto_merge"] = value
                    return {"data": {mutation: {"clientMutationId": None}}}
            if "markPullRequestReadyForReview" in payload["query"]:
                pr = next(
                    pr
                    for pulls in self.pulls.values()
                    for pr in pulls
                    if pr["node_id"] == payload["variables"]["id"]
                )
                pr["draft"] = False
                return {"data": {"markPullRequestReadyForReview": {"clientMutationId": None}}}
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
                "node_id": f"PR_{repo}_{len(pulls) + 1}",
                "state": "open",
                "merged_at": None,
                "draft": bool(payload.get("draft")),
                "auto_merge": None,
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
