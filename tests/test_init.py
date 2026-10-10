import re

import pytest
from conftest import REPO_ROOT, import_module_from_path

init = import_module_from_path("dev_standards_init", REPO_ROOT / "init.py")


def test_detect_python_version_takes_lower_bound_regardless_of_clause_order():
    assert init.detect_python_version({"project": {"requires-python": ">=3.11,<4.0"}}) == "3.11"
    assert init.detect_python_version({"project": {"requires-python": "<4.0,>=3.11"}}) == "3.11"


def test_dep_name_strips_version_specifier_and_extras():
    assert init._dep_name("pytest-cov>=4.0") == "pytest-cov"
    assert init._dep_name("ruff[extra]>=0.13; python_version>='3.11'") == "ruff"


def test_detect_dev_style_prefers_dependency_groups_when_both_present():
    pyproject = {
        "dependency-groups": {"dev": ["pytest"]},
        "project": {"optional-dependencies": {"dev": ["pytest"]}},
    }
    assert init.detect_dev_style(pyproject) == "groups"


def test_install_cmd_for_groups_vs_optional():
    assert init.install_cmd_for("groups", locked=False) == "uv sync --dev"
    assert init.install_cmd_for("groups", locked=True) == "uv sync --locked --dev"
    assert init.install_cmd_for("optional", locked=False) == 'uv venv && uv pip install -e ".[dev]"'


def test_detect_tests_requires_both_dir_and_pytest_dep_under_same_style(tmp_path):
    pyproject = {"dependency-groups": {"dev": ["pytest>=8.0"]}}
    (tmp_path / "tests").mkdir()
    assert init.detect_tests(tmp_path, pyproject, "groups") is True
    assert init.detect_tests(tmp_path, pyproject, "optional") is False


def test_render_ci_fills_fixmes_and_strips_test_job_when_excluded():
    rendered = init.render_ci(install_cmd="uv sync --dev", branch="main", include_tests=False)
    assert "# <FIXME>" not in rendered
    assert "test-python" not in rendered
    assert "branches: [main]" in rendered


def test_render_ci_keeps_test_job_when_included():
    rendered = init.render_ci(install_cmd="uv sync --dev", branch="develop", include_tests=True)
    assert "# <FIXME>" not in rendered
    assert "test-python" in rendered
    assert "branches: [develop]" in rendered


def test_render_ci_keeps_ci_runners_switch():
    """Guards against silently dropping the CI_RUNNERS fallback expression during a
    future template edit — without it, every repo rendered from this template loses
    its ability to route CI off GitHub-hosted runners (see README § CI runners)."""
    rendered = init.render_ci(install_cmd="uv sync --dev", branch="main", include_tests=True)
    assert rendered.count("fromJSON(vars.CI_RUNNERS") == 3


def test_render_automerge_keeps_ci_runners_switch():
    """Same guard as test_render_ci_keeps_ci_runners_switch, for the auto-merge
    template: without it, auto-merge stays pinned to ubuntu-latest and keeps
    failing on private repos where GitHub-hosted minutes are billing-blocked
    (see README § CI runners)."""
    rendered = init.render_automerge()
    assert rendered.count("fromJSON(vars.CI_RUNNERS") == 1


def test_render_pre_commit_substitutes_python_version():
    rendered = init.render_pre_commit("3.11")
    assert "python3.11" in rendered
    assert "--py311-plus" in rendered
    assert "python3.13" not in rendered


def test_main_requires_repo_argument(monkeypatch):
    monkeypatch.setattr("sys.argv", ["init.py"])
    with pytest.raises(SystemExit):
        init.main()


def _ci_needs(rendered: str) -> list[str]:
    block = rendered.split("\n  ci:\n")[1]
    needs = block.split("    needs:\n")[1].split("    if:")[0]
    return [line.strip()[2:] for line in needs.splitlines()]


def test_render_ci_aggregate_job_needs_every_job():
    rendered = init.render_ci(install_cmd="uv sync --dev", branch="main", include_tests=True)
    assert _ci_needs(rendered) == ["lint-python", "test-python"]
    assert "if: always()" in rendered


