# GitHub repo config standardization

`github-standard.py` applies a consistent baseline — repo settings, security
features, Actions fork PR approval, and a branch ruleset — across a set of GitHub
repos, reading a declarative JSON config as the source of truth. Dry run by default.

```bash
./github-standard.py                  # audit every repo in the config, report drift
./github-standard.py --apply          # actually write
./github-standard.py <repo>           # one repo only
```

## Setup

The actual per-repo config (`github-standard.json`) is gitignored — it necessarily
lists real repo names, org names, and any per-repo exceptions, which isn't public
information even when the tool that reads it is. Copy the example and fill it in:

```bash
cp github-standard.example.json github-standard.json
```

See `github-standard.example.json` for the full schema (defaults, profiles, and
per-repo entries) with placeholder data.

## Why Python, not bash+jq

If you're pairing this with a bash-based file-sync tool elsewhere in your dotfiles:
that stays bash because rsync is the right tool for syncing *files*. This syncs
*structured API state* instead — ruleset bodies are JSON, GitHub returns fields in
its own order, and a correct diff has to merge partial per-rule overrides and
compare order-insensitively. That's native in Python and fragile in jq.

## The baseline

Applied to every repo, regardless of profile:

- `delete_branch_on_merge: true`, `allow_auto_merge: true`
- `allow_squash_merge: true`, `allow_merge_commit: false`, `allow_rebase_merge: false`
  (matches the ruleset's squash-only rule so the repo UI doesn't offer a method the
  ruleset would reject anyway)
- Dependabot vulnerability alerts + automated security fixes (free on public and
  private repos)
- Secret scanning + push protection **on public repos only** — these need GitHub
  Advanced Security on private repos and the API call errors without it, so the
  script checks `private` and skips the PATCH entirely rather than failing.
- Actions "Approval for running fork pull request workflows from contributors" set to
  `all_external_contributors` (`defaults.actions.fork_pr_contributor_approval`;
  override per repo with `actions_overrides`) — **public repos only**, see below.

**Fork PR approval: `all_external_contributors`, not GitHub's default.** GitHub
defaults to `first_time_contributors`, which stops requiring approval once *one* PR
from an author has been merged. A fork `pull_request` run executes the PR's own,
possibly modified, workflow files, so "land a harmless first PR, then open one with a
malicious CI change" gets free runner compute (crypto mining, network probing) with
no one approving it. Secrets aren't passed to fork runs and `GITHUB_TOKEN` is
read-only, so the risk is compute abuse, not secret theft; `pull_request_target` is
unaffected either way, since it always runs the base branch's workflow file. GitHub
added first-time-contributor approval in 2021 in response to exactly this kind of
crypto-mining abuse; requiring approval for every external contributor closes the
"second PR" gap it leaves. The setting
([GitHub docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository))
only governs public repos — private repos have a separate fork-PR-workflow policy
(`fork-pr-workflows-private-repos`) that this script doesn't manage, so it logs the
skip and moves on rather than reporting drift.

