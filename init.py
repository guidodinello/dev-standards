#!/usr/bin/env -S uv run
"""init — bootstrap a Python repo onto the dev-standards baseline in one step.

Why: `github-standard.py` applies GitHub *API* state (settings, security,
branch rulesets) from `github-standard.json`, but refuses any repo that isn't
already a key in that file. `templates/` holds the CI/pre-commit/Dependabot
*files*. Before this script, onboarding a repo was four `cp`s, two hand-edits,
a hand-written JSON entry, then a run of github-standard.py. That friction is
what produced commit f44f66a ("docs(template): note two gotchas found rolling
out to rl-tournament-notification-bot"). This closes the gap.

The files are a Copier template (copier.yml, templates/copier/). Detection is
read from the target repo itself (pyproject.toml, tests/, docs/) and passed to
Copier as answers; every detected value has an override flag. The rendered
repo gets a .copier-answers.yml recording the template commit and the answers,
so `copier update` can bring later template changes in (docs/template-sync.md).
Required status
checks are deliberately NOT part of the one-step flow: docs/github-standard.md
is explicit that a required-check context is a job's display name, read off a
real CI run and hand-confirmed — never auto-derived. Run --checks after the
first green CI run: it prints the single `CI` context pinned with integration_id
15368 when the repo's CI has the aggregate job, else the per-job contexts plus a
hint to adopt it.

`github-standard.json` is gitignored, hand-aligned, and carries real
rationale in its comments — this never does a json.loads/json.dumps
round-trip on it (that would reflow the whole file with no git diff to
review the damage). It only ever splices one new line after `"repos": {`.

Usage:
    ./init.py my-repo                          # dry run, ~/projects/my-repo
    ./init.py my-repo --apply                  # write files + JSON entry, delegate
    ./init.py my-repo --path ~/other/dir --apply
    ./init.py my-repo --release --apply        # also add .github/workflows/release.yml
    ./init.py my-repo --checks                 # phase 2: after first green CI —
                                                # prints a snippet to paste, never writes
    ./init.py my-repo --render-to /tmp/out      # render only, for diffing — no writes elsewhere

Requires: `uv` (the shebang runs this in the project environment, which has
Copier; run it from the dev-standards checkout), and the `gh` CLI authenticated
with a token that has the `repo` scope (same preflight as github-standard.py,
run only when talking to the API).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from fnmatch import fnmatch
from pathlib import Path

try:
    import yaml
    from copier import run_copy
except ImportError:
    sys.exit(
        "init.py needs Copier: run it from the dev-standards checkout as ./init.py "
        "(its shebang is `uv run`) or `uv run ./init.py`."
    )

GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
NC = "\033[0m"

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = REPO_ROOT / "github-standard.json"
TEMPLATE_URL = "https://github.com/guidodinello/dev-standards.git"
ANSWERS_FILE = ".copier-answers.yml"


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


def detect_tests(repo_root: Path, pyproject: dict, dev_style: str) -> bool:
    """Checked against dev_style's own dep list, not both styles: dev_style
    is exactly what the rendered install step will install, so pytest declared only
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


# ── template rendering: Copier renders templates/copier/ (copier.yml at the
# repo root), the single source of truth for every file this script writes ───


