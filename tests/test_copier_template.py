"""The Copier template (copier.yml + templates/copier/, issue #53 spike).

Two jobs:
- Parity: while init.py still renders by string substitution, the Copier render
  must be byte-equal to it, so every guard in test_init.py (SHA pins,
  persist-credentials, CI_RUNNERS, the aggregate job) covers both.
- Update behaviour: what `copier update` does to a repo that has its own edits
  and its own Dependabot bumps. These pin down the facts docs/template-sync.md
  relies on, so a Copier upgrade that changes them fails here first.
"""

import itertools
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, import_module_from_path
from copier import run_copy, run_update

init = import_module_from_path("dev_standards_init", REPO_ROOT / "init.py")

CI = Path(".github/workflows/ci.yml")
PRE_COMMIT = Path(".pre-commit-config.yaml")
DELIMITERS = ("[=", "=]", "[%", "%]", "[#", "#]")

# actions/cache's pin in the lint job, the one pin that appears once in ci.yml.
CACHE_PIN = "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0"
CACHE_PIN_B = "actions/cache@" + "b" * 40 + " # v6.2.0"
CACHE_PIN_C = "actions/cache@" + "c" * 40 + " # v6.3.0"
ZIZMOR = "zizmor@1.31.1"


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """CI has no git identity and no GPG key, while a dev machine may sign every
    commit. Copier's own internal commits pass --no-gpg-sign; these cover ours."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


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


# ── parity with init.py ────────────────────────────────────────────────────

INSTALL_STYLES = {  # (dev_style, locked) -> what init.install_cmd_for returns for it
    ("groups", False): init.install_cmd_for("groups", locked=False),
    ("groups", True): init.install_cmd_for("groups", locked=True),
    ("optional", False): init.install_cmd_for("optional", locked=False),
}
MATRIX = list(itertools.product(["3.11", "3.13"], INSTALL_STYLES, [True, False], ["main", "dev"]))


@pytest.fixture(scope="module")
def template_dir(tmp_path_factory):
    return copy_template(tmp_path_factory.mktemp("template"))


@pytest.mark.parametrize(("version", "style", "include_tests", "branch"), MATRIX)
def test_copier_render_matches_init_render(
    template_dir, tmp_path, version, style, include_tests, branch
):
    dev_style, locked = style
    run_copy(
        str(template_dir),
        tmp_path,
        data={
            "python_version": version,
            "dev_style": dev_style,
            "locked": locked,
            "include_tests": include_tests,
            "branch": branch,
        },
        defaults=True,
        quiet=True,
    )
    assert (tmp_path / CI).read_text() == init.render_ci(
        version=version,
        install_cmd=INSTALL_STYLES[style],
        branch=branch,
        include_tests=include_tests,
    )
    assert (tmp_path / PRE_COMMIT).read_text() == init.render_pre_commit(version)


def test_answers_file_records_answers_but_not_computed_values(template_dir, tmp_path):
    run_copy(str(template_dir), tmp_path, defaults=True, quiet=True)
    answers = yaml.safe_load((tmp_path / ".copier-answers.yml").read_text())
    assert answers["python_version"] == "3.13"
    assert answers["dev_style"] == "groups"
    assert "install_cmd" not in answers


def test_plain_templates_never_contain_the_jinja_delimiters():
    """copier.yml's custom delimiters must not occur in any template file, or a
    file moved into templates/copier/ would render wrong. Covers every file under
    templates/, including the ones that join Copier later (release, automerge)."""
    for path in (REPO_ROOT / "templates").rglob("*"):
        if path.is_file() and path.suffix != ".jinja":
            text = path.read_text()
            assert not [d for d in DELIMITERS if d in text], path


# ── copier update behaviour ────────────────────────────────────────────────


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


def update(dst: Path) -> str:
    run_update(dst, defaults=True, overwrite=True, conflict="inline", quiet=True)
    return (dst / CI).read_text()


def has_conflict(text: str) -> bool:
    return "<<<<<<< " in text and ">>>>>>> " in text


def test_update_keeps_local_edit_and_repo_only_pin_bump(project):
    template, dst = project
    edit(dst, CI, "run: uv run pytest\n", 'run: uv run pytest -m "not integration"\n', "local")
    edit(dst, CI, CACHE_PIN, CACHE_PIN_B, "dependabot bump")
    edit_template(template, ZIZMOR, "zizmor@1.32.0")

    text = update(dst)

    assert not has_conflict(text)
    assert "zizmor@1.32.0" in text  # the template change arrived
    assert 'uv run pytest -m "not integration"' in text  # the local edit survived
    assert CACHE_PIN_B in text  # the repo's newer pin was not rolled back


def test_update_conflicts_when_both_sides_bumped_a_pin_differently(project):
    template, dst = project
    edit(dst, CI, CACHE_PIN, CACHE_PIN_B, "dependabot bump")
    edit_template(template, CACHE_PIN, CACHE_PIN_C)

    text = update(dst)

    assert has_conflict(text)
    assert CACHE_PIN_B in text and CACHE_PIN_C in text


def test_update_is_clean_when_both_sides_bumped_a_pin_to_the_same_version(project):
    template, dst = project
    edit(dst, CI, CACHE_PIN, CACHE_PIN_B, "dependabot bump")
    edit_template(template, CACHE_PIN, CACHE_PIN_B)

    text = update(dst)

    assert not has_conflict(text)
    assert text.count(CACHE_PIN_B) == 1


def test_update_refuses_a_dirty_destination(project):
    _, dst = project
    (dst / CI).write_text("uncommitted\n")
    with pytest.raises(Exception, match="dirty"):
        update(dst)


def test_untagged_template_updates_to_head_and_ignores_non_pep440_tags(project):
    """dev-standards has no tags (and must not get `v*` ones: release.yml fires on
    them). Without a PEP 440 tag Copier updates to HEAD and records `git describe`."""
    template, dst = project
    answers = dst / ".copier-answers.yml"
    assert yaml.safe_load(answers.read_text())["_commit"] == git(
        template, "rev-parse", "--short", "HEAD"
    )

    git(template, "tag", "template-v1")
    edit_template(template, ZIZMOR, "zizmor@1.32.0")
    assert "zizmor@1.32.0" in update(dst)
    commit = yaml.safe_load(answers.read_text())["_commit"]
    assert commit == f"template-v1-1-g{git(template, 'rev-parse', '--short', 'HEAD')}"
