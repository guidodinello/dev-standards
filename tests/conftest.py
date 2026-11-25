import importlib.util
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
from copier import run_copy

import sync_self_host

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
