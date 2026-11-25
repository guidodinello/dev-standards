# Template sync with Copier (issue #53)

Before Copier, `init.py` rendered `templates/` once, at bootstrap, and a template
fix never reached a repo rendered before it. With [Copier](https://copier.readthedocs.io/)
each repo records the template commit it was rendered from and its answers in
`.copier-answers.yml`, and `copier update` applies the template's change since
that commit to the repo's current files as a 3-way merge.

## Where it stands

`init.py` renders every file through Copier (PR 2). Nothing runs `copier update`
on existing repos yet (PR 3/4), and repos bootstrapped before PR 2 have no answers
file (PR 5).

```
init.py ──detection as answers──▶ copier run_copy(this checkout, vcs_ref=HEAD)
copier.yml ──_subdirectory──▶ templates/copier/
   .pre-commit-config.yaml.jinja
   .github/workflows/ci.yml.jinja
   .github/dependabot.yml                       verbatim
   .github/workflows/dependabot-automerge.yml   verbatim
   .github/workflows/release.yml                verbatim; _exclude unless `release`
   .github/CODEOWNERS                           verbatim
   CHANGELOG.md                                 verbatim; _skip_if_exists
   [= _copier_conf.answers_file =].jinja        → .copier-answers.yml
```

- **Answers:** `python_version`, `dev_style`, `locked`, `include_tests`, `branch`,
  `release`. `install_cmd` is computed in `copier.yml` and not stored.
- **`_src_path`:** `init.py` rewrites it in the answers file to
  `https://github.com/guidodinello/dev-standards.git`, so `copier update` works from
  any machine, not only from the checkout that rendered it.
- **`_commit`:** to keep it a commit the remote has, `init.py` refuses to bootstrap
  a real repo from a dirty checkout or from a HEAD that isn't on `origin/main`.
  `--render-to` renders whatever is checked out.
- **Running it:** Copier is a project dependency, so `init.py` runs in the project
  environment: `./init.py` (shebang `uv run`) or `uv run ./init.py`, from this
  checkout.

The Jinja delimiters are `[= =]`, `[% %]` and `[# #]`. The defaults won't do:
`{{ }}` collides with GitHub's `${{ }}`, and `[[ ]]` collides with bash's `[[`
in the `CI` job. A test checks that no verbatim file under `templates/` contains them.

Try an update by hand:

```bash
cd <repo> && uv run --project <dev-standards> copier update --defaults --conflict inline
```

## Spike findings

Verified with Copier 9.19.1. Each row is locked in by a test in `tests/test_copier_template.py`.

| Case | Result |
|---|---|
| The repo edited a line, and the template changed a different part of the file | Merged. Both changes are kept. |
| Dependabot bumped a pin in the repo, and the template left that pin alone | The repo's pin is kept, not rolled back. |
| Both sides bumped the same pin to different versions | Inline conflict markers (`<<<<<<< before updating` / `>>>>>>> after updating`). |
| Both sides bumped the same pin to the same version | Clean. |
| The destination has uncommitted changes | Refused ("Destination repository is dirty"). |
| The template has no tags | `update` goes to HEAD. `_commit` records the short SHA, or `git describe` output once a tag exists. |
| The template has a non-PEP 440 tag (`template-v1`) | Ignored for version selection. It only shows up in `_commit`. |
| Commit signing is on globally | Copier's internal commits pass `--no-gpg-sign`. No prompt. |

Merging works line by line. In the spike, edits on directly adjacent lines
conflicted. Edits one or more lines apart from a template change merged
cleanly.

`_exclude` patterns are rendered with Jinja (`match_exclude` in
`copier/_main.py`), so an answer can drive a per-repo skip list. This hasn't been
tested yet; PR5 adds the test.

## Open questions: decisions

1. **Pin conflicts.** Decided (2026-11-25). Copier marks them as conflicts (above), so
   `sync.py` gets a post-update step that resolves a conflict hunk automatically
   only when the two sides differ in nothing but pins. It reuses
   `sync_self_host`'s `_PRE_COMMIT_REV` / `_ACTION_USES` patterns, and the
   higher version wins. A SHA pin is compared by its `# vX` comment. Any other
   conflict stays inline, and the PR opens as a draft, so the markers fail CI
   instead of merging.
2. **Onboarding existing repos.** Decided (2026-11-25). Write `.copier-answers.yml` from `init.py`'s
   detection. Set `_commit` to the dev-standards commit whose render differs
   least from the repo's current files. Commit only that file, then run a normal
   `update`. A repo that diverged on purpose (fitted) opts out of files through a
   `skip` answer that feeds a templated `_exclude`.
3. **Release-gated syncs.** Decided (2026-11-25): no. Sync to `main` HEAD, which is already CI-gated.
   Tags would add ceremony for a single owner, and `v*` tags can't be used here
   anyway (`release.yml` fires on them). If gating is ever wanted, use PEP 440
   tags without the `v` (e.g. `2026.11.25`).

## Roadmap

| PR | Scope |
|---|---|
| 1 | The spike (#54). |
| 2 | Done: `init.py` renders through Copier. The answers come from the existing detection, and the CLI flags stay. The plain CI and pre-commit templates are removed, and the other files (dependabot, automerge, CODEOWNERS, CHANGELOG via `_skip_if_exists`, opt-in release) move in. Copier is a project dependency (uv.lock, bumped by Dependabot), so `init.py` runs through `uv run` (its shebang), no longer stdlib-only. |
| 3 | `sync.py`, dry run: for each repo in `github-standard.json` that has `.copier-answers.yml`, run `copier update` in a fresh clone and print the diff and any conflicts. Plus the pin post-step. |
| 4 | `sync.py --apply`: a branch, a signed commit (GraphQL `createCommitOnBranch`), and a PR titled `chore(dev-standards): sync templates to <ref>`. The README stops saying templates never re-apply. |
| 5 | `sync.py adopt <repo>` for onboarding. Then roll out in waves: system-one, the small Python repos, fitted. |
