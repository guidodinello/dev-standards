"""Keeps this repo's own checked-in config files honest against templates/.

This repo bootstraps ITSELF from its own templates (see README § self-hosting).
Without this test, Dependabot bumping a pin in the root .pre-commit-config.yaml
(or ci.yml) would silently diverge from templates/ — the exact "possibly-outdated
pinned versions baked into templates/" problem this baseline was meant to fix,
just relocated. When this test goes red, propagate the bump into templates/ too.
"""

from conftest import REPO_ROOT, import_module_from_path

init = import_module_from_path("dev_standards_init", REPO_ROOT / "init.py")


def test_pre_commit_config_matches_template():
    checked_in = (REPO_ROOT / ".pre-commit-config.yaml").read_text()
    assert checked_in == init.render_pre_commit("3.13")


def test_ci_workflow_matches_template():
    checked_in = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert checked_in == init.render_ci(
        install_cmd="uv sync --dev", branch="main", include_tests=True
    )


def test_dependabot_config_matches_template():
    checked_in = (REPO_ROOT / ".github" / "dependabot.yml").read_text()
    assert checked_in == init.render_dependabot()


def test_dependabot_automerge_matches_template():
    checked_in = (REPO_ROOT / ".github" / "workflows" / "dependabot-automerge.yml").read_text()
    assert checked_in == init.render_automerge()
