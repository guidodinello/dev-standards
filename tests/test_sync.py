"""sync.py: the dry-run `copier update` over every registered repo.

Scratch git repos only, no network. The one seam is sync.clone_command: a real run
clones with `gh repo clone`, these tests clone a local file:// "origin" with the
same shallow, blobless, no-checkout flags, so the probe, the checkout and
`copier update` on a depth-1 clone all run for real.
"""

import json

import pytest
import yaml
from conftest import (
    CACHE_PIN,
    CACHE_PIN_B,
    CACHE_PIN_C,
    CI,
    ZIZMOR,
    ZIZMOR_NEXT,
    commit_all,
    copy_template,
    edit,
    edit_template,
    git,
)
from copier import run_copy

import sync
from sync import Status


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


# ── CHANGELOG.md: rendered on the first copy only ─────────────────────────────

CHANGELOG = "CHANGELOG.md"


def change_template_and_its_changelog(template):
    with (template / "templates" / "copier" / CHANGELOG).open("a") as f:
        f.write("\n- A template-only line\n")
    commit_all(template, "template changelog change")
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)


def without_the_changelog_exclude(copier_yml: str) -> str:
    """copier.yml as before the fix: the `_exclude` entry naming CHANGELOG.md (read
    from the parsed list, not spelled out here) dropped, every other key kept."""
    excludes = yaml.safe_load(copier_yml)["_exclude"]
    (entry,) = (pattern for pattern in excludes if CHANGELOG in pattern)
    old = "".join(line for line in copier_yml.splitlines(True) if entry not in line)
    assert yaml.safe_load(old)["_exclude"] == [p for p in excludes if p != entry]
    return old


def assert_changelog_untouched(result):
    assert result.status is Status.CLEAN
    assert f"+        run: uvx {ZIZMOR_NEXT}" in result.diff  # the template change came in
    assert CHANGELOG not in [path for path, _ in result.changes]
    assert CHANGELOG not in result.diff


def test_a_sync_never_adds_a_changelog_the_repo_does_not_have(onboarded, tmp_path):
    """_skip_if_exists alone re-renders a missing CHANGELOG.md on every update; the
    update-time _exclude keeps the template's placeholder out."""
    template, origin = onboarded
    (origin / CHANGELOG).unlink()
    commit_all(origin, "no changelog")
    change_template_and_its_changelog(template)
    assert_changelog_untouched(run(tmp_path))


def test_no_changelog_is_added_when_the_repo_is_on_a_commit_before_the_exclude(tmp_path, origins):
    """The repo's `_commit` predates the update-time _exclude. Copier renders that
    old commit with the new template's excludes, so CHANGELOG.md is in neither
    render and isn't added (nor seen as deleted)."""
    (tmp_path / "template").mkdir()
    template = copy_template(tmp_path / "template")
    current = (template / "copier.yml").read_text()
    (template / "copier.yml").write_text(without_the_changelog_exclude(current))
    git(template, "init", "-q")
    commit_all(template, "before the exclude")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    git(origin, "config", "uploadpack.allowFilter", "true")
    run_copy(str(template), origin, defaults=True, quiet=True)
    (origin / CHANGELOG).unlink()
    commit_all(origin, "render, no changelog")
    origins["repo"] = origin
    (template / "copier.yml").write_text(current)
    commit_all(template, "the exclude")
    change_template_and_its_changelog(template)
    assert_changelog_untouched(run(tmp_path))


def test_a_sync_leaves_the_repos_own_changelog_alone(onboarded, tmp_path):
    template, origin = onboarded
    own = "# Changelog\n\nThe repo's own history.\n"
    (origin / CHANGELOG).write_text(own)
    commit_all(origin, "own changelog")
    change_template_and_its_changelog(template)
    result = run(tmp_path)
    assert_changelog_untouched(result)
    assert (tmp_path / "work" / "repo" / CHANGELOG).read_text() == own


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
    assert "2 repos: 0 skipped, 0 not onboarded, 0 unchanged, 1 clean, 0 conflicts, 1 error" in out
    assert git(origin, "show-ref") == refs_before  # nothing pushed
    assert config.read_text() == config_before  # config only read


