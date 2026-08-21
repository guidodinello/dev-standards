#!/usr/bin/env python3
"""init — bootstrap a Python repo onto the dev-standards baseline in one step.

Why: `github-standard.py` applies GitHub *API* state (settings, security,
branch rulesets) from `github-standard.json`, but refuses any repo that isn't
already a key in that file. `templates/` holds the CI/pre-commit/Dependabot
*files*, but the README only tells you to `cp` them by hand and fill in two
<FIXME> spots. Nothing joins the two — onboarding a repo is four `cp`s, two
hand-edits, a hand-written JSON entry, then a run of github-standard.py. That
friction is what produced commit f44f66a ("docs(template): note two gotchas
found rolling out to rl-tournament-notification-bot"). This closes the gap.

Detection is read from the target repo itself (pyproject.toml, tests/, docs/)
so the four <FIXME>/version spots in the templates get filled automatically;
every detected value has an override flag. Required status
checks are deliberately NOT part of the one-step flow: docs/github-standard.md
is explicit that a required-check context is a job's display name, read off a
real CI run and hand-confirmed — never auto-derived. Run --checks after the
first green CI run to add them.

`github-standard.json` is gitignored, hand-aligned, and carries real
rationale in its comments — this never does a json.loads/json.dumps
round-trip on it (that would reflow the whole file with no git diff to
review the damage). It only ever splices one new line after `"repos": {`.

Usage:
    ./init.py my-repo                          # dry run, ~/projects/my-repo
    ./init.py my-repo --apply                  # write files + JSON entry, delegate
    ./init.py my-repo --path ~/other/dir --apply
    ./init.py my-repo --checks                 # phase 2: after first green CI —
                                                # prints a snippet to paste, never writes
    ./init.py my-repo --render-to /tmp/out      # render only, for diffing — no writes elsewhere

Requires: `gh` CLI, authenticated with a token that has the `repo` scope
(same preflight as github-standard.py, run only when talking to the API).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
NC = "\033[0m"

REPO_ROOT = Path(__file__).parent
TEMPLATES = REPO_ROOT / "templates"
CONFIG_PATH = REPO_ROOT / "github-standard.json"


def log_info(msg: str) -> None:
    print(f"{GREEN}[INFO]{NC}  {msg}")


def log_warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{NC}  {msg}")


def log_err(msg: str) -> None:
    print(f"{RED}[FAIL]{NC}  {msg}")


# ── gh API (read-only calls only — this script never writes GitHub state
# directly; the ruleset/settings writes stay entirely inside
# github-standard.py, which owns that logic) ────────────────────────────────


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        log_err("`gh` CLI not found on PATH. Install it and authenticate before running init.py.")
        sys.exit(1)


def gh_get(path: str, *, paginate: bool = False) -> dict | list | None:
    cmd = ["api", path] + (["--paginate"] if paginate else [])
    proc = _run_gh(cmd)
    if proc.returncode != 0:
        return None
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


def preflight_gh_auth() -> None:
    """Run before anything else that shells out to gh — get_default_branch
    used to run first, so a bad/missing auth surfaced as a mid-detection
    warning instead of failing here, up front, before any writes."""
    proc = _run_gh(["auth", "status"])
    if proc.returncode != 0:
        log_err("gh auth status failed — not logged in. Aborting before any writes.")
        sys.exit(1)


# ── detection ────────────────────────────────────────────────────────────


def load_pyproject(repo_root: Path) -> dict:
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        log_err(
            f"{repo_root} has no pyproject.toml — init.py only bootstraps Python "
            "repos. (shell-utils is the shape a shell-flavored template would need; "
            "not built here — see the plan's 'explicitly out of scope'.)"
        )
        sys.exit(1)
    with pyproject_path.open("rb") as f:
        return tomllib.load(f)


def detect_python_version(pyproject: dict) -> str:
    """PEP 508 specifier clauses are unordered (`<4.0,>=3.11` is as valid as
    `>=3.11,<4.0`), so scanning the raw string for the first digit.digit
    pattern can pick up the upper-bound clause instead of the lower bound.
    Parse clause-by-clause and take the lower bound (>=, ==, or ~=) instead."""
    requires = pyproject.get("project", {}).get("requires-python", "")
    for clause in requires.split(","):
        clause = clause.strip()
        m = re.match(r"(>=|==|~=)\s*(\d+)\.(\d+)", clause)
        if m:
            return f"{m.group(2)}.{m.group(3)}"
    log_err(
        f"Couldn't find a >=/==/~= lower-bound clause in requires-python={requires!r}. "
        "Pass --python."
    )
    sys.exit(1)


def _dep_name(spec: str) -> str:
    """Bare package name from a PEP 508 dependency spec, e.g.
    'pytest-cov>=4.0' -> 'pytest-cov'. Needed so a substring check like
    "pytest" in dep doesn't false-match "pytest-cov"/"pytest-asyncio"/etc."""
    return re.split(r"[<>=!~\[\s;]", spec, maxsplit=1)[0].strip().lower()


