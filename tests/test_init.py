import re
import subprocess

import pytest
import yaml
from conftest import REPO_ROOT, import_module_from_path, rendered

init = import_module_from_path("dev_standards_init", REPO_ROOT / "init.py")

CI = ".github/workflows/ci.yml"
AUTOMERGE = ".github/workflows/dependabot-automerge.yml"
RELEASE = ".github/workflows/release.yml"
ANSWERS = ".copier-answers.yml"


def ci(**answers: object) -> str:
    return rendered(**answers)[CI]


def pre_commit(**answers: object) -> str:
    return rendered(**answers)[".pre-commit-config.yaml"]


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


def test_install_step_follows_dev_style_and_locked():
    assert ci().count("run: uv sync --dev\n") == 2  # lint and test jobs
    assert ci(locked=True).count("run: uv sync --locked --dev\n") == 2
    # --locked doesn't apply to the optional-dependencies style
    optional = ci(dev_style="optional", locked=True)
    assert optional.count('run: uv venv && uv pip install -e ".[dev]"\n') == 2


def test_detect_tests_requires_both_dir_and_pytest_dep_under_same_style(tmp_path):
    pyproject = {"dependency-groups": {"dev": ["pytest>=8.0"]}}
    (tmp_path / "tests").mkdir()
    assert init.detect_tests(tmp_path, pyproject, "groups") is True
    assert init.detect_tests(tmp_path, pyproject, "optional") is False


def test_render_ci_strips_test_job_when_excluded():
    text = ci(python_version="3.13", branch="main", include_tests=False)
    assert "test-python" not in text
    assert "branches: [main]" in text


def test_render_ci_keeps_test_job_when_included():
    text = ci(python_version="3.13", branch="develop", include_tests=True)
    assert "test-python" in text
    assert "branches: [develop]" in text


def test_render_ci_keeps_ci_runners_switch():
    """Guards against silently dropping the CI_RUNNERS fallback expression during a
    future template edit — without it, every repo text from this template loses
    its ability to route CI off GitHub-hosted runners (see README § CI runners)."""
    text = ci(python_version="3.13", branch="main", include_tests=True)
    assert text.count("fromJSON(vars.CI_RUNNERS") == 4


def test_render_automerge_keeps_ci_runners_switch():
    """Same guard as test_render_ci_keeps_ci_runners_switch, for the auto-merge
    template: without it, auto-merge stays pinned to ubuntu-latest and keeps
    failing on private repos where GitHub-hosted minutes are billing-blocked
    (see README § CI runners)."""
    text = rendered()[AUTOMERGE]
    assert text.count("fromJSON(vars.CI_RUNNERS") == 1


def test_render_pre_commit_substitutes_python_version():
    text = pre_commit(python_version="3.11")
    assert "python3.11" in text
    assert "--py311-plus" in text
    assert "python3.13" not in text


def test_render_ci_runs_lint_on_the_pre_commit_python_version():
    """The lint job's venv Python must be the one the pre-commit render writes into
    language_version; otherwise uv picks the newest allowed Python and pre-commit
    can't build the env of a hook pinned to python3.X (failed this way on #39)."""
    text = ci(python_version="3.11", branch="main", include_tests=True)
    assert 'python-version: "3.11"' in text
    assert "python3.11" in pre_commit(python_version="3.11")


def test_main_requires_repo_argument(monkeypatch):
    monkeypatch.setattr("sys.argv", ["init.py"])
    with pytest.raises(SystemExit):
        init.main()


def _ci_needs(text: str) -> list[str]:
    block = text.split("\n  ci:\n")[1]
    needs = block.split("    needs:\n")[1].split("    if:")[0]
    return [line.strip()[2:] for line in needs.splitlines()]


def test_render_ci_aggregate_job_needs_every_job():
    text = ci(python_version="3.13", branch="main", include_tests=True)
    assert _ci_needs(text) == ["lint-python", "test-python", "audit-workflows"]
    assert "if: always()" in text


def test_render_ci_without_tests_keeps_aggregate_job_without_dangling_need():
    text = ci(python_version="3.13", branch="main", include_tests=False)
    assert _ci_needs(text) == ["lint-python", "audit-workflows"]
    assert "test-python" not in text
    assert text.count("fromJSON(vars.CI_RUNNERS") == 3


def test_has_aggregate_ci_job():
    assert init.has_aggregate_ci_job(ci()) is True
    assert init.has_aggregate_ci_job(None) is False
    no_always = ci().replace("    if: always()\n", "")
    assert init.has_aggregate_ci_job(no_always) is False


def test_checks_snippet_aggregate_pins_integration_id_from_app():
    runs = [{"name": "Lint Python"}, {"name": "CI", "app": {"id": 15368}}]
    checks, hint = init.checks_snippet(runs, ci())
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
    checks, hint = init.checks_snippet([{"name": "CI", "app": {"id": 1}}], ci())
    assert checks == [{"context": "CI"}]
    assert hint is not None and "not GitHub Actions" in hint