def render(out_root: Path, answers: dict[str, object]) -> dict[Path, str]:
    """Render the Copier template with `answers`; returns {out_root/<rel>: content}.

    Renders this checkout's HEAD (plus any uncommitted changes, which Copier
    picks up itself), never a tag: `vcs_ref="HEAD"` keeps a future PEP 440 tag
    from silently rendering something older than what's checked out.
    """
    with tempfile.TemporaryDirectory() as tmp:
        run_copy(str(REPO_ROOT), tmp, data=answers, defaults=True, quiet=True, vcs_ref="HEAD")
        root = Path(tmp)
        files = {
            out_root / path.relative_to(root): path.read_text()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
    # Copier records the source it was given (this local path). A later
    # `copier update` must work from any machine, so point it at the remote.
    answers_path = out_root / ANSWERS_FILE
    files[answers_path] = re.sub(
        r"^_src_path: .*$", f"_src_path: {TEMPLATE_URL}", files[answers_path], count=1, flags=re.M
    )
    return files


def skip_if_exists_patterns() -> list[str]:
    """copier.yml's `_skip_if_exists`: files a repo owns after the first render."""
    return yaml.safe_load((REPO_ROOT / "copier.yml").read_text()).get("_skip_if_exists", [])


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def check_template_checkout(repo: Path, *, apply: bool) -> None:
    """A bootstrapped repo records the template commit it was rendered from in
    .copier-answers.yml; `copier update` later diffs from it. With --apply, refuse
    a render whose `_commit` the remote won't have: Copier turns any uncommitted or
    untracked change into a throwaway commit, and a local-only commit is no
    better. A dry run writes nothing, so it only warns about what --apply would
    refuse. Either way, warn when the checkout is behind the remote main — the
    stale-pins incident in issue #53."""
    problems = []
    if _git(repo, "status", "--porcelain").stdout.strip():
        problems.append(
            f"{repo} has uncommitted or untracked changes — the rendered repo would record "
            "a template commit that exists nowhere else. Commit or stash first "
            "(--render-to renders anything)."
        )
    elif _git(repo, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode != 0:
        problems.append(
            f"{repo}'s HEAD isn't on origin/main — the rendered repo would record a template "
            "commit the remote doesn't have. Push it or switch to main first."
        )
    for problem in problems:
        if apply:
            log_err(problem)
        else:
            log_warn(f"--apply would refuse: {problem}")
    if problems and apply:
        sys.exit(1)
    remote = _git(repo, "ls-remote", "origin", "refs/heads/main").stdout.split()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if remote and remote[0] != head:
        log_warn(
            "This dev-standards checkout isn't at the remote main — templates may be stale. "
            "`git pull` first unless that's intended."
        )


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


def write_all(files: dict[Path, str], out_root: Path, *, apply: bool, force: bool) -> None:
    """Writes the render. Two rules on top of write_rendered:
    - A `_skip_if_exists` file (CHANGELOG.md) that exists is the repo's own: kept
      even with --force, as `copier update` keeps it.
    - .copier-answers.yml claims every other file came from this render's
      `_commit`. If any of them is skipped because it already exists (no --force),
      that claim would be false, and a later `copier update` would 3-way merge
      from a base the file never had — so the answers file is not written."""
    keep = skip_if_exists_patterns()
    answers_path = out_root / ANSWERS_FILE

    def owned(path: Path) -> bool:
        return any(fnmatch(path.relative_to(out_root).as_posix(), p) for p in keep)

    kept = [path for path in files if path != answers_path and path.exists() and not force]
    not_rendered = [path for path in kept if not owned(path)]
    for path, content in files.items():
        if path == answers_path and not_rendered:
            log_warn(
                f"not writing {path}: these files already exist and were kept, so they "
                f"weren't rendered from this template commit: "
                f"{', '.join(str(p.relative_to(out_root)) for p in not_rendered)}. "
                "Pass --force to render them too, or adopt the repo later (issue #53)."
            )
        elif owned(path) and path.exists():
            log_info(f"{path} already exists — kept (the repo's own; --force doesn't touch it)")
        else:
            write_rendered(path, content, apply=apply, force=force)


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
    log_warn(
        "  - Confirm ruff, pytest and pre-commit are real dev dependencies, "
        "not just pre-commit hook envs or a global install"
    )
    log_warn(
        '    (otherwise CI fails with "Failed to spawn: `ruff`" or `pre-commit` '
        "even though they work locally)."
    )
    if docs_present:
        log_warn(
            '  - docs/ exists: add extend-exclude = ["docs"] under [tool.ruff] in pyproject.toml '
            "if ruff >=0.13 (it formats fenced Python in Markdown by default)."
        )
    log_warn("  - Run `pre-commit install` in the repo.")
    log_warn(f"  - Once CI has gone green once: ./init.py {repo} --checks")


# ── checks mode (phase 2) ────────────────────────────────────────────────


ACTIONS_APP_ID = 15368  # GitHub Actions' own app id, pinned so no other app can satisfy a check


def has_aggregate_ci_job(workflow_text: str | None) -> bool:
    """True if the workflow has a job named `CI` that `needs:` other jobs and runs
    `if: always()` (the aggregate pattern from the CI template). A bare
    check-run called "CI" proves nothing — only the workflow shows it gates on the rest."""
    if not workflow_text:
        return False
    for block in re.split(r"\n(?=  [\w-]+:\n)", workflow_text):
        if re.search(r"^    name:\s*[\"']?CI[\"']?\s*$", block, re.M):
            return bool(
                re.search(r"^    needs:", block, re.M)
                and re.search(r"^    if:\s*always\(\)", block, re.M)
            )
    return False


def checks_snippet(
    check_runs: list[dict], workflow_text: str | None
) -> tuple[list[dict], str | None]:
    """Pure: (check-runs seen on the branch, ci.yml text) -> (required_status_checks
    entries, hint). Aggregate mode emits the single `CI` context pinned to the app
    that produced it; anything else falls back to one entry per seen context."""
    ci_runs = [c for c in check_runs if c["name"] == "CI"]
    if has_aggregate_ci_job(workflow_text) and ci_runs:
        app_id = (ci_runs[0].get("app") or {}).get("id")
        if app_id == ACTIONS_APP_ID:
            return [{"context": "CI", "integration_id": app_id}], None
        return (
            [{"context": c} for c in sorted({c["name"] for c in check_runs})],
            f"Aggregate CI job found, but its check-run came from app {app_id!r}, "
            f"not GitHub Actions ({ACTIONS_APP_ID}) — not pinning it; check by hand.",
        )
    return (
        [{"context": c} for c in sorted({c["name"] for c in check_runs})],
        "No aggregate `CI` job in .github/workflows/ci.yml — listing per-job contexts. "
        "Adopt the aggregate job from dev-standards' CI template "
        "(templates/copier/.github/workflows/ci.yml.jinja), then re-run --checks "
        "to require the single pinned `CI` context instead.",
    )


def fetch_workflow_text(org: str, repo: str, branch: str) -> str | None:
    obj = gh_get(f"repos/{org}/{repo}/contents/.github/workflows/ci.yml?ref={branch}")
    if not isinstance(obj, dict) or "content" not in obj:
        return None
    return base64.b64decode(obj["content"]).decode()


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
    if not check_runs["check_runs"]:
        log_err(
            f"No check runs found on {org}/{repo}@{branch} yet — push once and let CI finish first."
        )
        return 1

    checks, hint = checks_snippet(check_runs["check_runs"], fetch_workflow_text(org, repo, branch))
    seen = sorted({c["name"] for c in check_runs["check_runs"]})
    log_info(f"Contexts seen on {org}/{repo}@{branch}: {seen}")
    if hint:
        log_warn(hint)
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
                            "required_status_checks": checks,
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

    dev_style_label = (
        "dependency-groups" if dev_style == "groups" else "project.optional-dependencies"
    )
    log_info(
        f"Detected: python {version}, dev-deps via [{dev_style_label}], "
        f"{'locked' if locked else 'unlocked'}, tests {'present' if include_tests else 'absent'}, "
        f"docs/ {'present' if docs_present else 'absent'}"
    )
    answers = {
        "python_version": version,
        "dev_style": dev_style,
        "locked": locked,
        "include_tests": include_tests,
        "release": args.release,
    }

    if args.render_to is not None:
        out_root = args.render_to
        files = render(out_root, {**answers, "branch": args.branch or "main"})
        write_all(files, out_root, apply=args.apply, force=args.force)
        log_info(f"Rendered to {out_root} — no JSON entry, no delegate, no API writes.")
        return 0

    check_template_checkout(REPO_ROOT, apply=args.apply)
    preflight_gh_auth()

    config = json.loads(CONFIG_PATH.read_text())
    org: str = config["org"]
    branch = args.branch or get_default_branch(org, args.repo)
    preflight_ruleset(org, args.repo, branch)

    files = render(repo_root, {**answers, "branch": branch})
    write_all(files, repo_root, apply=args.apply, force=args.force)

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
        "--release",
        action="store_true",
        help="Also render the tag-triggered release workflow (libraries/distributed apps only).",
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