Repos on any profile but `hobby` additionally get a branch ruleset (rulesets, not
classic branch protection — GitHub's newer, evaluatable mechanism):

| Rule | Setting | Why |
|---|---|---|
| `deletion` | on | can't delete the branch |
| `non_fast_forward` | on | no force-push |
| `copilot_code_review` | `review_on_push: false` | Copilot reviews PRs, not every push |
| `required_signatures` | on | every commit GPG-signed |
| `pull_request` | squash-only, **0 required approvals**, thread resolution required | changes land through a PR, but nothing blocks a solo maintainer from merging their own |
| `code_quality` | `severity: errors` | GitHub's built-in code-quality check gates on errors, not warnings |
| `bypass_actors` | `[]` — none | no one, including the owner, can push straight to the branch; the escape hatch is flipping the ruleset to `evaluate`/`disabled` in the UI, not a bypass actor |

**`required_approving_review_count: 0` is load-bearing, not an oversight.** GitHub's
ruleset UI defaults this to `1`, which on a solo repo makes every PR permanently
unmergeable (no one else exists to approve it). The template hardcodes `0`.

Repos with real CI additionally get `required_status_checks` (the single pinned `CI`
context — see § Required checks), added at the branch level (not the profile level —
see § Profiles).

**A CI check you don't fully trust should never be a required check.** A free-tier
scanner whose quota exhaustion marks it failed/canceled (rather than skipped) would
then block every merge for a reason unrelated to the PR's content. Leave that
context out of `required_status_checks`.

### CI runners

A repo entry can declare `"ci_runners": [...]` — a list of GitHub Actions runner
labels — to manage the `CI_RUNNERS` Actions variable that `templates/ci/python-ci.yml`
and `templates/dependabot/automerge.yml` both read
(`runs-on: ${{ fromJSON(vars.CI_RUNNERS || '["ubuntu-latest"]') }}`). See
README § CI runners for what it's for and the manual `gh variable` equivalent.

Absent means unmanaged, not "unset": a repo with no `ci_runners` key is left alone
entirely, and this pass never deletes a variable a repo set by hand. There's no
declarative "unset" value — remove the key and run `gh variable delete` yourself.

## Profiles

A profile names a branch's review posture. Every branch resolves to one — set
`"profile"` on the branch, or on the repo (applies to all its branches), or omit it
entirely and get `baseline`. Four ship by default:

| Profile | Tier | Shape |
|---|---|---|
| `hobby` | `settings` | No ruleset at all — settings + security baseline only, for scratch repos pushed to directly |
| `baseline` | `ruleset` | The canonical shape above, as-is |
| `oss` | `ruleset` | + 1 required approval, code-owner review, admin bypass — for published repos taking external contributions |
| `flagship` | `ruleset` | + merge-commit-only (never squash), + `required_deployments` — for a release branch on a repo with a real deploy pipeline where a squash merge would break ancestry between two long-lived branches |

**Profiles cover review posture, not settings.** `flagship`'s merge-commit policy
usually also needs the repo-wide `allow_merge_commit` setting enabled — but that's a
repo setting, not branch-scoped, so it can't live in the profile. Put it in that
repo's `settings_overrides` instead.

**Layering, applied in order:** `defaults.ruleset` → profile (`rule_overrides` deep-merged
per rule type, `extra_rules` appended, `bypass_actors` replaced if set) → the branch's
own `rule_overrides`/`extra_rules`/`bypass_actors`, same merge rules. A branch can take
a profile and still add its own repo-specific quirks on top without those quirks
polluting the profile for every other repo that uses it.

**When to add a new profile vs. a one-off override:** once, an override on the one
repo that needs it (with a comment explaining why) is cheaper and clearer than an
abstraction serving a single consumer. Promote a shape into a named profile once a
*third* repo needs it.

## Required checks — one pinned `CI` context

Require a single context, the aggregate `CI` job from `templates/ci/python-ci.yml`
(job id `ci`, name `CI`, `needs:` every other job, `if: always()`, fails on any
failure or cancellation), pinned to the GitHub Actions app:

```json
{ "context": "CI", "integration_id": 15368 }
```

Per-job and per-matrix-leg contexts (`Tests (Python) (3.13)`) rot: rename a job or
change a matrix and the old context is never reported again, so the rule quietly
stops gating. `CI` is stable; a new job only needs adding to its `needs:` list.
`integration_id: 15368` (the Actions app) stops any other app or status from
satisfying the check by posting a result named `CI`.

A context is still a job's *display name*, read off a real run, not guessed —
`./init.py <repo> --checks` does this: it prints the single pinned `CI` entry when
the repo's `ci.yml` has the aggregate job, otherwise the per-job contexts plus a
hint to adopt it. Treat the config as authoritative afterward; don't regenerate it
from CI on every run. A repo's CI must gain the `CI` job *before* its ruleset
switches to requiring it, or every PR blocks on a check that never reports.
The order is: (1) merge the PR that adds the `CI` job to the repo's `ci.yml` and
let it report green on its default branch; (2) `./init.py <repo> --checks`
(the repo needs an entry in `github-standard.json` first — add a bare
`{"profile": "baseline"}` if it has none); (3) edit that entry in the local,
gitignored `github-standard.json` — not a PR; (4) `./github-standard.py <repo>`
as a dry run, review it, then `--apply`.

## No upsert — match by name, never blind-create

GitHub allows multiple rulesets with the same name on one repo, and there is no
upsert endpoint. `github-standard.py` lists existing rulesets, matches by name against
the config's branch keys (canonically the branch name itself, e.g. `main`), and only
then decides `PUT` (exists) vs. `POST` (create). A ruleset whose name doesn't match
any configured branch is reported as **unmanaged** and left alone — it is never
auto-renamed, because renaming changes what the ruleset *is* without discussion.
Bringing a legacy-named ruleset under management is a one-time manual step:

```bash
echo '{"name":"main"}' | gh api -X PUT repos/<org>/<repo>/rulesets/<id> --input -
```

Do this *before* running `--apply` on that repo — otherwise the script correctly sees
"no ruleset named `main`" and creates a second one, doubling enforcement.

## Failure handling

Each repo is independent: a failure on one (bad auth, a repo that's gone private,
rate-limiting) is reported and skipped, not a hard stop. The script exits non-zero if
anything failed, so CI or a human running it can tell partial success from a clean
run. Preflight checks `gh auth status` and that the active token carries the `repo`
scope before writing anything, so an auth problem surfaces once instead of as N
misleading per-repo 403s.

## Dependabot

`templates/dependabot/` has three files: `python.yml` (version updates — pip, pre-commit
hook revs, GitHub Actions pins, weekly), `npm.yml` (version updates for npm/pnpm/yarn
repos, plus a `security-updates` group so every currently-fixable vulnerability alert
lands as one PR instead of one per CVE — see the file's own header for why), and
`automerge.yml` (patch/minor auto-merge, major and github-actions bumps stay manual; ecosystem-agnostic, works
as-is for either language template). The version-update groups are distinct from the
vulnerability-alert/automated-security-fix *settings* this script already manages via
the API — those settings control whether Dependabot acts at all; the templates control
how its output is shaped (one bundled PR vs. many) once it does.

**Grouping doesn't unblock a stuck alert.** If a vulnerable package is pinned by a
transitive dependency that itself hasn't released a fix, Dependabot still can't open a
PR for it regardless of grouping — that still needs a manual bump of whatever pins it.

**Auto-merge needs a GitHub App, not just `GITHUB_TOKEN`.** A merge performed with
`GITHUB_TOKEN` triggers no downstream workflow runs at all — GitHub suppresses that to
prevent a workflow from triggering itself in an infinite loop. On a repo with a deploy
pipeline gated on push-to-`main`, that means an auto-merged Dependabot bump silently
never deploys: nothing fails, nothing reports a problem, the change just sits merged
but not shipped until an unrelated human push happens to carry it out. Caught this in
production — several merged bumps sat undeployed for days to weeks before it was
noticed.

One-time setup (the App can be shared across every repo that uses this template):

1. https://github.com/settings/apps/new — name it anything, homepage can be anything
   valid, **uncheck "Active" under Webhook** (not needed)
2. Repository permissions: **Contents: Read and write**, **Pull requests: Read and
   write** (merging a PR is categorized under the Contents permission, not Pull
   requests, despite what you'd expect)
3. Create the App, then generate a private key (downloads a `.pem`) and note the App ID
4. Install the App — **"Only select repositories"**, not "All repositories". This App
   can merge PRs; scope its blast radius to the repos that actually use it, and add
   more explicitly later rather than granting it every repo (including ones created
   after today) up front.
5. Per repo: **Settings → Secrets and variables → Dependabot** (not Actions — a
   Dependabot-triggered workflow can't read Actions secrets) → add `AUTOMERGE_APP_ID`
   and `AUTOMERGE_APP_PRIVATE_KEY` (the full `.pem` contents)

**github-actions bumps are never auto-merged.** Workflows are SHA-pinned (see
[Supply chain](#supply-chain)), so every patch/minor release of an action becomes a PR.
Auto-merging those would be an unreviewed path into code that runs on the CI runner
pool, and merging a PR that edits `.github/workflows/` with an App token likely also
needs the `workflows: write` permission, which is untested. `automerge.yml` skips them
(reporting why in the job summary); review the weekly grouped actions PR by hand. The
mint step requests only `contents: write` and `pull-requests: write` via `permission-*`
inputs, and the job gates on the PR author (`github.event.pull_request.user.login`), not
`github.actor`.

Without the App configured, `automerge.yml` degrades gracefully — it detects the
missing secret, reports "not configured" in the job summary, and leaves the PR for a
manual merge (which does trigger a deploy). It doesn't fail loudly, so a repo adopting
this template before the App is set up isn't broken, just not yet automated.

## Releases

Only repos where a release means something — a library others pin, an app that ships
installable artifacts — cut releases. An app that merely tracks a dependency (catan,
truco-py) doesn't, and doesn't adopt `templates/release/release.yml`.

- **Versioning:** [SemVer](https://semver.org). The manifest version (`pyproject.toml`
  `[project].version` or `package.json` `version`) is static and is the single source of
  truth for it.
- **Changelog:** a curated [Keep a Changelog](https://keepachangelog.com) `CHANGELOG.md`
  (`templates/github/CHANGELOG.md`) is the source of the release notes. Written by
  hand, not generated from commit messages — which is why release-please and
  semantic-release are deliberately not used.
- **Cutting a release:**
  1. Open a `chore(release): vX.Y.Z` PR: bump the manifest, move `[Unreleased]` under a
     new `## [X.Y.Z] - YYYY-MM-DD` section, update the compare links at the bottom.
  2. Squash-merge it.
  3. Create an **annotated** tag on the merge commit and push it:
     `git tag -a vX.Y.Z -m vX.Y.Z <merge-sha> && git push origin vX.Y.Z`.
- **Automation:** the tag push triggers the `Release` workflow, which verifies the tag
  equals the manifest version and sits on the default branch, uses that version's
  CHANGELOG section as the notes, builds the artifacts (Python: `uv build`, wheel +
  sdist; JS/extension: the `BUILD_COMMAND`/`ARTIFACTS` hook, e.g. pullscope's
  `wxt zip` for chrome + firefox), and creates the GitHub Release (a version with a
  letter or `-` is marked pre-release). `contents: write` is granted to that one job
  only. A failed verification publishes nothing: fix it, delete the tag
  (`git push origin :refs/tags/vX.Y.Z`), and re-tag.
- **Supply chain:** see [Supply chain](#supply-chain). The release workflow holds
  `contents: write`; its checked-in `.github/workflows/release.yml` exists only so
  Dependabot scans it, and `sync_self_host.py` propagates its bumps (SHA and tag
  comment) into the template.
- **Adopting:** `./init.py <repo> --release` adds the workflow for a Python repo; for
  JS repos `cp templates/release/release.yml` and swap the toolchain step and the two
  env values as the template's comments show. It is opt-in and not part of the default
  bootstrap.

## Supply chain

Every action in every workflow is pinned to a full commit SHA with the tag as a trailing
comment (`uses: actions/checkout@<sha> # v7`), and every `actions/checkout` sets
`persist-credentials: false`. CI's token is read-only, but the runner pool is switchable
(`CI_RUNNERS`) and may be self-hosted, so a hijacked tag would still execute code there.
Dependabot's `github-actions` ecosystem keeps SHA and comment in step, and its PRs are
reviewed by hand (see [Dependabot](#dependabot)). `tests/test_init.py` fails if any
rendered or checked-in workflow has an unpinned `uses:` or a checkout that persists
credentials.

`templates/ci/python-ci.yml` includes an **advisory** `Audit workflows (advisory)` job
running a pinned `zizmor`. It is deliberately outside the `CI` job's `needs:` and its
step is `continue-on-error`, so findings never block a merge. Move it to blocking once it
has run clean for a few weeks; bump its pinned version by hand (Dependabot does not track
`uvx` pins).

## Adding a new repo

For a Python repo taking the standard CI/pre-commit/Dependabot templates, use
`./init.py <repo> --apply` (see the README) — it detects the repo's Python
version, dev-dependency style, and test presence, renders all four templates,
adds a `{"profile": "baseline"}` entry to `github-standard.json`, and delegates
to `github-standard.py --apply` in one step. Then, once CI has gone green at
least once, `./init.py <repo> --checks` prints the `required_status_checks`
snippet to merge in by hand (the single `CI` context pinned with
`integration_id` 15368 — see § Required checks above). That edit goes into your
local, gitignored `github-standard.json`: there is no PR for it, so make it in the
checkout you run `./github-standard.py` from (keep that checkout on current `main`,
or the script and `github-standard.example.json` it audits against will be stale).

For a scratch repo (`hobby`, no ruleset) or one that doesn't fit the Python
templates (an `"oss"`/`"flagship"` shape, or a different language entirely),
add the entry by hand: `{"profile": "hobby"}` or `{"profile": "baseline"}` (or
`"oss"`/`"flagship"`), plus a `branches` block with `extra_rules` for
`required_status_checks` (the pinned `CI` context, once its CI has the aggregate
job) once it has real CI. Then
`./github-standard.py <repo> --apply`.
