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
    assert init.install_cmd_for("optional", locked=False) == 'uv pip install -e ".[dev]"'


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


def test_render_pre_commit_substitutes_python_version():
    rendered = init.render_pre_commit("3.11")
    assert "python3.11" in rendered
    assert "--py311-plus" in rendered
    assert "python3.13" not in rendered


def test_main_requires_repo_argument(monkeypatch):
    monkeypatch.setattr("sys.argv", ["init.py"])
    with pytest.raises(SystemExit):
        init.main()