def test_release_workflow_is_opt_in():
    """`release` drives copier.yml's templated _exclude; when true the pattern renders
    empty, which must exclude nothing else."""
    without = set(rendered(release=False))
    assert without == {
        ANSWERS,
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
        AUTOMERGE,
        CI,
        ".pre-commit-config.yaml",
        "CHANGELOG.md",
    }
    assert set(rendered(release=True)) == without | {RELEASE}


def test_answers_file_points_at_the_remote_and_records_the_answers():
    answers = yaml.safe_load(rendered()[ANSWERS])
    assert answers["_src_path"] == init.TEMPLATE_URL  # not this checkout's local path
    assert answers["_commit"]
    assert answers["python_version"] == "3.13"
    assert answers["release"] is True
    assert "install_cmd" not in answers  # computed, never stored


def _existing(root, rel, text="the repo's own\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _files(root):
    return {root / rel: f"rendered {rel}\n" for rel in (ANSWERS, CI, "CHANGELOG.md")}


def test_force_never_overwrites_a_skip_if_exists_file(tmp_path, capsys):
    changelog = _existing(tmp_path, "CHANGELOG.md")
    ci_file = _existing(tmp_path, CI)
    init.write_all(_files(tmp_path), tmp_path, apply=True, force=True)
    assert changelog.read_text() == "the repo's own\n"
    assert ci_file.read_text() == f"rendered {CI}\n"
    assert (tmp_path / ANSWERS).read_text() == f"rendered {ANSWERS}\n"
    out = capsys.readouterr().out
    assert "kept (the repo's own" in out
    assert "pass --force" not in out  # it was passed


def test_answers_file_is_not_written_when_a_rendered_file_was_kept(tmp_path, capsys):
    """The answers file claims every file came from its _commit; a file kept as the
    repo had it didn't, and a later `copier update` would merge from the wrong base."""
    ci_file = _existing(tmp_path, CI)
    init.write_all(_files(tmp_path), tmp_path, apply=True, force=False)
    assert not (tmp_path / ANSWERS).exists()
    assert ci_file.read_text() == "the repo's own\n"
    assert (tmp_path / "CHANGELOG.md").exists()  # the rest is still written
    out = capsys.readouterr().out
    assert f"not writing {tmp_path / ANSWERS}" in out
    assert CI in out


def test_answers_file_is_written_when_only_a_skip_if_exists_file_was_kept(tmp_path):
    """Copier itself never renders over an existing CHANGELOG.md, so keeping it
    doesn't make the answers file's claim false."""
    _existing(tmp_path, "CHANGELOG.md")
    init.write_all(_files(tmp_path), tmp_path, apply=True, force=False)
    assert (tmp_path / ANSWERS).read_text() == f"rendered {ANSWERS}\n"


def test_dry_run_reports_the_withheld_answers_file(tmp_path, capsys):
    _existing(tmp_path, CI)
    init.write_all(_files(tmp_path), tmp_path, apply=False, force=False)
    assert f"not writing {tmp_path / ANSWERS}" in capsys.readouterr().out
    assert not (tmp_path / ANSWERS).exists()


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """A committed repo whose HEAD is on origin/main (no remote to ls-remote)."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def test_template_checkout_on_origin_main_passes(checkout):
    init.check_template_checkout(checkout, apply=True)


@pytest.mark.parametrize("dirty", ["modified", "untracked"])
def test_template_checkout_refuses_any_dirty_state(checkout, dirty):
    """Untracked files count: Copier commits them into its throwaway commit too."""
    (checkout / ("f" if dirty == "modified" else "new")).write_text("y\n")
    with pytest.raises(SystemExit):
        init.check_template_checkout(checkout, apply=True)


def test_template_checkout_refuses_a_commit_not_on_origin_main(checkout):
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "local only")
    with pytest.raises(SystemExit):
        init.check_template_checkout(checkout, apply=True)


def test_template_checkout_only_warns_in_a_dry_run(checkout, capsys):
    (checkout / "new").write_text("y\n")
    init.check_template_checkout(checkout, apply=False)
    assert "--apply would refuse" in capsys.readouterr().out


@pytest.fixture
def bootstrap(tmp_path, monkeypatch, capsys):
    """Runs the real (non --render-to) bootstrap against a scratch target repo, with
    the GitHub API calls and github-standard.json stubbed, and the dev-standards
    checkout reported dirty and off origin/main (as on any feature branch)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.13"\n')
    config = tmp_path / "github-standard.json"
    config.write_text('{"org": "o", "repos": {\n}}\n')
    monkeypatch.setattr(init, "CONFIG_PATH", config)
    monkeypatch.setattr(init, "preflight_gh_auth", lambda: None)
    monkeypatch.setattr(init, "get_default_branch", lambda org, repo: "main")
    monkeypatch.setattr(init, "preflight_ruleset", lambda org, repo, branch: None)
    monkeypatch.setattr(init, "print_checklist", lambda org, repo, docs_present: None)
    dirty = {"status": " M init.py\n"}
    monkeypatch.setattr(
        init,
        "_git",
        lambda repo, *args: subprocess.CompletedProcess(args, 1, dirty.get(args[0], ""), ""),
    )

    def run(*flags: str) -> int:
        monkeypatch.setattr("sys.argv", ["init.py", "x", "--path", str(repo), *flags])
        return init.main()

    return repo, config, run


def test_dry_run_from_a_dirty_checkout_succeeds_and_writes_nothing(bootstrap, capsys):
    repo, config, run = bootstrap
    assert run() == 0
    assert sorted(p.name for p in repo.iterdir()) == ["pyproject.toml"]
    assert config.read_text() == '{"org": "o", "repos": {\n}}\n'
    out = capsys.readouterr().out
    assert "--apply would refuse" in out
    assert "would write" in out


def test_apply_from_a_dirty_checkout_refuses_before_writing(bootstrap):
    repo, config, run = bootstrap
    with pytest.raises(SystemExit):
        run("--apply")
    assert sorted(p.name for p in repo.iterdir()) == ["pyproject.toml"]


def test_render_to_writes_every_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n[dependency-groups]\ndev = ["pytest"]\n'
    )
    out = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        ["init.py", "x", "--path", str(repo), "--render-to", str(out), "--apply", "--release"],
    )
    assert init.main() == 0
    written = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert written == set(rendered(release=True))
    assert 'python-version: "3.12"' in (out / CI).read_text()
    assert yaml.safe_load((out / ANSWERS).read_text())["python_version"] == "3.12"


