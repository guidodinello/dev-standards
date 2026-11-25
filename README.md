# dev-standards

Repo governance and engineering standards, applied consistently across every repo I
own — as opposed to hand-configuring each one and letting them drift.

The templates are a [Copier](https://copier.readthedocs.io/) template: `copier.yml`
plus `templates/copier/`, laid out as the files land in a repo (`.jinja` files are
rendered, the rest copied verbatim). This repo self-hosts its own baseline
(`.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, ...)
rendered from that template via `./init.py dev-standards --render-to`, the same as any
other repo onboarded with this tool. A repo bootstrapped now records the template
commit and its answers in `.copier-answers.yml`, and `./sync.py --apply` later opens a
PR on it whenever the template changes (§ Syncing template changes). Repos
bootstrapped before that (e.g. `herdr-routines`) have no answers file yet, so sync
skips them until they're onboarded: see [docs/template-sync.md](docs/template-sync.md).

Because Dependabot only scans checked-in files, its version bumps (a
`pre-commit-hooks` rev, a `checkout` SHA pin) never reach the `templates/copier/`
file they were rendered from, which `tests/test_self_host.py` then catches as
drift. `.github/workflows/sync-self-host-templates.yml` runs `sync_self_host.py`
on Dependabot's own PRs to propagate the bump automatically (reusing the same
GitHub App as `dependabot-automerge.yml`, for the same GITHUB_TOKEN-permissions reason —
see docs/github-standard.md § Dependabot); without that App configured, run
`./sync_self_host.py` by hand when that test goes red. The same applies to
`.github/workflows/release.yml`, a checked-in copy of the release template kept only
so Dependabot sees its SHA pins. This repo doesn't cut releases: don't push `v*` tags
here, since that workflow would run.

## Bootstrapping a new repo

```bash
./init.py my-repo --apply                  # ~/projects/my-repo, autodetected
./init.py my-repo --path ~/other/dir --apply
```

Run it from this checkout: the shebang is `uv run`, which runs it in this project's
environment, where Copier is a dependency (`uv run ./init.py` works the same).

One command instead of the manual sequence below: detects Python version,
dev-dependency style, and test presence from the target repo's own
`pyproject.toml`/layout; renders the Copier template with those values as its
answers (plus `.copier-answers.yml`); adds a `{"profile": "baseline"}` entry to `github-standard.json`; then
delegates to `github-standard.py --apply`. Dry run by default; every detected
value has an override flag (`--python`, `--dev-style`, `--tests`/`--no-tests`,
`--locked`, `--branch`); `--release` additionally renders the release workflow (see § Releases). Refuses to overwrite a file that already exists
(`--force` to override, except `CHANGELOG.md`, which is the repo's own once it exists)
and never touches an already-registered repo's JSON entry. It writes
`.copier-answers.yml` only when every other rendered file was written too: a file kept
because it already existed wasn't rendered from that template commit, so the answers
file would be wrong (`--force` renders them all; an existing `CHANGELOG.md` doesn't
count, Copier never renders over it either). With `--apply` it refuses a dirty
dev-standards checkout or a HEAD that isn't on `origin/main` (the answers file would
record a template commit the remote doesn't have); a dry run only warns. It also warns
when the checkout is behind the remote. `--render-to` renders whatever is checked out.

Required status checks are deliberately **not** part of this — a check
context has to be read off a real CI run and hand-confirmed (see
`docs/github-standard.md` § Required checks). Run `./init.py my-repo --checks`
once CI has gone green at least once; it prints the single `CI` context pinned
with `integration_id` 15368 (or the per-job contexts plus a hint, if the repo's CI
lacks the aggregate job) to paste rather than writing it, since the JSON entry is by then a nested structure this
script won't touch automatically. You paste it into your local, gitignored
`github-standard.json` — there is no PR for that step, and `--checks` needs the repo's
entry to exist already (it refuses an unregistered repo).

To adopt only one piece, render to a scratch dir and copy the file you want
(`./init.py my-repo --render-to /tmp/out --apply`), or `cp` a verbatim file straight
from `templates/copier/`. For a non-Python repo `init.py` doesn't cover, see
`shell-utils` for that shape.

## Syncing template changes

```bash
./sync.py                     # dry run: every repo in ~/projects/dev-standards/github-standard.json
./sync.py my-repo             # dry run: one registered repo
./sync.py my-repo --apply     # open (or update) that repo's sync PR
./sync.py --apply             # ... for every registered repo
./sync.py --config path/to/github-standard.json
```

Previews what the current template (dev-standards' `main` HEAD) would change in each
registered repo. For every repo with a `.copier-answers.yml` on its default branch it
makes a fresh shallow clone in a temp dir (`gh repo clone`, so private repos work), runs
`copier update`, and prints the diff. Each repo is reported as `not onboarded` (no
answers file, skipped), `unchanged`, `clean` (flagged "answers only" when only `_commit`
moved), `conflicts` (Copier's inline markers are left in the diff) or `error`.

When the repo's Dependabot and the template both bumped the same pin, the conflict is
resolved to the newer version, but only when the two sides differ in nothing but pins (a
pre-commit `rev`, or a SHA-pinned `uses:` with its `# vX` comment). Anything else stays
a conflict. Without `--apply` it is a dry run: it reads `github-standard.json`, clones,
and deletes the clones; it never creates a branch, pushes or opens a PR.

`--apply` opens one PR per repo with a real change, titled `chore(dev-standards): sync
templates to <template commit>`, from the branch `dev-standards/sync` into the repo's
default branch:

- The commit is made through GitHub's API (`createCommitOnBranch`), so GitHub signs
  it and rulesets that require signed commits accept it. The PR body links the
  template range, lists the changed files and any pins resolved, and links issue #53.
- `unchanged` and "answers only" repos get no PR.
- `conflicts` get a draft PR listing the conflicted files. Where drafts aren't
  available (private repos on GitHub Free), a normal PR titled `[conflicts] ...` that
  says not to merge; the markers keep CI red either way.
- Re-running is safe. An open sync PR that already holds the same result is left as
  is; if the template or the default branch moved, its branch is rebuilt as one fresh
  commit on the default branch and the PR updated, never a second PR. A sync branch
  with a commit `sync.py` didn't make (a conflict fix, GitHub's "Update branch") is
  never overwritten: merge or close that PR and delete the branch to sync again. A
  sync PR closed without merging isn't reopened for the same template commit.
- It needs gh's token to have the `workflow` scope (a sync usually edits
  `.github/workflows/`) and stops before writing anything if it doesn't
  (`gh auth refresh -s workflow`).