def test_render_ci_without_tests_keeps_aggregate_job_without_dangling_need():
    rendered = init.render_ci(install_cmd="uv sync --dev", branch="main", include_tests=False)
    assert _ci_needs(rendered) == ["lint-python"]
    assert "test-python" not in rendered
    assert rendered.count("fromJSON(vars.CI_RUNNERS") == 2


AGGREGATE_WF = (REPO_ROOT / "templates" / "ci" / "python-ci.yml").read_text()


def test_has_aggregate_ci_job():
    assert init.has_aggregate_ci_job(AGGREGATE_WF) is True
    assert init.has_aggregate_ci_job(None) is False
    no_always = AGGREGATE_WF.replace("    if: always()\n", "")
    assert init.has_aggregate_ci_job(no_always) is False


def test_checks_snippet_aggregate_pins_integration_id_from_app():
    runs = [{"name": "Lint Python"}, {"name": "CI", "app": {"id": 15368}}]
    checks, hint = init.checks_snippet(runs, AGGREGATE_WF)
    assert checks == [{"context": "CI", "integration_id": 15368}]
    assert hint is None


def test_checks_snippet_falls_back_with_hint_without_aggregate_job():
    runs = [{"name": "Tests (Python)"}, {"name": "Lint Python"}]
    checks, hint = init.checks_snippet(runs, None)
    assert checks == [{"context": "Lint Python"}, {"context": "Tests (Python)"}]
    assert hint is not None and "aggregate" in hint


def test_checks_snippet_bare_ci_check_without_aggregate_workflow_is_not_pinned():
    checks, hint = init.checks_snippet([{"name": "CI", "app": {"id": 15368}}], "jobs:\n")
    assert checks == [{"context": "CI"}]
    assert hint is not None


def test_checks_snippet_refuses_to_pin_foreign_app():
    checks, hint = init.checks_snippet([{"name": "CI", "app": {"id": 1}}], AGGREGATE_WF)
    assert checks == [{"context": "CI"}]
    assert hint is not None and "not GitHub Actions" in hint


def _rendered(tmp_path, *, release):
    return init.rendered_files(
        tmp_path,
        version="3.13",
        install_cmd="uv sync --dev",
        branch="main",
        include_tests=True,
        release=release,
    )


def test_release_workflow_is_opt_in(tmp_path):
    release_path = tmp_path / ".github" / "workflows" / "release.yml"
    assert release_path not in _rendered(tmp_path, release=False)
    assert _rendered(tmp_path, release=True)[release_path] == init.render_release()


def test_release_template_is_least_privilege_and_tag_triggered():
    text = init.render_release()
    assert 'tags: ["v*"]' in text
    assert (
        text.count("\n      contents: write\n") == 1
    )  # one job; top level stays `permissions: {}`
    assert "\npermissions: {}\n" in text
    assert text.count("fromJSON(vars.CI_RUNNERS") == 1


def test_release_template_gates_publish_on_verification():
    text = init.render_release()
    # verification and notes extraction both precede the build and the release
    order = [
        text.index("Verify tag, version, and branch"),
        text.index("Extract release notes from CHANGELOG.md"),
        text.index("Build artifacts"),
        text.index("gh release create"),
    ]
    assert order == sorted(order)
    assert "--verify-tag" in text


def test_release_template_pins_every_action_to_a_sha():
    uses = [
        line.strip()
        for line in init.render_release().splitlines()
        if "uses:" in line and not line.lstrip().startswith("#")
    ]
    assert uses
    for line in uses:
        assert re.fullmatch(r"- uses: \S+@[0-9a-f]{40} # v\d.*", line), line


def test_release_checkout_does_not_persist_credentials():
    text = init.render_release()
    step = text[text.index("actions/checkout@") :].split("\n\n")[0]
    assert "persist-credentials: false" in step
    assert "fetch-depth: 0" in step  # the on-default-branch check needs full history


def test_release_flag_parses(monkeypatch):
    monkeypatch.setattr("sys.argv", ["init.py", "x", "--release"])
    seen = {}
    monkeypatch.setattr(init, "run_bootstrap_mode", lambda a: seen.setdefault("r", a.release) or 0)
    init.main()
    assert seen["r"] is True