def _dev_deps(pyproject: dict, style: str) -> list[str]:
    if style == "groups":
        return pyproject.get("dependency-groups", {}).get("dev", [])
    return pyproject.get("project", {}).get("optional-dependencies", {}).get("dev", [])


def detect_dev_style(pyproject: dict) -> str:
    """[dependency-groups] wins if both exist — claude-client declares both,
    and [dependency-groups] is what the template's default `uv sync --dev`
    actually reads."""
    has_groups = "dev" in pyproject.get("dependency-groups", {})
    has_optional = "dev" in pyproject.get("project", {}).get("optional-dependencies", {})
    if has_groups:
        return "groups"
    if has_optional:
        return "optional"
    log_warn(
        "No [dependency-groups] dev or [project.optional-dependencies] dev found — "
        "defaulting to 'groups' (uv sync --dev). Pass --dev-style if that's wrong."
    )
    return "groups"


def install_cmd_for(style: str, locked: bool) -> str:
    if style == "groups":
        return "uv sync --locked --dev" if locked else "uv sync --dev"
    return 'uv pip install -e ".[dev]"'


def detect_tests(repo_root: Path, pyproject: dict, dev_style: str) -> bool:
    """Checked against dev_style's own dep list, not both styles: dev_style
    is exactly what install_cmd_for() will install, so pytest declared only
    under the *other* style genuinely won't be on PATH — excluding the test
    job in that case is correct, not a detection miss."""
    has_dir = (repo_root / "tests").is_dir()
    has_pytest = any(_dep_name(dep) == "pytest" for dep in _dev_deps(pyproject, dev_style))
    return has_dir and has_pytest


def detect_docs(repo_root: Path) -> bool:
    return (repo_root / "docs").is_dir()


def get_default_branch(org: str, repo: str) -> str:
    obj = gh_get(f"repos/{org}/{repo}")
    if obj and isinstance(obj, dict) and obj.get("default_branch"):
        return obj["default_branch"]
    log_warn(f"Couldn't read {org}/{repo}'s default branch from the API — assuming 'main'.")
    return "main"


# ── template rendering — read the real template files, don't duplicate their
# content in this script (single source of truth stays templates/) ─────────


def _must_replace(text: str, old: str, new: str) -> str:
    """Like str.replace, but fails loudly if `old` isn't present. templates/
    is edited independently of this script — if a FIXME's wording or a
    version literal there ever changes, a silent no-op here would ship a
    stale <FIXME>/version literal with exit code 0."""
    if old not in text:
        raise RuntimeError(
            f"Expected template text not found — templates/ may have changed:\n{old!r}"
        )
    return text.replace(old, new)


def render_pre_commit(version: str) -> str:
    text = (TEMPLATES / "pre-commit" / "python.yaml").read_text()
    version_nodot = version.replace(".", "")
    text = _must_replace(text, "python3.13", f"python{version}")
    text = _must_replace(text, "--py313-plus", f"--py{version_nodot}-plus")
    return text


_FIXME_INSTALL_BLOCK = (
    "      # <FIXME> uv sync --dev assumes a [dependency-groups] dev group in\n"
    "      # pyproject.toml. If this repo instead uses [project.optional-dependencies],\n"
    '      # swap to: uv pip install -e ".[dev]"\n'
    "      - name: Install dependencies\n"
    "        run: uv sync --dev"
)
_FIXME_TEST_BLOCK = (
    "      # <FIXME> adjust the test path/markers to this repo's layout\n"
    '      # (e.g. `-m "not integration"` if it separates unit from integration).\n'
    "      - name: Run tests\n"
    "        run: uv run pytest"
)
_NO_TESTS_COMMENT = (
    '# No "Tests (Python)" job yet — this repo has no test suite. Add one (pytest\n'
    "# as a dev dependency, a tests/ directory) then bring that job back from the\n"
    "# template and add its context to required_status_checks in github-standard.json.\n"
)


