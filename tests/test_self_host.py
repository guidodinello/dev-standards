"""Keeps this repo's own checked-in config files honest against templates/copier/.

This repo bootstraps ITSELF from its own Copier template (see README § self-hosting),
with the answers in conftest.SELF_HOST_ANSWERS. Without this test, Dependabot
bumping a pin in the root .pre-commit-config.yaml (or ci.yml) would silently
diverge from the template — the exact "possibly-outdated pinned versions baked into
templates/" problem this baseline was meant to fix, just relocated. When this test
goes red, propagate the bump into templates/copier/ too (./sync_self_host.py).
"""

import pytest
from conftest import REPO_ROOT, rendered

SELF_HOST_FILES = [
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    ".github/dependabot.yml",
    ".github/workflows/dependabot-automerge.yml",
    # Checked in only so Dependabot scans its SHA pins; this repo cuts no releases.
    ".github/workflows/release.yml",
    ".github/CODEOWNERS",
]


@pytest.mark.parametrize("rel", SELF_HOST_FILES)
def test_checked_in_file_matches_template(rel):
    assert (REPO_ROOT / rel).read_text() == rendered()[rel]
