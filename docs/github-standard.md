# GitHub repo config standardization

`github-standard.py` applies a consistent baseline — repo settings, security
features, and a branch ruleset — across a set of GitHub repos, reading a declarative
JSON config as the source of truth. Dry run by default.

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

Repos with real CI additionally get `required_status_checks`, added at the branch
level (not the profile level — see § Profiles).

**A CI check you don't fully trust should never be a required check.** A free-tier
scanner whose quota exhaustion marks it failed/canceled (rather than skipped) would
then block every merge for a reason unrelated to the PR's content. Leave that
context out of `required_status_checks`.

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

## Required checks — never derived from CI

A required-check context is a job's *display name*, not its file or step name, and
picking the wrong one silently makes the required-check rule toothless. Read the
actual contexts off a recent commit —
`gh api repos/{org}/{repo}/commits/{branch}/check-runs --jq '[.check_runs[].name]'`
— then hand-write them into the config. Treat the config as authoritative afterward;
don't regenerate it from CI on every run.

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

`templates/dependabot/` has two files: `python.yml` (version updates — pip, pre-commit
hook revs, GitHub Actions pins, weekly) and `automerge.yml` (patch/minor auto-merge,
major stays manual). These are distinct from the vulnerability-alert/automated-security-fix
*settings* this script already manages via the API — those are security-only; the
templates are routine currency.

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

Without the App configured, `automerge.yml` degrades gracefully — it detects the
missing secret, reports "not configured" in the job summary, and leaves the PR for a
manual merge (which does trigger a deploy). It doesn't fail loudly, so a repo adopting
this template before the App is set up isn't broken, just not yet automated.

## Adding a new repo

For a Python repo taking the standard CI/pre-commit/Dependabot templates, use
`./init.py <repo> --apply` (see the README) — it detects the repo's Python
version, dev-dependency style, and test presence, renders all four templates,
adds a `{"profile": "baseline"}` entry to `github-standard.json`, and delegates
to `github-standard.py --apply` in one step. Then, once CI has gone green at
least once, `./init.py <repo> --checks` prints the `required_status_checks`
snippet to merge in by hand (see § Required checks above — never auto-derived).

For a scratch repo (`hobby`, no ruleset) or one that doesn't fit the Python
templates (an `"oss"`/`"flagship"` shape, or a different language entirely),
add the entry by hand: `{"profile": "hobby"}` or `{"profile": "baseline"}` (or
`"oss"`/`"flagship"`), plus a `branches` block with `extra_rules` for
`required_status_checks` once it has real CI. Then
`./github-standard.py <repo> --apply`.