def render_ci(*, install_cmd: str, branch: str, include_tests: bool) -> str:
    text = (TEMPLATES / "ci" / "python-ci.yml").read_text()

    resolved_install = f"      - name: Install dependencies\n        run: {install_cmd}"
    text = _must_replace(text, _FIXME_INSTALL_BLOCK, resolved_install)
    # The test job's own (uncommented) install line, only reached if that
    # job survives below — resolve it too so both jobs use the same command.
    text = _must_replace(text, "        run: uv sync --dev", f"        run: {install_cmd}")
    text = _must_replace(
        text,
        _FIXME_TEST_BLOCK,
        "      - name: Run tests\n        run: uv run pytest",
    )
    text = _must_replace(text, "branches: [main]", f"branches: [{branch}]")

    if not include_tests:
        anchor = "\n  test-python:"
        if anchor not in text:
            raise RuntimeError(
                f"Expected {anchor!r} job marker not found — templates/ may have changed"
            )
        before, _, _ = text.partition(anchor)
        text = before.rstrip("\n") + "\n"
        text = _must_replace(
            text,
            '# then add "Lint Python" and "Tests (Python)" as required_status_checks\n',
            '# then add "Lint Python" as a required_status_checks context (no test job\n'
            "# below yet — see the note under jobs:)\n",
        )
        text = _must_replace(text, "jobs:\n", f"jobs:\n{_NO_TESTS_COMMENT}")

    return text


def render_dependabot() -> str:
    return (TEMPLATES / "dependabot" / "python.yml").read_text()


def render_automerge() -> str:
    return (TEMPLATES / "dependabot" / "automerge.yml").read_text()


# ── writing ──────────────────────────────────────────────────────────────


def write_rendered(path: Path, content: str, *, apply: bool, force: bool) -> bool:
    """Returns True if the file was (or would be) written."""
    if path.exists() and not force:
        log_warn(f"{path} already exists — skipping (pass --force to overwrite)")
        return False
    verb = "would write" if not apply else "writing"
    log_info(f"{verb}: {path}")
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return True


def _align_entry_line(repo: str, profile: str, column: int = 38) -> str:
    key = f'    "{repo}":'
    pad = " " * max(1, column - len(key))
    return f'{key}{pad}{{ "profile": "{profile}" }},\n'


def splice_repos_entry(repo: str, profile: str, *, apply: bool) -> None:
    """Text-anchored insertion only — never json.loads/json.dumps the whole
    file back out. That would reflow every hand-aligned entry and every
    _comment in a gitignored file with no git diff to review it."""
    config = json.loads(CONFIG_PATH.read_text())
    if repo in config.get("repos", {}):
        log_info(f"{CONFIG_PATH.name}: '{repo}' already has a repos entry — leaving it untouched")
        return

    raw = CONFIG_PATH.read_text()
    anchor = '"repos": {'
    idx = raw.find(anchor)
    if idx == -1:
        log_err(
            f"Could not find the literal `{anchor}` anchor in {CONFIG_PATH.name} — not touching it."
        )
        sys.exit(1)
    insert_at = raw.index("\n", idx) + 1
    new_line = _align_entry_line(repo, profile)

    verb = "would add" if not apply else "adding"
    log_info(f"{verb} {CONFIG_PATH.name} entry: {new_line.strip()}")
    if apply:
        CONFIG_PATH.write_text(raw[:insert_at] + new_line + raw[insert_at:])


# ── ruleset pre-flight (read-only) ──────────────────────────────────────


def preflight_ruleset(org: str, repo: str, branch: str) -> None:
    """A ruleset whose name doesn't match the branch key would make
    github-standard.py create a *second* ruleset instead of updating the
    existing one (no upsert endpoint — see docs/github-standard.md § No
    upsert). But that's only a real risk if the mismatched ruleset actually
    targets our branch under the wrong name — a repo with correctly-named
    `main` and `development` rulesets isn't a conflict for either one, and
    docs/github-standard.md § No upsert explicitly blesses unmanaged
    rulesets targeting other branches as fine to leave alone."""
    existing = gh_get(f"repos/{org}/{repo}/rulesets")
    if not isinstance(existing, list):
        return  # repo has no rulesets API access yet (e.g. doesn't exist), nothing to check
    if any(rs.get("name") == branch for rs in existing):
        return  # github-standard.py will PUT-update this one — no duplicate risk

    target_ref = f"refs/heads/{branch}"
    for rs in existing:
        detail = gh_get(f"repos/{org}/{repo}/rulesets/{rs.get('id')}")
        if not isinstance(detail, dict):
            continue
        includes = detail.get("conditions", {}).get("ref_name", {}).get("include", [])
        if target_ref in includes:
            log_err(
                f"{repo} already has a ruleset named '{rs.get('name')}' (id {rs.get('id')}) "
                f"that targets {target_ref} under the wrong name. Rename it first — "
                "otherwise github-standard.py will create a second ruleset:\n"
                f'    echo \'{{"name":"{branch}"}}\' | gh api -X PUT '
                f"repos/{org}/{repo}/rulesets/{rs.get('id')} --input -"
            )
            sys.exit(1)


