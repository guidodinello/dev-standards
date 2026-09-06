#!/usr/bin/env python3
"""Sync dependency-pin versions from this repo's own checked-in config files
into templates/, after Dependabot bumps only the checked-in copy.

Dependabot's pre-commit and github-actions ecosystems scan the checked-in
.pre-commit-config.yaml and .github/workflows/*.yml — they know nothing about
templates/, so a bump there never reaches the template it was rendered from.
tests/test_self_host.py fails loudly when that happens; this script is the
fix, applied automatically by .github/workflows/sync-self-host-templates.yml
on Dependabot's own PRs (see that workflow for why it can push at all).

Matches pins by identity (a pre-commit repo URL, or an action name before the
`@`) rather than by position, so it's safe to run even when templates/ has
unrelated text drift from the checked-in file (FIXME blocks, branch names).
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# `repo: URL` immediately followed by `rev: VERSION` — matches every hook
# entry in a .pre-commit-config.yaml regardless of which repo it is.
_PRE_COMMIT_REV = re.compile(r"(repo:\s*(\S+)\n\s*rev:\s*)(\S+)")

# `uses: owner/action@VERSION` — matches every action pin in a workflow file.
_ACTION_USES = re.compile(r"(uses:\s*([^@\s]+)@)(\S+)")

PAIRS = [
    (REPO_ROOT / ".pre-commit-config.yaml", REPO_ROOT / "templates/pre-commit/python.yaml"),
    (REPO_ROOT / ".github/workflows/ci.yml", REPO_ROOT / "templates/ci/python-ci.yml"),
    (
        REPO_ROOT / ".github/workflows/dependabot-automerge.yml",
        REPO_ROOT / "templates/dependabot/automerge.yml",
    ),
]


def _extract_pins(text: str, pattern: re.Pattern) -> dict[str, str]:
    return {m.group(2): m.group(3) for m in pattern.finditer(text)}


def _apply_pins(template_text: str, pattern: re.Pattern, pins: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        pin = pins.get(m.group(2))
        return f"{m.group(1)}{pin}" if pin else m.group(0)

    return pattern.sub(repl, template_text)


def sync_pins(checked_in_text: str, template_text: str) -> str:
    updated = template_text
    for pattern in (_PRE_COMMIT_REV, _ACTION_USES):
        updated = _apply_pins(updated, pattern, _extract_pins(checked_in_text, pattern))
    return updated


def main() -> int:
    changed = []
    for checked_in, template in PAIRS:
        if not checked_in.exists() or not template.exists():
            continue
        template_text = template.read_text()
        updated = sync_pins(checked_in.read_text(), template_text)
        if updated != template_text:
            template.write_text(updated)
            changed.append(template.relative_to(REPO_ROOT))

    for path in changed:
        print(f"synced: {path}")
    if not changed:
        print("no drift — templates already in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
