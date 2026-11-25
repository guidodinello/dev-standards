from conftest import REPO_ROOT, import_module_from_path

sync_self_host = import_module_from_path("sync_self_host", REPO_ROOT / "sync_self_host.py")


def test_sync_pins_updates_pre_commit_rev():
    checked_in = "  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.16.5\n"
    template = (
        "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
        "    rev: v0.16.3\n"
        "    hooks:\n"
        "      - id: ruff-format\n"
    )
    updated = sync_self_host.sync_pins(checked_in, template)
    assert "rev: v0.16.5" in updated
    assert "rev: v0.16.3" not in updated
    assert "      - id: ruff-format\n" in updated  # unrelated lines untouched


def test_sync_pins_updates_action_uses():
    checked_in = "      - uses: actions/checkout@v8\n"
    template = "      - uses: actions/checkout@v7\n"
    updated = sync_self_host.sync_pins(checked_in, template)
    assert updated == "      - uses: actions/checkout@v8\n"


def test_sync_pins_updates_sha_pin_and_its_tag_comment():
    checked_in = "      - uses: actions/checkout@" + "b" * 40 + "  # v8\n"
    template = "      - uses: actions/checkout@" + "a" * 40 + " # v7\n"
    updated = sync_self_host.sync_pins(checked_in, template)
    assert updated == checked_in
    assert "# v7" not in updated


def test_sync_pins_tag_pin_does_not_swallow_following_lines():
    checked_in = "      - uses: actions/checkout@v8\n        with:\n          x: 1\n"
    template = "      - uses: actions/checkout@v7\n        with:\n          x: 1\n"
    assert sync_self_host.sync_pins(checked_in, template) == checked_in


def test_sync_pins_ignores_pins_absent_from_checked_in():
    # A repo/action only present in templates/ (not yet adopted downstream)
    # must be left alone — sync only touches keys it can actually verify.
    checked_in = "  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.16.5\n"
    template = (
        "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
        "    rev: v0.16.3\n"
        "  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
        "    rev: v6.0.0\n"
    )
    updated = sync_self_host.sync_pins(checked_in, template)
    assert "https://github.com/pre-commit/pre-commit-hooks\n    rev: v6.0.0" in updated


def test_main_is_idempotent_against_repos_own_files():
    assert sync_self_host.main() == 0
    for _, templates in sync_self_host.PAIRS:
        for template in templates:
            before = template.read_text()
            sync_self_host.main()
            assert template.read_text() == before


def test_main_syncs_a_bump_into_every_template_of_a_pair(tmp_path, monkeypatch):
    """A Dependabot bump in a checked-in file must reach the plain template and its
    Copier twin alike; otherwise the Copier parity test goes red on every bump PR."""
    checked_in = tmp_path / "ci.yml"
    checked_in.write_text("      - uses: actions/checkout@v8\n")
    plain = tmp_path / "python-ci.yml"
    twin = tmp_path / "ci.yml.jinja"
    for template in (plain, twin):
        template.write_text("      - uses: actions/checkout@v7\n")
    monkeypatch.setattr(sync_self_host, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sync_self_host, "PAIRS", [(checked_in, (plain, twin))])
    sync_self_host.main()
    assert plain.read_text() == twin.read_text() == checked_in.read_text()


def test_every_copier_template_is_a_sync_target():
    targets = {t for _, templates in sync_self_host.PAIRS for t in templates}
    twins = set((REPO_ROOT / "templates" / "copier").rglob("*.jinja"))
    pinned = {t for t in twins if "uses:" in t.read_text() or "rev:" in t.read_text()}
    assert pinned
    assert pinned <= targets
