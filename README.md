# dev-standards

Repo governance and engineering standards, applied consistently across every repo I
own — as opposed to hand-configuring each one and letting them drift.

## Bootstrapping a new repo

```bash
./init.py my-repo --apply                  # ~/projects/my-repo, autodetected
./init.py my-repo --path ~/other/dir --apply
```

One command instead of the manual sequence below: detects Python version,
dev-dependency style, and test presence from the target repo's own
`pyproject.toml`/layout; renders the four templates with those values filled
in; adds a `{"profile": "baseline"}` entry to `github-standard.json`; then
delegates to `github-standard.py --apply`. Dry run by default; every detected
value has an override flag (`--python`, `--dev-style`, `--tests`/`--no-tests`,
`--locked`, `--branch`). Refuses to overwrite a file that already exists
(`--force` to override) and never touches an already-registered repo's JSON
entry.

Required status checks are deliberately **not** part of this — a check
context has to be read off a real CI run and hand-confirmed (see
`docs/github-standard.md` § Required checks). Run `./init.py my-repo --checks`
once CI has gone green at least once; it prints the snippet to paste rather
than writing it, since the JSON entry is by then a nested structure this
script won't touch automatically.

The manual `cp` recipes below still work and are what `init.py` runs under
the hood — reach for them if you're adopting only one piece, or on a non-Python
repo `init.py` doesn't cover (see `shell-utils` for that shape).

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
