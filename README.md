# dev-standards

Repo governance and engineering standards, applied consistently across every repo I
own — as opposed to hand-configuring each one and letting them drift.

This repo self-hosts its own baseline (`pyproject.toml`, `.pre-commit-config.yaml`,
`.github/workflows/ci.yml`, `.github/dependabot.yml`) rendered from its own
`templates/` via `./init.py dev-standards --render-to`, the same as any other repo
onboarded with this tool. Repos already bootstrapped from an earlier version of
`templates/` (e.g. `herdr-routines`) are unaffected — `init.py` only renders at
bootstrap time and never re-applies itself to a repo later, so picking up template
changes there is a manual re-run, not automatic.

Because Dependabot only scans checked-in files, its version bumps (a
`pre-commit-hooks` rev, a `checkout@vN` pin) never reach the `templates/` copy
they were rendered from, which `tests/test_self_host.py` then catches as
drift. `.github/workflows/sync-self-host-templates.yml` runs `sync_self_host.py`
on Dependabot's own PRs to propagate the bump automatically (reusing the same
GitHub App as `automerge.yml`, for the same GITHUB_TOKEN-permissions reason —
see docs/github-standard.md § Dependabot); without that App configured, run
`./sync_self_host.py` by hand when that test goes red.

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

## CI runners

`templates/ci/python-ci.yml` and `templates/dependabot/automerge.yml` both read the
`CI_RUNNERS` repo variable to pick where their jobs run:

```yaml
runs-on: ${{ fromJSON(vars.CI_RUNNERS || '["ubuntu-latest"]') }}
```

Unset means GitHub-hosted `ubuntu-latest` — the default, no action needed. This exists
because a **private** repo's free GitHub-hosted minutes run out, and every job then
refuses to start with "recent account payments have failed or your spending limit
needs to be increased" (hit on `truco`). A public repo has unlimited free minutes and
needs none of this.

`automerge.yml` follows the same variable rather than staying pinned to
`ubuntu-latest`: the billing block hits it exactly like any other job on a private
repo, and it only runs `dependabot/fetch-metadata` and `gh`, both of which work on
the homelab runner (`gh` and the actions' bundled node ship with the runner install).
An offline self-hosted runner just delays this job rather than blocking a merge —
`auto-merge` isn't in any repo's `required_status_checks`, and the job's own
`if: github.actor == 'dependabot[bot]'` already keeps human and fork PRs off it.

Point CI at the homelab HP:

```bash
gh variable set CI_RUNNERS -R guidodinello/<repo> --body '["self-hosted","linux","x64"]'
```

Go back to GitHub-hosted:

```bash
gh variable delete CI_RUNNERS -R guidodinello/<repo>
```

Declaratively, `github-standard.py` can manage the same variable via a repo's
optional `"ci_runners": [...]` key — see `docs/github-standard.md` § CI runners.
It only ever writes a variable a repo explicitly declares; it never unsets one.

### Registering a runner on the homelab HP

**Self-hosted runners on a personal GitHub account are registered per repository —
there's no org-level pool to share.** Do this once per repo that needs it:

```bash
# from this machine — mints a short-lived registration token
gh api -X POST repos/guidodinello/<repo>/actions/runners/registration-token -q .token
```

```bash
# on the HP (ssh hp) — matches the naming of the runners already registered
# on fitted (homelab-hp, homelab-hp-2) and weekly-highlights (homelab-hp-weekly-highlights)
mkdir -p ~/actions-runner-<repo> && cd ~/actions-runner-<repo>
curl -o runner.tar.gz -L https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/guidodinello/<repo> --token <TOKEN> \
            --name homelab-hp-<repo> --labels homelab,hp --work _work --unattended
sudo ./svc.sh install "$USER" && sudo ./svc.sh start
```

`self-hosted`, `Linux`, and `X64` are added automatically as read-only labels; the
`--labels` flag above only adds the extra custom ones. Labels match
case-insensitively, which is why `CI_RUNNERS`'s lowercase `"linux"`/`"x64"` still hit a
runner registered with `Linux`/`X64`.

Caveats, in order of how likely they are to actually bite:

- **Private repos only.** A self-hosted runner on a public repo lets a fork's PR run
  arbitrary code on the box. Every repo this applies to is private.
- **An offline runner queues CI forever, with no timeout.** If the HP (or its network
  path) is down while `CI_RUNNERS` is set, every PR blocks indefinitely rather than
  falling back — `gh variable delete` is the escape hatch, not a workflow retry.
- **Start the runner from a clean shell, not one with a venv activated.** The runner
  inherits the environment of whatever process started it. A leaked `VIRTUAL_ENV`
  silently redirects `uv pip install` into the wrong environment inside every job that
  takes the optional-dependencies branch of this template's install step — install and
  start the service (`svc.sh`) from a plain login shell, never from inside an
  activated `.venv`.
- **The uv cache lives on the host, under the account that runs the service**
  (`setup-uv` sets `UV_CACHE_DIR` to `~/.cache/uv` there and prunes it) — it's shared
  and pruned across every repo's runner on that account, not sandboxed per repo.

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
