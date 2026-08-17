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

## Adding a new repo

Add an entry under `"repos"` in your `github-standard.json` — `{"profile": "hobby"}`
for a scratch repo, `{"profile": "baseline"}` once it's meant to take PRs (or `"oss"`
/ `"flagship"` if it matches one of those shapes), plus a `branches` block with
`extra_rules` for `required_status_checks` once it has real CI. Then
`./github-standard.py <repo> --apply`.