def test_release_template_is_least_privilege_and_tag_triggered():
    text = rendered()[RELEASE]
    assert 'tags: ["v*"]' in text
    assert (
        text.count("\n      contents: write\n") == 1
    )  # one job; top level stays `permissions: {}`
    assert "\npermissions: {}\n" in text
    assert text.count("fromJSON(vars.CI_RUNNERS") == 1


def test_release_template_gates_publish_on_verification():
    text = rendered()[RELEASE]
    # verification and notes extraction both precede the build and the release
    order = [
        text.index("Verify tag, version, and branch"),
        text.index("Extract release notes from CHANGELOG.md"),
        text.index("Build artifacts"),
        text.index("gh release create"),
    ]
    assert order == sorted(order)
    assert "--verify-tag" in text


# Rendered lazily, inside each test (rendering clones this repo through Copier).
WORKFLOWS = {
    "ci": lambda: ci(),
    "ci-no-tests": lambda: ci(include_tests=False),
    "automerge": lambda: rendered()[AUTOMERGE],
    "release": lambda: rendered()[RELEASE],
    **{
        f"checked-in {path.name}": path.read_text
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    },
}


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_action_is_pinned_to_a_sha(name):
    uses = [
        line.strip()
        for line in WORKFLOWS[name]().splitlines()
        if "uses:" in line and not line.lstrip().startswith("#")
    ]
    assert uses
    for line in uses:
        assert re.fullmatch(r"(- )?uses: \S+@[0-9a-f]{40} # v\d.*", line), line


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_checkout_does_not_persist_credentials(name):
    text = WORKFLOWS[name]()
    steps = text.split("actions/checkout@")[1:]
    for step in steps:
        # the step's own block runs to the next blank line or the next list item
        block = re.split(r"\n\s*\n|\n\s*- ", step)[0]
        assert "persist-credentials: false" in block, block


def test_release_checkout_fetches_full_history():
    text = rendered()[RELEASE]
    step = text[text.index("actions/checkout@") :].split("\n\n")[0]
    assert "fetch-depth: 0" in step  # the on-default-branch check needs full history


@pytest.mark.parametrize("include_tests", [True, False])
def test_ci_audit_job_survives_render_and_is_required(include_tests):
    text = ci(python_version="3.13", branch="main", include_tests=include_tests)
    assert "  audit-workflows:\n" in text
    ci_job = text[text.index("\n  ci:\n") :]
    needs = ci_job[: ci_job.index("if: always()")]
    assert "      - audit-workflows\n" in needs
    zizmor_step = text[text.index("name: zizmor") :]
    assert "continue-on-error" not in zizmor_step.split("run:")[0]


def test_automerge_policy():
    text = rendered()[AUTOMERGE]
    assert "if: github.event.pull_request.user.login == 'dependabot[bot]'" in text
    assert "if: github.actor" not in text
    assert "permission-contents: write" in text
    assert "permission-pull-requests: write" in text
    merge = text[text.index("- name: Auto-merge patch and minor updates") :].split("run:")[0]
    assert "package-ecosystem != 'github_actions'" in merge


def test_release_flag_parses(monkeypatch):
    monkeypatch.setattr("sys.argv", ["init.py", "x", "--release"])
    seen = {}
    monkeypatch.setattr(init, "run_bootstrap_mode", lambda a: seen.setdefault("r", a.release) or 0)
    init.main()
    assert seen["r"] is True