def _break_answers(origin, key, value):
    path = origin / ".copier-answers.yml"
    text = path.read_text()
    line = next(line for line in text.splitlines() if line.startswith(f"{key}:"))
    path.write_text(text.replace(line, f"{key}: {value}"))
    commit_all(origin, f"break {key}")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("_commit", "deadbee", id="unknown-commit"),  # ValueError in Copier
        pytest.param(  # plumbum ProcessExecutionError: git clone of the URL fails
            "_src_path", "file:///nonexistent/template.git", id="unreachable-template-url"
        ),
    ],
)
def test_a_failing_update_is_that_repos_error_and_the_run_continues(
    onboarded, origins, tmp_path, capsys, key, value
):
    """The docstring promises the other repos still run: an exception from one
    repo's update (not only CopierError / CalledProcessError) is reported as that
    repo's error, and main() still syncs the next repo and prints the summary."""
    template, origin = onboarded
    _break_answers(origin, key, value)
    edit_template(template, ZIZMOR, ZIZMOR_NEXT)
    (tmp_path / "healthy").mkdir()
    git(tmp_path / "healthy", "clone", "-q", str(origin), ".")
    git(tmp_path / "healthy", "revert", "--no-edit", "HEAD")  # answers file restored
    origins["healthy"] = tmp_path / "healthy"
    config = tmp_path / "github-standard.json"
    config.write_text(json.dumps({"org": "org", "repos": {"repo": {}, "healthy": {}}}))

    assert sync.main(["--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert "[error] repo: " in out
    assert "[clean] healthy" in out
    assert "2 repos: 0 skipped, 0 not onboarded, 0 unchanged, 1 clean, 0 conflicts, 1 error" in out


def test_ctrl_c_is_not_swallowed(onboarded, tmp_path, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(sync, "run_update", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path)


def test_main_rejects_an_unregistered_repo(tmp_path):
    config = tmp_path / "github-standard.json"
    config.write_text(json.dumps({"org": "org", "repos": {"repo": {}}}))
    assert sync.main(["other", "--config", str(config)]) == 1


CACHE_COMMENT = (
    "        # Persistent homelab runners keep ~/.cache/pre-commit on disk; restoring or\n"
)


def test_a_pin_only_template_change_keeps_the_repo_file_around_it(onboarded, tmp_path):
    """The repo edited the line next to a pin the template bumped: Copier's merge
    conflicts on the hunk, but the template changed nothing but that pin."""
    template, origin = onboarded
    edit(origin, CI, CACHE_COMMENT, "        # our own comment\n", "local edit")
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    result = run(tmp_path)
    assert result.status is Status.CLEAN
    old, new = CACHE_PIN.split("@")[1], CACHE_PIN_C.split("@")[1]
    assert result.resolved_pins == (sync.ResolvedPin(str(CI), "actions/cache", old, new, new),)
    assert f"-      - uses: {CACHE_PIN}" in result.diff
    assert f"+      - uses: {CACHE_PIN_C}" in result.diff
    assert "-        # our own comment" not in result.diff  # the repo's edit stays
    assert "<<<<<<<" not in result.diff


def test_a_template_change_beyond_pins_keeps_copiers_conflict(onboarded, tmp_path):
    template, origin = onboarded
    edit(origin, CI, CACHE_COMMENT, "        # our own comment\n", "local edit")
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    edit_template(template, CACHE_COMMENT, "        # the template's comment\n")
    result = run(tmp_path)
    assert result.status is Status.CONFLICTS
    assert result.conflicted_files == (str(CI),)


def test_a_copier_yml_change_keeps_copiers_conflict(onboarded, tmp_path):
    """copier.yml's questions and settings can change what renders: fail closed."""
    template, origin = onboarded
    edit(origin, CI, CACHE_COMMENT, "        # our own comment\n", "local edit")
    edit_template(template, CACHE_PIN, CACHE_PIN_C)
    edit(template, "copier.yml", "# Copier template", "# The Copier template", "comment")
    result = run(tmp_path)
    assert result.status is Status.CONFLICTS


# ── bump_template_pins ──────────────────────────────────────────────────────

CHECKOUT_7 = "actions/checkout@" + "a" * 40 + " # v7"
CHECKOUT_8 = "actions/checkout@" + "e" * 40 + " # v8"


def uses(*pins: str) -> str:
    return "".join(f"      - uses: {pin}\n" for pin in pins)


def test_bumps_every_occurrence_the_repo_has_wherever_it_moved_it():
    repo = "jobs:\n  mine:\n" + uses(CACHE_PIN_B) + "  extra:\n" + uses(CACHE_PIN_B)
    merged, pins = sync.bump_template_pins(repo, uses(CACHE_PIN_B), uses(CACHE_PIN_C))
    assert merged == repo.replace(CACHE_PIN_B, CACHE_PIN_C)
    old, new = CACHE_PIN_B.split("@")[1], CACHE_PIN_C.split("@")[1]
    assert pins == [
        ("actions/cache", old, new, new),
    ]


def test_keeps_a_repo_pin_newer_than_the_template():
    repo = uses(CACHE_PIN_C)
    merged, _ = sync.bump_template_pins(repo, uses(CACHE_PIN), uses(CACHE_PIN_B))
    assert merged == repo


def test_moves_only_the_names_the_template_bumped():
    """The repo's other pins differ from the template's too; they're not this sync's."""
    repo = uses(CHECKOUT_8, CACHE_PIN_B)
    merged, _ = sync.bump_template_pins(
        repo, uses(CHECKOUT_7, CACHE_PIN_B), uses(CHECKOUT_7, CACHE_PIN_C)
    )
    assert merged == uses(CHECKOUT_8, CACHE_PIN_C)


def test_a_name_the_repo_no_longer_pins_is_skipped():
    merged, pins = sync.bump_template_pins(uses(CHECKOUT_7), uses(CACHE_PIN_B), uses(CACHE_PIN_C))
    assert (merged, pins) == (uses(CHECKOUT_7), [])


@pytest.mark.parametrize(
    ("repo", "old", "new"),
    [
        pytest.param(
            uses(CACHE_PIN_B), uses(CACHE_PIN_B), uses(CACHE_PIN_C) + "# new\n", id="non-pin-change"
        ),
        pytest.param(
            uses(CACHE_PIN_B, CACHE_PIN),
            uses(CACHE_PIN_B),
            uses(CACHE_PIN_C),
            id="repo-two-versions",
        ),
        pytest.param(
            uses(CACHE_PIN_B),
            uses(CACHE_PIN_B, CACHE_PIN_B),
            uses(CACHE_PIN_C, CACHE_PIN_B),
            id="template-two-versions",
        ),
        pytest.param(
            uses("actions/cache@main"),
            uses(CACHE_PIN_B),
            uses(CACHE_PIN_C),
            id="unparseable-version",
        ),
    ],
)
def test_fails_closed(repo, old, new):
    assert sync.bump_template_pins(repo, old, new) is None


# ── resolve_pin_conflicts ───────────────────────────────────────────────────


def hunk(ours: str, theirs: str) -> str:
    return f"<<<<<<< before updating\n{ours}=======\n{theirs}>>>>>>> after updating\n"


RUFF = "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"


def test_pre_commit_rev_resolves_with_the_repo_line_above_the_hunk():
    text = RUFF + hunk("    rev: v0.18.0\n", "    rev: v0.17.9\n") + "    hooks:\n"
    resolved, pins, left = sync.resolve_pin_conflicts(text)
    assert resolved == RUFF + "    rev: v0.18.0\n    hooks:\n"
    assert pins == [
        ("https://github.com/astral-sh/ruff-pre-commit", "v0.18.0", "v0.17.9", "v0.18.0")
    ]
    assert left == 0


def test_each_hunk_resolves_on_its_own():
    other = "  - repo: https://github.com/asottile/pyupgrade\n"
    text = (
        RUFF
        + hunk("    rev: v0.17.0\n", "    rev: v0.18.0\n")
        + other
        + hunk("    rev: v3.22.0\n", "    rev: v3.21.0\n")
    )
    resolved, pins, left = sync.resolve_pin_conflicts(text)
    assert resolved == RUFF + "    rev: v0.18.0\n" + other + "    rev: v3.22.0\n"
    assert (len(pins), left) == (2, 0)


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
    assert sync.resolve_pin_conflicts(text) == (text, [], 1)


# ── file modes createCommitOnBranch can't carry ─────────────────────────────


def test_staged_changes_flag_executables_symlinks_and_mode_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name in ("plain", "was-plain", "deleted-script"):
        (repo / name).write_text(f"{name}\n")
    (repo / "deleted-script").chmod(0o755)
    commit_all(repo, "init")
    (repo / "plain").write_text("changed\n")  # regular file: fine
    (repo / "was-plain").chmod(0o755)  # mode change
    (repo / "new-script").write_text("#!/bin/sh\n")
    (repo / "new-script").chmod(0o755)  # new executable
    (repo / "link").symlink_to("plain")  # new symlink
    (repo / "deleted-script").unlink()  # deleting needs no mode: fine
    git(repo, "add", "-A")

    changes, unsupported = sync._staged_changes(repo)

    assert dict(changes) == {"plain": b"changed\n", "deleted-script": None}
    assert sorted(unsupported) == ["link", "new-script", "was-plain"]


# ── the config's skip list ──────────────────────────────────────────────────


def test_a_skipped_repo_is_reported_with_its_reason_and_never_cloned(
    onboarded, origins, tmp_path, capsys, monkeypatch
):
    cloned = []
    real_clone = sync.clone_command
    monkeypatch.setattr(
        sync,
        "clone_command",
        lambda org, repo, dest: cloned.append(repo) or real_clone(org, repo, dest),
    )
    origins["kept-out"] = tmp_path / "does-not-exist"  # a clone would be an error
    config = tmp_path / "github-standard.json"
    config.write_text(
        json.dumps(
            {
                "org": "org",
                "repos": {
                    "repo": {},
                    "kept-out": {"template_sync": {"skip": "diverged on purpose"}},
                },
            }
        )
    )

    assert sync.main(["--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert "[skipped] kept-out: diverged on purpose" in out
    assert "2 repos: 1 skipped, 0 not onboarded, 1 unchanged, 0 clean, 0 conflicts, 0 error" in out
    assert cloned == ["repo"]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"template_sync": {"skip": ""}}, id="empty-reason"),
        pytest.param({"template_sync": {"skip": True}}, id="not-a-reason"),
        pytest.param({"template_sync": "skip"}, id="not-an-object"),
        pytest.param({"template_sync": {}}, id="no-skip-key"),
        pytest.param({"template_sync": {"skpi": "diverged"}}, id="misspelled-key"),
        pytest.param({"template_sync": {"skip": "x", "extra": 1}}, id="extra-key"),
    ],
)
def test_a_malformed_skip_entry_is_an_error_and_the_repo_is_not_touched(
    onboarded, tmp_path, capsys, monkeypatch, entry
):
    """Fails closed: a skip someone meant to write, but wrote wrong, must not sync."""
    monkeypatch.setattr(sync, "clone_command", lambda *a: pytest.fail("cloned"))
    config = tmp_path / "github-standard.json"
    config.write_text(json.dumps({"org": "org", "repos": {"repo": entry}}))
    assert sync.main(["--config", str(config)]) == 1
    assert "[error] repo: github-standard.json: template_sync" in capsys.readouterr().out
