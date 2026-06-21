"""Tests for the advanced architecture rules added to close competitor gaps:
glob forbidden matching, can_only_depend_on allowlist, module independence,
must_not_reach reachability, no_orphans, and per-rule severity overrides.
"""

from __future__ import annotations

from code_governance.dep_graph import DependencyGraph
from code_governance.rules import (
    apply_severity_overrides,
    check_can_only_depend_on,
    check_enforce_cannot_depend_on,
    check_independence,
    check_must_not_reach,
    check_no_orphans,
    run_all_rules,
)
from code_governance.schemas import (
    EdgeDetail,
    GovernanceConfig,
    Language,
    ModuleConfig,
    ReachConstraint,
    RulesConfig,
    Severity,
)


def _graph(edges: dict[str, list[str]]) -> DependencyGraph:
    g = DependencyGraph()
    for src, tgts in edges.items():
        for tgt in tgts:
            g.module_edges[src][tgt] += 1
            g.edge_details.append(EdgeDetail(
                source_file=f"{src}/x.py", source_module=src, target_module=tgt,
                imported_name="y", line=1, raw_statement=f"from {tgt} import y",
            ))
    return g


def _cfg(modules, **rules) -> GovernanceConfig:
    return GovernanceConfig(
        language=Language.PYTHON,
        modules=[ModuleConfig(**m) for m in modules],
        rules=RulesConfig(**rules),
    )


# ── Glob forbidden ────────────────────────────────────────────────────────


def test_cannot_depend_on_glob_match():
    g = _graph({"api": ["tests_unit", "core"]})
    cfg = _cfg(
        [{"name": "api", "path": "api/", "cannot_depend_on": ["tests_*"]},
         {"name": "tests_unit", "path": "tests_unit/"},
         {"name": "core", "path": "core/"}],
    )
    v = check_enforce_cannot_depend_on(g, cfg)
    assert len(v) == 1 and v[0].module == "api"
    assert "tests_unit" in v[0].detail


def test_cannot_depend_on_exact_still_works():
    g = _graph({"api": ["billing"]})
    cfg = _cfg([{"name": "api", "path": "api/", "cannot_depend_on": ["billing"]},
                {"name": "billing", "path": "billing/"}])
    assert len(check_enforce_cannot_depend_on(g, cfg)) == 1


# ── Allowlist ─────────────────────────────────────────────────────────────


def test_can_only_depend_on_flags_outside_allowlist():
    g = _graph({"api": ["core", "db"]})
    cfg = _cfg([
        {"name": "api", "path": "api/", "can_only_depend_on": ["core"]},
        {"name": "core", "path": "core/"},
        {"name": "db", "path": "db/"},
    ])
    v = check_can_only_depend_on(g, cfg)
    assert len(v) == 1 and "db" in v[0].detail and "core" not in v[0].detail.split("imports")[1].split("(")[0]


def test_can_only_depend_on_allows_listed():
    g = _graph({"api": ["core"]})
    cfg = _cfg([{"name": "api", "path": "api/", "can_only_depend_on": ["core"]},
                {"name": "core", "path": "core/"}])
    assert check_can_only_depend_on(g, cfg) == []


def test_can_only_depend_on_none_means_unenforced():
    g = _graph({"api": ["anything"]})
    cfg = _cfg([{"name": "api", "path": "api/"}, {"name": "anything", "path": "anything/"}])
    assert check_can_only_depend_on(g, cfg) == []


# ── Independence ──────────────────────────────────────────────────────────


def test_independence_direct():
    g = _graph({"billing": ["shipping"]})
    cfg = _cfg([{"name": "billing", "path": "billing/"},
                {"name": "shipping", "path": "shipping/"}],
               independence=[["billing", "shipping"]])
    v = check_independence(g, cfg)
    assert len(v) == 1 and v[0].module == "billing"


def test_independence_transitive():
    g = _graph({"billing": ["util"], "util": ["shipping"]})
    cfg = _cfg([{"name": "billing", "path": "billing/"},
                {"name": "shipping", "path": "shipping/"},
                {"name": "util", "path": "util/"}],
               independence=[["billing", "shipping"]], transitive=True)
    v = check_independence(g, cfg)
    assert any("transitive" in x.detail.lower() for x in v)


