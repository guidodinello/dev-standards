#!/usr/bin/env python3
"""github-standard — audit/apply the GitHub repo-config baseline.

Why: `push-guidelines.sh` solved config drift for *file* content pushed into
project repos. This solves the same problem for *GitHub repo settings and
branch rulesets* — before this, the baseline (delete_branch_on_merge, a
squash-only pull_request ruleset, required_signatures, etc.) existed only as
four hand-configured repos that had quietly drifted from each other. See
docs/github-standard.md for the full rationale and the two pinned exceptions
(fitted, pullscope).

This is Python, not bash+jq, because the payload GitHub actually wants *is*
JSON (ruleset bodies), and diffing/merging nested JSON structures correctly
(order-insensitive, partial per-rule overrides) is native here and fragile in
jq. `push-guidelines.sh` stays bash because rsync is the right tool for
syncing files; this is the right tool for syncing structured API state.
Output style (colored [INFO]/[WARN], dry-run default, --apply to write,
optional single-repo argument, "keep going on per-target failure") matches
push-guidelines.sh deliberately — same house pattern, different substrate.

Usage:
    ./github-standard.py                  # audit every repo in the config
    ./github-standard.py --apply          # actually write
    ./github-standard.py knowledger-bot   # one repo, dry run
    ./github-standard.py knowledger-bot --apply

Requires: `gh` CLI, authenticated with a token that has the `repo` scope.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
NC = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{GREEN}[INFO]{NC}  {msg}")


def log_warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{NC}  {msg}")


def log_err(msg: str) -> None:
    print(f"{RED}[FAIL]{NC}  {msg}")


class GhApiError(RuntimeError):
    pass


def gh_api(
    path: str,
    method: str = "GET",
    body: dict | None = None,
    allow_404: bool = False,
) -> dict | list | None:
    """Call `gh api`. Raises GhApiError on failure unless allow_404 and the
    failure is a 404 (used for the vulnerability-alerts / security-fixes
    on-off endpoints, where 404 means "currently disabled")."""
    cmd = ["gh", "api", "-X", method, path]
    input_data = None
    if body is not None:
        cmd += ["--input", "-"]
        input_data = json.dumps(body)
    proc = subprocess.run(cmd, input=input_data, capture_output=True, text=True)
    if proc.returncode != 0:
        if allow_404 and "HTTP 404" in proc.stderr:
            return None
        raise GhApiError(f"{method} {path} failed: {proc.stderr.strip()}")
    if not proc.stdout.strip():
        return {}
    return json.loads(proc.stdout)


def gh_api_obj(path: str, method: str = "GET", body: dict | None = None) -> dict:
    result = gh_api(path, method=method, body=body)
    assert isinstance(result, dict), f"expected object from {method} {path}, got {type(result)}"
    return result


def gh_api_list(path: str) -> list:
    result = gh_api(path)
    assert isinstance(result, list), f"expected array from GET {path}, got {type(result)}"
    return result


def preflight() -> None:
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if proc.returncode != 0:
        log_err("gh auth status failed — not logged in. Aborting before any writes.")
        sys.exit(1)
    scopes_proc = subprocess.run(
        ["gh", "api", "-i", "/"], capture_output=True, text=True
    )
    scopes_line = next(
        (l for l in scopes_proc.stdout.splitlines() if l.lower().startswith("x-oauth-scopes")),
        "",
    )
    if "repo" not in scopes_line:
        log_err(
            f"Active gh token is missing the 'repo' scope ({scopes_line.strip() or 'no scope header seen'}). "
            "Aborting before any writes — this would otherwise surface as 20 misleading 403s."
        )
        sys.exit(1)


# ── ruleset construction ──────────────────────────────────────────────────


def deep_merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_repo_tier(profiles: dict, repo_cfg: dict) -> str:
    """A repo with an explicit 'branches' map always wants a ruleset (branches
    only exist to configure one). Otherwise tier comes from the repo's profile."""
    if "branches" in repo_cfg:
        return "ruleset"
    profile_name = repo_cfg.get("profile")
    if profile_name is None:
        raise ValueError(f"repo config needs 'profile' or 'branches': {repo_cfg}")
    return profiles[profile_name]["tier"]


