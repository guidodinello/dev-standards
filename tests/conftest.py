import importlib.util
from functools import cache
from pathlib import Path

import pytest

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