def test_independence_clean():
    g = _graph({"billing": ["util"], "shipping": ["util"]})
    cfg = _cfg([{"name": "billing", "path": "billing/"},
                {"name": "shipping", "path": "shipping/"},
                {"name": "util", "path": "util/"}],
               independence=[["billing", "shipping"]], transitive=True)
    assert check_independence(g, cfg) == []


# ── Reachability ──────────────────────────────────────────────────────────


def test_must_not_reach_transitive():
    g = _graph({"web": ["service"], "service": ["secrets"]})
    cfg = _cfg([{"name": "web", "path": "web/"},
                {"name": "service", "path": "service/"},
                {"name": "secrets", "path": "secrets/"}],
               must_not_reach=[ReachConstraint(source=["web"], target=["secrets"])])
    v = check_must_not_reach(g, cfg)
    assert len(v) == 1 and "secrets" in v[0].detail


def test_must_not_reach_glob_target():
    g = _graph({"web": ["service"], "service": ["internal_keys"]})
    cfg = _cfg([{"name": "web", "path": "web/"},
                {"name": "service", "path": "service/"},
                {"name": "internal_keys", "path": "internal_keys/"}],
               must_not_reach=[ReachConstraint(source=["web"], target=["internal_*"])])
    assert len(check_must_not_reach(g, cfg)) == 1


# ── Orphans ───────────────────────────────────────────────────────────────


def test_no_orphans_detects():
    g = _graph({"api": ["core"]})
    cfg = _cfg([{"name": "api", "path": "api/"},
                {"name": "core", "path": "core/"},
                {"name": "lonely", "path": "lonely/"}],
               no_orphans=True)
    v = check_no_orphans(g, cfg)
    assert len(v) == 1 and v[0].module == "lonely"
    assert v[0].severity == Severity.WARNING


def test_no_orphans_excluded():
    g = _graph({"api": ["core"]})
    cfg = _cfg([{"name": "api", "path": "api/"}, {"name": "core", "path": "core/"},
                {"name": "main", "path": "main/"}],
               no_orphans=True, exclude_from_orphans=["main"])
    assert check_no_orphans(g, cfg) == []


# ── Severity overrides ────────────────────────────────────────────────────


def test_severity_override_downgrades_and_off():
    g = _graph({"api": ["billing"]})
    cfg = _cfg([{"name": "api", "path": "api/", "cannot_depend_on": ["billing"]},
                {"name": "billing", "path": "billing/"}],
               severity={"enforce_cannot_depend_on": "warning"})
    vs = run_all_rules(g, cfg)
    forbidden = [v for v in vs if v.rule.value == "enforce_cannot_depend_on"]
    assert forbidden and all(v.severity == Severity.WARNING for v in forbidden)

    cfg.rules.severity = {"enforce_cannot_depend_on": "off"}
    vs = run_all_rules(g, cfg)
    assert not [v for v in vs if v.rule.value == "enforce_cannot_depend_on"]


def test_apply_severity_overrides_passthrough():
    g = _graph({"api": ["billing"]})
    cfg = _cfg([{"name": "api", "path": "api/", "cannot_depend_on": ["billing"]},
                {"name": "billing", "path": "billing/"}])
    vs = check_enforce_cannot_depend_on(g, cfg)
    assert apply_severity_overrides(vs, cfg) == vs  # no overrides -> unchanged


# ── Determinism ───────────────────────────────────────────────────────────


def _cycle_details(edges):
    from code_governance.rules import check_no_cycles
    g = _graph(edges)
    names = {n for n in edges} | {t for ts in edges.values() for t in ts}
    cfg = _cfg([{"name": n, "path": f"{n}/"} for n in names])
    return [v.detail for v in check_no_cycles(g, cfg)]


def test_cycle_detection_is_order_independent():
    # Same graph, edges inserted in two different orders -> identical output.
    a = {"x": ["y"], "y": ["x"], "p": ["q"], "q": ["r"], "r": ["p"]}
    b = {"r": ["p"], "p": ["q"], "q": ["r"], "y": ["x"], "x": ["y"]}
    assert _cycle_details(a) == _cycle_details(b)


def test_cycle_is_canonically_rotated():
    # cycle entered from any node reports starting at the smallest member.
    details = _cycle_details({"m": ["b"], "b": ["a"], "a": ["m"]})
    assert details == ["Circular dependency: a -> m -> b -> a"]
