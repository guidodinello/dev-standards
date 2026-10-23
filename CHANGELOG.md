# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project bootstrap with dev-standards baseline
- `github-standard.py` manages the Actions "Approval for running fork pull request workflows from contributors" setting on public repos (`defaults.actions.fork_pr_contributor_approval`, default `all_external_contributors`, per-repo `actions_overrides`); private repos are skipped
- Dependabot templates (`templates/dependabot/{python,npm}.yml`, and this repo's own `.github/dependabot.yml`) wait 7 days after a release before proposing it (`cooldown: default-days: 7`; security updates are not delayed), and keep majors out of the dependency group (`update-types: [minor, patch]`) so a breaking major gets its own PR instead of blocking the safe ones
- Dependabot auto-merge (`templates/dependabot/automerge.yml`) gates on the PR author instead of `github.actor`, mints its App token with only `contents: write` and `pull-requests: write`, and no longer auto-merges `github-actions` bumps (reported in the job summary); the same author gate and a `contents: write`-only token apply to `sync-self-host-templates.yml`
- Every workflow (CI template, automerge, sync, and their checked-in copies) SHA-pins its actions and sets `persist-credentials: false` on checkout; tests enforce it for all rendered and checked-in workflows
- CI template gains an advisory, non-blocking `Audit workflows (advisory)` job running a pinned zizmor, outside the aggregate `CI` check

## [0.1.0] - YYYY-MM-DD

### Added
- Project scaffolded
