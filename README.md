# dev-standards

Repo governance and engineering standards, applied consistently across every repo I
own — as opposed to hand-configuring each one and letting them drift.

## GitHub repo config

```bash
cp github-standard.example.json github-standard.json   # fill in your real repos
./github-standard.py                                    # audit, dry run by default
./github-standard.py --apply                             # write
```

A declarative baseline (repo settings, security features, branch rulesets) plus an
idempotent audit/apply script. Full rationale: [docs/github-standard.md](docs/github-standard.md).

`github-standard.json` is gitignored — it necessarily names your real repos and org.
`github-standard.example.json` documents the schema with placeholder data.

## CI / pre-commit templates

```bash
cp templates/pre-commit/python.yaml <repo>/.pre-commit-config.yaml
cp templates/ci/python-ci.yml       <repo>/.github/workflows/ci.yml
```

Baseline for a Python repo with no CI yet (or a thin one) — lint (ruff) + test
(pytest), mirrored between local pre-commit and a required CI gate. Deliberately
narrow: no typecheck, no path filtering — see the comments at the top of each
template for when to add them. Copy and genericize, don't symlink — CI workflows
are repo-specific enough (dependency install command, test markers) that a template
needs a few `<FIXME>` spots filled in per repo, unlike the guideline files a sibling
tool (`push-guidelines.sh`, in `claude-dotfiles`) keeps byte-identical across repos.

## Dependabot

```bash
cp templates/dependabot/python.yml     <repo>/.github/dependabot.yml
cp templates/dependabot/automerge.yml  <repo>/.github/workflows/dependabot-automerge.yml
```

Version-update PRs (pip + pre-commit hook revs + GitHub Actions pins, weekly),
auto-merged when patch/minor and left for manual review on major. Distinct from the
vulnerability-alert/automated-security-fix settings `github-standard.py` already
turns on via the API — those are security-only; this is routine currency. Needs
`allow_auto_merge: true` at the repo level, already in the baseline settings.
`automerge.yml` is fully generic, copy verbatim; `python.yml` needs its `directory`
adjusted for anything but a single-package repo with `pyproject.toml` at the root.