# ── follow-up checklist ──────────────────────────────────────────────────


def print_checklist(org: str, repo: str, *, docs_present: bool) -> None:
    log_info("Follow-up checklist:")
    secrets = gh_get(f"repos/{org}/{repo}/dependabot/secrets")
    names = {s["name"] for s in secrets.get("secrets", [])} if isinstance(secrets, dict) else set()
    missing = {"AUTOMERGE_APP_ID", "AUTOMERGE_APP_PRIVATE_KEY"} - names
    if missing:
        log_warn(
            f"  - Dependabot secrets not set: {sorted(missing)}. Not a failure — "
            "automerge.yml degrades to a green 'not configured' summary and PRs "
            "wait for manual merge. See docs/github-standard.md § Dependabot to set up the App."
        )
    log_warn("  - Confirm ruff and pytest are real dev dependencies, not just pre-commit hook envs")
    log_warn(
        '    (otherwise CI fails with "Failed to spawn: `ruff`" even though '
        "pre-commit works locally)."
    )
    if docs_present:
        log_warn(
            '  - docs/ exists: add extend-exclude = ["docs"] under [tool.ruff] in pyproject.toml '
            "if ruff >=0.13 (it formats fenced Python in Markdown by default)."
        )
    log_warn("  - Run `pre-commit install` in the repo.")
    log_warn(f"  - Once CI has gone green once: ./init.py {repo} --checks")


# ── checks mode (phase 2) ────────────────────────────────────────────────


def run_checks_mode(org: str, repo: str, branch: str) -> int:
    config = json.loads(CONFIG_PATH.read_text())
    if repo not in config.get("repos", {}):
        log_err(f"'{repo}' is not in {CONFIG_PATH.name} yet — run ./init.py {repo} --apply first.")
        return 1

    check_runs = gh_get(f"repos/{org}/{repo}/commits/{branch}/check-runs", paginate=True)
    if not isinstance(check_runs, dict) or "check_runs" not in check_runs:
        log_err(
            f"Couldn't read check-runs for {org}/{repo}@{branch} — has CI ever run on this branch?"
        )
        return 1
    contexts = sorted({c["name"] for c in check_runs["check_runs"]})
    if not contexts:
        log_err(
            f"No check runs found on {org}/{repo}@{branch} yet — push once and let CI finish first."
        )
        return 1

    log_info(f"Contexts seen on {org}/{repo}@{branch}: {contexts}")
    log_warn(
        "Not auto-splicing this — docs/github-standard.md is explicit that required-check "
        "contexts must be hand-confirmed, never regenerated from CI automatically. "
        f'Merge this into the existing repos["{repo}"] entry in {CONFIG_PATH.name} by hand:'
    )
    snippet = {
        "branches": {
            branch: {
                "extra_rules": [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "strict_required_status_checks_policy": False,
                            "do_not_enforce_on_create": False,
                            "required_status_checks": [{"context": c} for c in contexts],
                        },
                    }
                ]
            }
        }
    }
    print(json.dumps(snippet, indent=2))
    log_info(f"Then: ./github-standard.py {repo} --apply")
    return 0


# ── bootstrap mode (phase 1) ─────────────────────────────────────────────


