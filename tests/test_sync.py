"""sync.py: the dry-run `copier update` over every registered repo.

Scratch git repos only, no network. The one seam is sync.clone_command: a real run
clones with `gh repo clone`, these tests clone a local file:// "origin" with the
same shallow, blobless, no-checkout flags, so the probe, the checkout and
`copier update` on a depth-1 clone all run for real.
"""

import json

import pytest
from conftest import (
    CACHE_PIN,
    CACHE_PIN_B,
    CACHE_PIN_C,
    CI,
    ZIZMOR,
    ZIZMOR_NEXT,
    commit_all,
    edit,
    edit_template,
    git,
)

import sync
from sync import Status


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


def run(tmp_path, name="repo"):
    return sync.sync_repo("org", name, tmp_path / "work")


def test_unchanged_when_the_template_did_not_move(onboarded, tmp_path):
    assert run(tmp_path).status is Status.UNCHANGED


def test_clean_update_shows_the_template_change(onboarded, tmp_path):
    template, _ = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    result = run(tmp_path)
    assert result.status is Status.CLEAN
    assert f"+        run: uvx {ZIZMOR_NEXT}" in result.diff
    assert not result.answers_only


def test_answers_only_when_no_rendered_file_changes(onboarded, tmp_path):
    """A template commit that renders the same files (here a copier.yml comment)
    still moves `_commit`; PR 4 must tell that apart to skip no-op PRs."""
    template, _ = onboarded
    edit(template, "copier.yml", "# Copier template", "# The Copier template", "comment")
    result = run(tmp_path)
    assert result.status is Status.CLEAN
    assert result.answers_only


def test_pin_conflict_resolves_to_the_template_when_it_is_newer(onboarded, tmp_path):
    template, origin = onboarded
    edit(origin, CI, CACHE_PIN, CACHE_PIN_B, "dependabot bump")  # v99.0.0
    edit_template(template, CACHE_PIN, CACHE_PIN_C)  # v99.1.0
    result = run(tmp_path)
    assert result.status is Status.CLEAN
    assert result.pins_resolved == 1
    assert f"+      - uses: {CACHE_PIN_C}" in result.diff
    assert "<<<<<<<" not in result.diff


def test_pin_conflict_keeps_the_repo_pin_when_it_is_newer(onboarded, tmp_path):
    template, origin = onboarded
    edit(origin, CI, CACHE_PIN, CACHE_PIN_C, "dependabot bump")  # v99.1.0
    edit_template(template, CACHE_PIN, CACHE_PIN_B)  # v99.0.0
    result = run(tmp_path)
    assert result.pins_resolved == 1
    assert result.status is Status.CLEAN
    assert result.answers_only  # ci.yml stays exactly as the repo has it


def test_a_real_conflict_is_left_in_place_and_reported(onboarded, tmp_path):
    template, origin = onboarded
    # zizmor's `uvx` version isn't a Dependabot pin, and the repo edited the same line.
    edit(origin, CI, "--format=github .github", "--format=plain .github", "local edit")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    result = run(tmp_path)
    assert result.status is Status.CONFLICTS
    assert result.conflicted_files == (str(CI),)
    assert result.pins_resolved == 0
    assert "+<<<<<<< before updating" in result.diff
    assert "+>>>>>>> after updating" in result.diff


def test_a_repo_without_an_answers_file_is_not_onboarded(origins, tmp_path):
    origin = tmp_path / "plain"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "README.md").write_text("not rendered from the template\n")
    commit_all(origin, "init")
    origins["plain"] = origin
    assert run(tmp_path, "plain").status is Status.NOT_ONBOARDED


def test_updates_to_head_even_when_the_template_has_a_version_tag(onboarded, tmp_path):
    """Decision 3: sync to main HEAD. Without vcs_ref="HEAD" Copier would pick the
    latest PEP 440 tag, here the commit the repo is already on."""
    template, _ = onboarded
    git(template, "tag", "1.0.0")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    assert ZIZMOR_NEXT in run(tmp_path).diff


def test_main_reports_every_repo_and_never_pushes(onboarded, origins, tmp_path, capsys):
    template, origin = onboarded
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    origins["gone"] = tmp_path / "does-not-exist"
    config = tmp_path / "github-standard.json"
    config.write_text(
        json.dumps({"org": "org", "repos": {"repo": {}, "gone": {}, "_comment": "ignored"}})
    )
    config_before = config.read_text()
    refs_before = git(origin, "show-ref")

    assert sync.main(["--config", str(config)]) == 1  # "gone" errored
    out = capsys.readouterr().out

    assert "DRY RUN" in out
    assert "[clean] repo" in out
    assert "[error] gone" in out
    assert "2 repos: 0 not onboarded, 0 unchanged, 1 clean, 0 conflicts, 1 error" in out
    assert git(origin, "show-ref") == refs_before  # nothing pushed
    assert config.read_text() == config_before  # config only read


def test_main_rejects_an_unregistered_repo(tmp_path):
    config = tmp_path / "github-standard.json"
    config.write_text(json.dumps({"org": "org", "repos": {"repo": {}}}))
    assert sync.main(["other", "--config", str(config)]) == 1


# ── resolve_pin_conflicts ───────────────────────────────────────────────────


def hunk(ours: str, theirs: str) -> str:
    return f"<<<<<<< before updating\n{ours}=======\n{theirs}>>>>>>> after updating\n"


RUFF = "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"


def test_pre_commit_rev_resolves_with_the_repo_line_above_the_hunk():
    text = RUFF + hunk("    rev: v0.18.0\n", "    rev: v0.17.9\n") + "    hooks:\n"
    resolved, n_resolved, left = sync.resolve_pin_conflicts(text)
    assert resolved == RUFF + "    rev: v0.18.0\n    hooks:\n"
    assert (n_resolved, left) == (1, 0)


def test_each_hunk_resolves_on_its_own():
    other = "  - repo: https://github.com/asottile/pyupgrade\n"
    text = (
        RUFF
        + hunk("    rev: v0.17.0\n", "    rev: v0.18.0\n")
        + other
        + hunk("    rev: v3.22.0\n", "    rev: v3.21.0\n")
    )
    resolved, n_resolved, left = sync.resolve_pin_conflicts(text)
    assert resolved == RUFF + "    rev: v0.18.0\n" + other + "    rev: v3.22.0\n"
    assert (n_resolved, left) == (2, 0)


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        pytest.param(  # a pin plus another change
            f"      - uses: {CACHE_PIN_B}\n        with: {{x: 1}}\n",
            f"      - uses: {CACHE_PIN_C}\n        with: {{x: 2}}\n",
            id="mixed",
        ),
        pytest.param(
            "      - uses: actions/cache@main\n",
            f"      - uses: {CACHE_PIN_C}\n",
            id="unparseable-version",
        ),
        pytest.param(  # same tag, different commit: suspicious, a human decides
            "      - uses: actions/cache@" + "d" * 40 + " # v99.1.0\n",
            f"      - uses: {CACHE_PIN_C}\n",
            id="equal-versions-different-shas",
        ),
        pytest.param(
            "      - uses: actions/cache@" + "d" * 40 + "\n",
            f"      - uses: {CACHE_PIN_C}\n",
            id="sha-without-tag",
        ),
    ],
)
def test_hunks_that_are_not_comparable_pin_bumps_stay_conflicts(ours, theirs):
    text = "    steps:\n" + hunk(ours, theirs)
    assert sync.resolve_pin_conflicts(text) == (text, 0, 1)
