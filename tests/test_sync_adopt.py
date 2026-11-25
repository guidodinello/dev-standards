"""sync.py adopt: onboarding a repo bootstrapped before Copier.

Real scratch git repos and real Copier renders. The "legacy" repo is rendered from
the scratch template at commit v1 with its answers file left out, as a repo
bootstrapped before Copier would be; then the template moves to v2. GitHub is
the in-memory FakeGitHub (conftest); the autouse guard fails any other call.
"""

import json
from pathlib import Path

import pytest
from conftest import CI, ZIZMOR, ZIZMOR_NEXT, commit_all, edit, edit_template, git

import sync
import sync_adopt
from sync import ANSWERS_FILE, Status
from sync_pr import SYNC_BRANCH

PYPROJECT = (
    '[project]\nname = "legacy"\nrequires-python = ">=3.13"\n'
    '[dependency-groups]\ndev = ["pytest"]\n'
)
# What detection finds in that pyproject (+ tests/), with the template's defaults.
ANSWERS = {
    "python_version": "3.13",
    "dev_style": "groups",
    "include_tests": True,
    "locked": False,
    "branch": "main",
    "release": False,
}


def render_into(dest: Path, template: Path, ref: str = "HEAD") -> None:
    """Write what the template renders at `ref`, without the answers file."""
    for path, text in sync_adopt.init.render(
        Path(), ANSWERS, src=template, vcs_ref=ref, src_url=str(template)
    ).items():
        if path.as_posix() != ANSWERS_FILE:
            (dest / path).parent.mkdir(parents=True, exist_ok=True)
            (dest / path).write_text(text)


@pytest.fixture
def template(project, monkeypatch):
    template, _ = project
    monkeypatch.setattr(sync_adopt, "template_source", lambda: str(template))
    return template