def run_bootstrap_mode(args: argparse.Namespace) -> int:
    repo_root = args.path or (Path.home() / "projects" / args.repo)
    if not repo_root.is_dir():
        log_err(f"{repo_root} doesn't exist. Pass --path.")
        return 1

    pyproject = load_pyproject(repo_root)
    version = args.python or detect_python_version(pyproject)
    dev_style = args.dev_style or detect_dev_style(pyproject)
    # Not auto-detected from uv.lock's presence: that file is near-universal
    # once a repo uses uv at all (logger and rl-bot both have one but use
    # plain `uv sync --dev`), so it doesn't discriminate. --locked is an
    # explicit per-repo style choice — default to the template's own default
    # (unlocked) unless asked for.
    locked = bool(args.locked)
    include_tests = (
        detect_tests(repo_root, pyproject, dev_style) if args.tests is None else args.tests
    )
    docs_present = detect_docs(repo_root)
    install_cmd = install_cmd_for(dev_style, locked)

    dev_style_label = (
        "dependency-groups" if dev_style == "groups" else "project.optional-dependencies"
    )
    log_info(
        f"Detected: python {version}, dev-deps via [{dev_style_label}], "
        f"{'locked' if locked else 'unlocked'}, tests {'present' if include_tests else 'absent'}, "
        f"docs/ {'present' if docs_present else 'absent'}"
    )

    if args.render_to is not None:
        branch = args.branch or "main"
        out_root = args.render_to
        files = {
            out_root / ".pre-commit-config.yaml": render_pre_commit(version),
            out_root / ".github" / "workflows" / "ci.yml": render_ci(
                install_cmd=install_cmd, branch=branch, include_tests=include_tests
            ),
            out_root / ".github" / "dependabot.yml": render_dependabot(),
            out_root / ".github" / "workflows" / "dependabot-automerge.yml": render_automerge(),
        }
        for path, content in files.items():
            write_rendered(path, content, apply=args.apply, force=args.force)
        log_info(f"Rendered to {out_root} — no JSON entry, no delegate, no API writes.")
        return 0

    preflight_gh_auth()

    config = json.loads(CONFIG_PATH.read_text())
    org: str = config["org"]
    branch = args.branch or get_default_branch(org, args.repo)
    preflight_ruleset(org, args.repo, branch)

    out_root = repo_root
    files = {
        out_root / ".pre-commit-config.yaml": render_pre_commit(version),
        out_root / ".github" / "workflows" / "ci.yml": render_ci(
            install_cmd=install_cmd, branch=branch, include_tests=include_tests
        ),
        out_root / ".github" / "dependabot.yml": render_dependabot(),
        out_root / ".github" / "workflows" / "dependabot-automerge.yml": render_automerge(),
    }
    for path, content in files.items():
        write_rendered(path, content, apply=args.apply, force=args.force)

    splice_repos_entry(args.repo, args.profile, apply=args.apply)

    if not args.apply:
        log_info(
            f"Dry run — re-run with --apply to write, then: "
            f"./github-standard.py {args.repo} --apply"
        )
        print_checklist(org, args.repo, docs_present=docs_present)
        return 0

    log_info(f"Delegating to github-standard.py for {args.repo} (settings/security/ruleset)...")
    proc = subprocess.run(
        ["./github-standard.py", args.repo, "--apply"], cwd=REPO_ROOT, check=False
    )
    print_checklist(org, args.repo, docs_present=docs_present)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", help="GitHub repo name (may differ from the local directory name).")
    parser.add_argument(
        "--path", type=Path, default=None, help="Local repo path (default: ~/projects/<repo>)."
    )
    parser.add_argument(
        "--profile", default="baseline", help="Profile to write into github-standard.json."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually write. Without it, dry run only."
    )
    parser.add_argument("--force", action="store_true", help="Overwrite files that already exist.")
    parser.add_argument("--branch", default=None, help="Override the detected default branch.")
    parser.add_argument(
        "--python", default=None, help="Override the detected Python version, e.g. 3.13."
    )
    parser.add_argument(
        "--dev-style",
        choices=["groups", "optional"],
        default=None,
        help="Override dev-dependency style detection.",
    )
    parser.add_argument(
        "--locked",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Add --locked to uv sync (default: off, matching the template).",
    )
    parser.add_argument(
        "--tests",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override test-job-inclusion detection.",
    )
    parser.add_argument(
        "--checks",
        action="store_true",
        help="Phase 2: read real CI contexts, print the required_status_checks snippet.",
    )
    parser.add_argument(
        "--render-to",
        type=Path,
        default=None,
        help="Render templates into DIR only — no JSON entry, no delegate, no API writes.",
    )
    args = parser.parse_args()

    if args.checks:
        config = json.loads(CONFIG_PATH.read_text())
        org = config["org"]
        preflight_gh_auth()
        branch = args.branch or get_default_branch(org, args.repo)
        return run_checks_mode(org, args.repo, branch)

    return run_bootstrap_mode(args)


if __name__ == "__main__":
    sys.exit(main())
