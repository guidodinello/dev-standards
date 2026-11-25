"""The Copier template (copier.yml + templates/copier/) as Copier itself sees it.

What init.py renders is tested in test_init.py and test_self_host.py. This file
pins down what `copier update` does to a repo that has its own edits and its own
Dependabot bumps: the facts docs/template-sync.md relies on, so a Copier upgrade
that changes them fails here first.
"""

from pathlib import Path

import pytest
import yaml
from conftest import (
    CACHE_PIN,
    CACHE_PIN_B,
    CACHE_PIN_C,
    CI,
    REPO_ROOT,
    ZIZMOR,
    ZIZMOR_NEXT,
    copy_template,
    edit,
    edit_template,
    git,
)
from copier import run_copy, run_update
from copier.errors import UserMessageError

DELIMITERS = ("[=", "=]", "[%", "%]", "[#", "#]")


@pytest.fixture(scope="module")
def template_dir(tmp_path_factory):
    return copy_template(tmp_path_factory.mktemp("template"))


def test_answers_file_records_answers_but_not_computed_values(template_dir, tmp_path):
    run_copy(str(template_dir), tmp_path, defaults=True, quiet=True)
    answers = yaml.safe_load((tmp_path / ".copier-answers.yml").read_text())
    assert answers["python_version"] == "3.13"
    assert answers["dev_style"] == "groups"
    assert "install_cmd" not in answers


def test_plain_templates_never_contain_the_jinja_delimiters():
    """copier.yml's custom delimiters must not occur in any verbatim template file,
    or turning it into a .jinja one later would render it wrong."""
    for path in (REPO_ROOT / "templates").rglob("*"):
        if path.is_file() and path.suffix != ".jinja":
            text = path.read_text()
            assert not [d for d in DELIMITERS if d in text], path


# ── copier update behaviour ────────────────────────────────────────────────


def update(dst: Path) -> str:
    run_update(dst, defaults=True, overwrite=True, conflict="inline", quiet=True)
    return (dst / CI).read_text()


def has_conflict(text: str) -> bool:
    return "<<<<<<< " in text and ">>>>>>> " in text


def test_update_keeps_local_edit_and_repo_only_pin_bump(project):
    template, dst = project
    edit(dst, CI, "run: uv run pytest\n", 'run: uv run pytest -m "not integration"\n', "local")
    edit(dst, CI, CACHE_PIN, CACHE_PIN_B, "dependabot bump")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)

    text = update(dst)

    assert not has_conflict(text)
    assert ZIZMOR_NEXT in text  # the template change arrived
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
    with pytest.raises(UserMessageError, match="dirty"):
        update(dst)


def test_update_never_signs_its_own_commits(project, monkeypatch):
    """A dev machine signs every commit (commit.gpgsign=true). Copier's internal
    commits pass --no-gpg-sign; with a gpg program that always fails, any signing
    attempt would make the update fail."""
    template, dst = project
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "gpg.program")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
    assert ZIZMOR_NEXT in update(dst)


def test_untagged_template_updates_to_head_and_ignores_non_pep440_tags(project):
    """dev-standards has no tags (and must not get `v*` ones: release.yml fires on
    them). Without a PEP 440 tag Copier updates to HEAD and records `git describe`."""
    template, dst = project
    answers = dst / ".copier-answers.yml"
    assert yaml.safe_load(answers.read_text())["_commit"] == git(
        template, "rev-parse", "--short", "HEAD"
    )

    git(template, "tag", "template-v1")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    assert ZIZMOR_NEXT in update(dst)
    commit = yaml.safe_load(answers.read_text())["_commit"]
    assert commit == f"template-v1-1-g{git(template, 'rev-parse', '--short', 'HEAD')}"


def test_every_skip_if_exists_file_is_excluded_on_update():
    """Copier skips a `_skip_if_exists` file only when it exists, so `copier update`
    would add it to a repo without one. Each such file must also be excluded on
    update; init.py and `sync.py adopt` rely on no sync touching them."""
    config = yaml.safe_load((REPO_ROOT / "copier.yml").read_text())
    for path in config["_skip_if_exists"]:
        assert f"[% if _copier_operation == 'update' %]{path}[% endif %]" in config["_exclude"]