def resolve_branch_cfg(profiles: dict, repo_cfg: dict, branch_cfg: dict) -> dict:
    """A branch resolves its profile (branch-level 'profile' wins, else the
    repo-level one, else 'baseline'), then layers the branch's own
    rule_overrides/extra_rules/bypass_actors on top of the profile's — same
    layering build_ruleset then applies on top of defaults.ruleset."""
    profile_name = branch_cfg.get("profile") or repo_cfg.get("profile") or "baseline"
    profile = profiles[profile_name]
    merged = {
        "rule_overrides": deep_merge(
            profile.get("rule_overrides", {}), branch_cfg.get("rule_overrides", {})
        ),
        "extra_rules": profile.get("extra_rules", []) + branch_cfg.get("extra_rules", []),
    }
    bypass = branch_cfg.get("bypass_actors", profile.get("bypass_actors"))
    if bypass is not None:
        merged["bypass_actors"] = bypass
    return merged


def build_ruleset(defaults_ruleset: dict, branch: str, branch_cfg: dict) -> dict:
    rules = copy.deepcopy(defaults_ruleset["rules"])
    overrides = branch_cfg.get("rule_overrides", {})
    for rule in rules:
        if rule["type"] in overrides:
            rule["parameters"] = deep_merge(
                rule.get("parameters", {}), overrides[rule["type"]]
            )
    rules += copy.deepcopy(branch_cfg.get("extra_rules", []))

    return {
        "name": branch,
        "target": defaults_ruleset["target"],
        "enforcement": defaults_ruleset["enforcement"],
        "bypass_actors": branch_cfg.get("bypass_actors", defaults_ruleset["bypass_actors"]),
        "conditions": {"ref_name": {"include": [f"refs/heads/{branch}"], "exclude": []}},
        "rules": rules,
    }


def normalize(ruleset: dict) -> dict:
    """Order-insensitive canonical form, for comparing intended vs. what
    GitHub returns. GitHub returns rules/bypass_actors/checks in its own
    order — a naive dict/list compare would report permanent phantom drift."""
    r = copy.deepcopy(ruleset)

    def sort_key(rule: dict) -> str:
        return json.dumps(rule, sort_keys=True)

    r["rules"] = sorted(r.get("rules", []), key=sort_key)
    for rule in r["rules"]:
        params = rule.get("parameters")
        if isinstance(params, dict) and "required_status_checks" in params:
            params["required_status_checks"] = sorted(
                params["required_status_checks"], key=lambda c: c["context"]
            )
        if isinstance(params, dict) and "required_reviewers" in params:
            params["required_reviewers"] = sorted(
                params["required_reviewers"], key=json.dumps
            )
    r["bypass_actors"] = sorted(r.get("bypass_actors", []), key=sort_key)
    r["conditions"]["ref_name"]["include"] = sorted(r["conditions"]["ref_name"]["include"])
    r["conditions"]["ref_name"]["exclude"] = sorted(r["conditions"]["ref_name"]["exclude"])
    return {
        "name": r["name"],
        "target": r["target"],
        "enforcement": r["enforcement"],
        "bypass_actors": r["bypass_actors"],
        "conditions": r["conditions"],
        "rules": r["rules"],
    }


# ── per-repo passes ─────────────────────────────────────────────────────────


def sync_settings(org: str, repo: str, defaults: dict, repo_cfg: dict, apply: bool) -> bool:
    """Returns True if anything changed (or would change)."""
    intended = deep_merge(defaults["settings"], repo_cfg.get("settings_overrides", {}))
    current = gh_api_obj(f"repos/{org}/{repo}")
    diff = {k: v for k, v in intended.items() if current.get(k) != v}
    if not diff:
        log_info(f"{repo} settings — up to date")
        return False
    log_warn(f"{repo} settings — drift: {diff}")
    if apply:
        gh_api(f"repos/{org}/{repo}", method="PATCH", body=diff)
        log_info(f"{repo} settings — applied")
    return True


def sync_security(org: str, repo: str, defaults: dict, apply: bool) -> bool:
    sec = defaults["security"]
    changed = False
    repo_obj = gh_api_obj(f"repos/{org}/{repo}")
    is_public = not repo_obj.get("private", True)

    # vulnerability alerts: 204 = enabled, 404 = disabled
    va_on = (
        subprocess.run(
            ["gh", "api", f"repos/{org}/{repo}/vulnerability-alerts"],
            capture_output=True,
        ).returncode
        == 0
    )
    if sec["vulnerability_alerts"] and not va_on:
        changed = True
        log_warn(f"{repo} security — vulnerability alerts disabled, want enabled")
        if apply:
            gh_api(f"repos/{org}/{repo}/vulnerability-alerts", method="PUT")

    asf_on = (
        subprocess.run(
            ["gh", "api", f"repos/{org}/{repo}/automated-security-fixes"],
            capture_output=True,
        ).returncode
        == 0
    )
    if sec["automated_security_fixes"] and not asf_on:
        changed = True
        log_warn(f"{repo} security — automated security fixes disabled, want enabled")
        if apply:
            gh_api(f"repos/{org}/{repo}/automated-security-fixes", method="PUT")

    if sec["secret_scanning"] == "public-only":
        if is_public:
            sa = repo_obj.get("security_and_analysis") or {}
            ss_status = (sa.get("secret_scanning") or {}).get("status")
            pp_status = (sa.get("secret_scanning_push_protection") or {}).get("status")
            want = {}
            if ss_status != "enabled":
                want["secret_scanning"] = {"status": "enabled"}
            if pp_status != "enabled":
                want["secret_scanning_push_protection"] = {"status": "enabled"}
            if want:
                changed = True
                log_warn(f"{repo} security — secret scanning drift: {list(want)}")
                if apply:
                    gh_api(
                        f"repos/{org}/{repo}",
                        method="PATCH",
                        body={"security_and_analysis": want},
                    )
        # private repos: needs GHAS, skip entirely — not an error, not drift.

    if not changed:
        log_info(f"{repo} security — up to date")
    elif apply:
        log_info(f"{repo} security — applied")
    return changed