Exits 1 if any repo errored or its PR step failed; the other repos still run.

## GitHub repo config

```bash
cp github-standard.example.json github-standard.json   # fill in your real repos
./github-standard.py                                    # audit, dry run by default
./github-standard.py --apply                             # write
```

A declarative baseline (repo settings, security features, Actions fork PR approval,
branch rulesets) plus an idempotent audit/apply script. Full rationale: [docs/github-standard.md](docs/github-standard.md).

`github-standard.json` is gitignored — it necessarily names your real repos and org.
`github-standard.example.json` documents the schema with placeholder data.

## CI / pre-commit templates

`templates/copier/.pre-commit-config.yaml.jinja` and
`templates/copier/.github/workflows/ci.yml.jinja`, rendered by `init.py` (see above).

Baseline for a Python repo with no CI yet (or a thin one) — lint (ruff) + test
(pytest), mirrored between local pre-commit and a required CI gate. An aggregate
`CI` job `needs:` every other job, so the ruleset requires that one context
(pinned `integration_id` 15368) instead of a list that rots when jobs are renamed. Deliberately
narrow: no typecheck, no path filtering — see the comments at the top of each
template for when to add them. Rendered per repo, not symlinked: the Python version,
install command, branch and test job vary per repo (the Copier answers), unlike the
guideline files a sibling tool (`push-guidelines.sh`, in `claude-dotfiles`) keeps
byte-identical across repos.

## Releases

```bash
./init.py my-repo --release --apply      # adds .github/workflows/release.yml
cp templates/copier/.github/workflows/release.yml <repo>/.github/workflows/   # JS repos
```

Opt-in, for libraries and distributed apps only. A pushed `vX.Y.Z` tag becomes a GitHub
Release: the workflow checks the tag against the manifest version, takes the notes from
that version's `CHANGELOG.md` section, builds and attaches the artifacts (Python: wheel +
sdist via `uv build`; JS/extension: a `BUILD_COMMAND`/`ARTIFACTS` hook), with
`contents: write` on that one job only. The policy (SemVer, curated changelog,
`chore(release)` PR then annotated tag) is in `docs/github-standard.md` § Releases.

## CI runners

The CI and Dependabot auto-merge workflows (`templates/copier/.github/workflows/`) both read the
`CI_RUNNERS` repo variable to pick where their jobs run:

```yaml
runs-on: ${{ fromJSON(vars.CI_RUNNERS || '["ubuntu-latest"]') }}
```

Unset means GitHub-hosted `ubuntu-latest` — the default, no action needed. This exists
because a **private** repo's free GitHub-hosted minutes run out, and every job then
refuses to start with "recent account payments have failed or your spending limit
needs to be increased" (hit on `truco`). A public repo has unlimited free minutes and
needs none of this.

`dependabot-automerge.yml` follows the same variable rather than staying pinned to
`ubuntu-latest`: the billing block hits it exactly like any other job on a private
repo, and it only runs `dependabot/fetch-metadata` and `gh`, both of which work on
the homelab runner (`gh` and the actions' bundled node ship with the runner install).
An offline self-hosted runner just delays this job rather than blocking a merge —
`auto-merge` isn't in any repo's `required_status_checks`, and the job's own
`if: github.event.pull_request.user.login == 'dependabot[bot]'` already keeps human and fork PRs off it.

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
cp templates/copier/.github/dependabot.yml                      <repo>/.github/
cp templates/copier/.github/workflows/dependabot-automerge.yml  <repo>/.github/workflows/
```

Version-update PRs (uv + pre-commit hook revs + GitHub Actions pins, weekly),
auto-merged when patch/minor and left for manual review on major and for every
GitHub Actions bump (SHA pins, see `docs/github-standard.md` § Supply chain). Each version waits
7 days after release before Dependabot proposes it (`cooldown`), and majors arrive as
their own PRs instead of inside the group; security updates are never delayed.
Distinct from the
vulnerability-alert/automated-security-fix settings `github-standard.py` already
turns on via the API — those are security-only; this is routine currency. Needs
`allow_auto_merge: true` at the repo level, already in the baseline settings.
`dependabot-automerge.yml` is fully generic; `dependabot.yml` needs its `directory`
adjusted for anything but a single-package repo with `pyproject.toml` at the root.
