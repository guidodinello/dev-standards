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
    for _, template in sync_self_host.PAIRS:
        before = template.read_text()
        sync_self_host.main()
        assert template.read_text() == before