@pytest.fixture
def legacy(tmp_path, template, origins):
    """(template, origin): a Python repo with the template's v1 files, no answers file."""
    origin = tmp_path / "legacy"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "uploadpack.allowFilter", "true")
    (origin / "pyproject.toml").write_text(PYPROJECT)
    (origin / "tests").mkdir()
    (origin / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    render_into(origin, template)
    commit_all(origin, "bootstrap before Copier")
    origins["repo"] = origin
    return template, origin


@pytest.fixture
def config(tmp_path):
    def write(entry=None):
        path = tmp_path / "github-standard.json"
        path.write_text(json.dumps({"org": "org", "repos": {"repo": entry or {}}}))
        return str(path)

    return write


def adopt(tmp_path) -> sync_adopt.Adoption:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return sync_adopt.adopt_repo("org", "repo", work)


def local_edit(origin: Path) -> None:
    """A line the repo added, away from the template's v2 change."""
    with (origin / CI).open("a") as f:
        f.write("# this repo's own line\n")
    commit_all(origin, "local edit")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


# ── choosing `_commit` ──────────────────────────────────────────────────────


def test_adopt_picks_the_closest_commit_not_the_newest(legacy, tmp_path):
    template, origin = legacy
    v1 = head(template)
    local_edit(origin)
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    v2 = head(template)

    adoption = adopt(tmp_path)

    assert [c.sha for c in adoption.candidates] == [v2, v1]
    assert adoption.chosen.sha == v1
    assert adoption.chosen.score == 1  # the repo's own line
    assert [path for path, _, _ in adoption.chosen.residual] == [str(CI)]
    assert adoption.candidates[0].score == 3  # v2 also differs on the zizmor line
    # The first sync after onboarding brings v2 in and keeps the local edit.
    assert adoption.preview.status is Status.CLEAN
    assert f"+        run: uvx {ZIZMOR_NEXT}" in adoption.preview.diff
    assert "this repo's own line" not in adoption.preview.diff  # kept, not touched


def test_a_repo_on_the_newest_render_adopts_it_and_has_nothing_to_sync(legacy, tmp_path):
    template, origin = legacy
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    render_into(origin, template)
    commit_all(origin, "hand-applied v2")

    adoption = adopt(tmp_path)

    assert adoption.chosen.sha == head(template)
    assert (adoption.chosen.score, adoption.chosen.residual) == (0, ())
    assert adoption.preview.status is Status.UNCHANGED


def test_a_tie_goes_to_the_newest_commit(legacy, tmp_path):
    template, _ = legacy
    edit(template, Path("copier.yml"), "# Copier template", "# The Copier template", "comment")

    adoption = adopt(tmp_path)

    assert [c.score for c in adoption.candidates] == [0, 0]
    assert adoption.chosen.sha == head(template)


def test_a_commit_rendering_fewer_files_is_no_candidate(legacy, tmp_path):
    """v1 lacks a file the template added later: adopting v1 would turn that file,
    which the repo already has, into a whole-file conflict on the first sync."""
    template, origin = legacy
    (template / "templates" / "copier" / "NEW.md").write_text("added later\n")
    commit_all(template, "add a file")
    (origin / "NEW.md").write_text("added later\n")
    commit_all(origin, "has it already")

    adoption = adopt(tmp_path)

    assert [c.sha for c in adoption.candidates] == [head(template)]
    assert adoption.preview.status is Status.UNCHANGED


def test_answers_come_from_init_detection_and_the_existing_files(legacy, tmp_path):
    template, origin = legacy
    (origin / "pyproject.toml").write_text(PYPROJECT.replace(">=3.13", ">=3.12"))
    ci = (origin / CI).read_text()
    (origin / CI).write_text(ci.replace("uv sync --dev", "uv sync --locked --dev"))
    (origin / ".github" / "workflows" / "release.yml").write_text("on: push\n")
    commit_all(origin, "a repo with other answers")

    adoption = adopt(tmp_path)

    assert adoption.answers == {
        **ANSWERS,
        "python_version": "3.12",
        "locked": True,
        "release": True,
    }
    text = adoption.chosen.answers_text
    assert "python_version: '3.12'" in text or 'python_version: "3.12"' in text
    assert "locked: true" in text and "release: true" in text
    assert f"_src_path: {template}" in text


# ── CLI: dry run and refusals ───────────────────────────────────────────────


def test_the_dry_run_reports_and_touches_nothing(legacy, config, capsys):
    template, origin = legacy
    local_edit(origin)
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    config_path = config()
    config_before = Path(config_path).read_text()
    refs_before = git(origin, "show-ref")

    assert sync.main(["adopt", "repo", "--config", config_path]) == 0
    out = capsys.readouterr().out

    assert "DRY RUN" in out
    assert "[adopt] repo: template commit" in out
    assert "(1 line(s) differ from it)" in out
    assert "answers: python_version=3.13, dev_style=groups" in out
    assert f"    {CI}: 1 line(s)" in out
    assert "+# this repo's own line" in out  # the residual hunk
    assert "first sync after the adopt PR merges: [clean] repo" in out
    assert f"+        run: uvx {ZIZMOR_NEXT}" in out
    assert git(origin, "show-ref") == refs_before
    assert Path(config_path).read_text() == config_before


@pytest.mark.usefixtures("onboarded", "template")
def test_an_onboarded_repo_is_refused(config, capsys):
    assert sync.main(["adopt", "repo", "--config", config()]) == 1
    assert "[error] repo: AdoptError: already onboarded" in capsys.readouterr().out


def test_a_skipped_repo_is_refused_without_a_clone(config, capsys, monkeypatch):
    monkeypatch.setattr(sync, "clone_command", lambda *a: pytest.fail("cloned"))
    config_path = config({"template_sync": {"skip": "diverged on purpose"}})
    assert sync.main(["adopt", "repo", "--config", config_path]) == 1
    assert "[skipped] repo: diverged on purpose" in capsys.readouterr().err


def test_a_non_python_repo_is_an_error_not_an_exit(legacy, config, capsys):
    _, origin = legacy
    (origin / "pyproject.toml").unlink()
    commit_all(origin, "not Python any more")
    assert sync.main(["adopt", "repo", "--config", config()]) == 1
    assert "[error] repo: DetectionError:" in capsys.readouterr().out


def test_an_unregistered_repo_is_refused(config):
    assert sync.main(["adopt", "other", "--config", config()]) == 1


# ── --apply ─────────────────────────────────────────────────────────────────


def test_apply_opens_a_pr_adding_only_the_answers_file(legacy, fake, config, capsys):
    template, origin = legacy
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    config_path = config()

    assert sync.main(["adopt", "repo", "--apply", "--config", config_path]) == 0
    out = capsys.readouterr().out

    pr = next(iter(fake.pulls["repo"]))
    assert pr["head"]["ref"] == SYNC_BRANCH and pr["base"]["ref"] == "main"
    assert pr["title"].startswith("chore(dev-standards): onboard template sync at ")
    assert "Part of guidodinello/dev-standards#53" in pr["body"]
    assert "- `python_version`: `3.13`" in pr["body"]
    assert "1 file(s) to update, no conflicts." in pr["body"]
    commit = fake.head("repo")
    assert list(commit["files"]) == [ANSWERS_FILE]
    assert commit["parents"] == [head(origin)]
    assert "Dev-Standards-Sync: 1" in commit["message"]
    assert f"apply: opened PR: {pr['html_url']}" in out

    # A re-run with nothing new writes nothing.
    writes = len(fake.writes)
    assert sync.main(["adopt", "repo", "--apply", "--config", config_path]) == 0
    assert "already up to date" in capsys.readouterr().out
    assert len(fake.writes) == writes

    # Once it merges, the repo is onboarded: sync brings v2 in.
    (origin / ANSWERS_FILE).write_text(commit["files"][ANSWERS_FILE])
    commit_all(origin, "merge the adopt PR")
    result = sync.sync_repo("org", "repo", Path(config_path).parent / "sync-work")
    assert result.status is Status.CLEAN
    assert f"+        run: uvx {ZIZMOR_NEXT}" in result.diff


def test_apply_without_the_workflow_scope_stops_before_cloning(
    legacy, fake, config, capsys, monkeypatch
):
    monkeypatch.setattr(sync_adopt.sync_pr, "token_scopes", lambda: {"repo"})
    monkeypatch.setattr(sync, "clone_command", lambda *a: pytest.fail("cloned"))
    assert sync.main(["adopt", "repo", "--apply", "--config", config()]) == 1
    assert "workflow" in capsys.readouterr().err
    assert fake.calls == []


def test_a_rendered_file_the_repo_lacks_stays_missing_and_is_flagged(legacy, tmp_path):
    """Copier takes a file the base render has and the repo doesn't as deleted on
    purpose: the first sync doesn't add it back. The report says so."""
    template, origin = legacy
    (origin / ".github" / "CODEOWNERS").unlink()
    commit_all(origin, "no CODEOWNERS")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)

    adoption = adopt(tmp_path)

    assert ".github/CODEOWNERS" in [path for path, _, _ in adoption.chosen.residual]
    assert "CODEOWNERS" not in adoption.preview.diff
    assert f"+        run: uvx {ZIZMOR_NEXT}" in adoption.preview.diff
    assert "(missing here: no sync will add it)" in sync_adopt.report(adoption)
