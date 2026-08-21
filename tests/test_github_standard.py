from conftest import REPO_ROOT, import_module_from_path

gh = import_module_from_path("github_standard", REPO_ROOT / "github-standard.py")


def test_deep_merge_overrides_scalar():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    overrides = {"a": 9, "b": {"c": 8}}
    assert gh.deep_merge(base, overrides) == {"a": 9, "b": {"c": 8, "d": 3}}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    overrides = {"a": {"c": 2}}
    result = gh.deep_merge(base, overrides)
    assert result == {"a": {"b": 1, "c": 2}}
    assert base == {"a": {"b": 1}}
    assert overrides == {"a": {"c": 2}}


def test_get_repo_tier_branches_key_forces_ruleset():
    profiles = {"hobby": {"tier": "no-ruleset"}}
    repo_cfg = {"profile": "hobby", "branches": {"main": {}}}
    assert gh.get_repo_tier(profiles, repo_cfg) == "ruleset"


def test_get_repo_tier_falls_back_to_profile():
    profiles = {"baseline": {"tier": "ruleset"}}
    repo_cfg = {"profile": "baseline"}
    assert gh.get_repo_tier(profiles, repo_cfg) == "ruleset"


def test_resolve_branch_cfg_layers_profile_then_branch():
    profiles = {
        "baseline": {
            "rule_overrides": {"pull_request": {"required_approving_review_count": 0}},
            "extra_rules": [{"type": "deletion"}],
            "bypass_actors": [],
        }
    }
    repo_cfg = {"profile": "baseline"}
    branch_cfg = {"rule_overrides": {"pull_request": {"required_approving_review_count": 1}}}
    resolved = gh.resolve_branch_cfg(profiles, repo_cfg, branch_cfg)
    assert resolved["rule_overrides"]["pull_request"]["required_approving_review_count"] == 1
    assert resolved["extra_rules"] == [{"type": "deletion"}]
    assert resolved["bypass_actors"] == []


def test_build_ruleset_applies_overrides_and_extra_rules():
    defaults_ruleset = {
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "rules": [{"type": "pull_request", "parameters": {"required_approving_review_count": 0}}],
    }
    branch_cfg = {
        "rule_overrides": {"pull_request": {"required_approving_review_count": 1}},
        "extra_rules": [{"type": "deletion"}],
    }
    built = gh.build_ruleset(defaults_ruleset, "main", branch_cfg)
    assert built["name"] == "main"
    assert built["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
    pr_rule = next(r for r in built["rules"] if r["type"] == "pull_request")
    assert pr_rule["parameters"]["required_approving_review_count"] == 1
    assert {"type": "deletion"} in built["rules"]


def test_normalize_is_order_insensitive():
    ruleset_a = {
        "name": "main",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [{"actor_id": 2}, {"actor_id": 1}],
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "b"}, {"context": "a"}]},
            },
        ],
    }
    ruleset_b = {
        "name": "main",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [{"actor_id": 1}, {"actor_id": 2}],
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "a"}, {"context": "b"}]},
            },
            {"type": "deletion"},
        ],
    }
    assert gh.normalize(ruleset_a) == gh.normalize(ruleset_b)