def sync_ruleset(org: str, repo: str, defaults: dict, profiles: dict, repo_cfg: dict, apply: bool) -> bool:
    changed = False
    branches = repo_cfg.get("branches") or {"main": {}}
    existing = gh_api_list(f"repos/{org}/{repo}/rulesets")
    existing_by_name = {r["name"]: r for r in existing}

    for branch, raw_branch_cfg in branches.items():
        branch_cfg = resolve_branch_cfg(profiles, repo_cfg, raw_branch_cfg)
        intended = build_ruleset(defaults["ruleset"], branch, branch_cfg)
        intended_norm = normalize(intended)

        if branch not in existing_by_name:
            changed = True
            log_warn(f"{repo}/{branch} ruleset — MISSING, will create")
            if apply:
                gh_api(f"repos/{org}/{repo}/rulesets", method="POST", body=intended)
                log_info(f"{repo}/{branch} ruleset — created")
            continue

        current = gh_api_obj(f"repos/{org}/{repo}/rulesets/{existing_by_name[branch]['id']}")
        current_norm = normalize(current)
        if current_norm == intended_norm:
            log_info(f"{repo}/{branch} ruleset — up to date")
            continue

        changed = True
        log_warn(f"{repo}/{branch} ruleset — drift detected")
        if apply:
            gh_api(
                f"repos/{org}/{repo}/rulesets/{existing_by_name[branch]['id']}",
                method="PUT",
                body=intended,
            )
            log_info(f"{repo}/{branch} ruleset — applied")

    # Report legacy-named rulesets this config doesn't manage. Never
    # auto-rename — a rename is a one-time manual step (see docs).
    managed = set(branches.keys())
    for name in existing_by_name:
        if name not in managed:
            log_warn(
                f"{repo} — unmanaged ruleset named '{name}' exists (id {existing_by_name[name]['id']}). "
                f"Rename it to match a branch key ({sorted(managed)}) to bring it under management, or leave it if intentional."
            )

    return changed


# ── driver ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", nargs="?", help="Only audit/apply this one repo.")
    parser.add_argument("--apply", action="store_true", help="Actually write. Without it, dry run only.")
    args = parser.parse_args()

    config_path = Path(__file__).parent / "github-standard.json"
    config = json.loads(config_path.read_text())
    org = config["org"]
    defaults = config["defaults"]
    profiles = config["profiles"]
    repos = config["repos"]

    if args.repo:
        if args.repo not in repos:
            log_err(f"'{args.repo}' is not in {config_path.name} — nothing to do.")
            return 1
        repos = {args.repo: repos[args.repo]}

    preflight()

    if args.apply:
        log_info(f"Applying baseline to {len(repos)} repo(s).")
    else:
        log_info(f"Dry run over {len(repos)} repo(s) — nothing will be written. Re-run with --apply.")

    any_changed = False
    any_failed = False
    for repo, repo_cfg in repos.items():
        try:
            c1 = sync_settings(org, repo, defaults, repo_cfg, args.apply)
            c2 = sync_security(org, repo, defaults, args.apply)
            c3 = False
            if get_repo_tier(profiles, repo_cfg) == "ruleset":
                c3 = sync_ruleset(org, repo, defaults, profiles, repo_cfg, args.apply)
            if c1 or c2 or c3:
                any_changed = True
        except GhApiError as e:
            any_failed = True
            log_warn(f"{repo} — {e}, skipping rest of this repo")
            continue

    if not any_changed:
        log_info("Everything already in sync.")
    elif args.apply:
        log_info("Done.")
    else:
        log_info("Re-run with --apply to write these changes.")

    if any_failed:
        log_warn("One or more repos failed — see warnings above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
