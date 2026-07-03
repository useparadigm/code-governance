from __future__ import annotations

from dataclasses import asdict
from fnmatch import fnmatch

from code_governance.dep_graph import DependencyGraph
from code_governance.schemas import (
    EdgeDetail,
    GovernanceConfig,
    ModuleMetrics,
    RuleKind,
    Severity,
    Violation,
)


def _matches_any(name: str, patterns) -> bool:
    """True if `name` equals or glob-matches any pattern (fnmatch: *, ?, []).
    Plain names with no wildcards behave as exact matches (backward compatible)."""
    return any(name == p or fnmatch(name, p) for p in patterns)


def _transitive_evidence(edge_details: list[EdgeDetail], path: list[str]) -> list[dict]:
    """Collect evidence for each hop in a transitive path."""
    evidence: list[dict] = []
    for i in range(len(path) - 1):
        evidence.extend(_evidence_for_edge(edge_details, path[i], path[i + 1]))
    return evidence


def _evidence_for_edge(edge_details: list[EdgeDetail], src: str, tgt: str) -> list[dict]:
    seen: set[tuple[str, int]] = set()
    results: list[dict] = []
    for e in edge_details:
        if e.source_module != src or e.target_module != tgt:
            continue
        key = (e.source_file, e.line)
        if key in seen:
            continue
        seen.add(key)
        results.append(asdict(e))
    return results


def check_no_cycles(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    if not config.rules.no_cycles:
        return []

    excluded = set(config.rules.exclude_from_cycles)
    module_names = {m.name for m in config.modules} - excluded
    adjacency: dict[str, set[str]] = {}
    for mod in module_names:
        adjacency[mod] = (graph.get_module_dependencies(mod) & module_names) - excluded

    # Report one violation per strongly connected component (a maximal cluster of
    # mutually-reachable modules), not per elementary cycle. Dense clusters contain
    # exponentially many elementary cycles; enumerating them floods the report and
    # hides the real signal — that this *set* of modules is entangled. SCCs are
    # unique and deterministic, so output is stable and every cyclic relationship
    # is surfaced exactly once. A representative shortest cycle is shown as evidence.
    sccs = _strongly_connected_components(adjacency)

    violations: list[Violation] = []
    for scc in sccs:
        if len(scc) < 2:
            continue
        cycle = _representative_cycle(scc, adjacency)
        cycle_str = " -> ".join(cycle + [cycle[0]])
        if len(scc) > len(set(cycle)):
            detail = (
                f"Circular dependency among {len(scc)} modules "
                f"{{{', '.join(scc)}}}: {cycle_str}"
            )
        else:
            detail = f"Circular dependency: {cycle_str}"
        evidence: list[dict] = []
        for i in range(len(cycle)):
            src = cycle[i]
            tgt = cycle[(i + 1) % len(cycle)]
            evidence.extend(_evidence_for_edge(graph.edge_details, src, tgt))
        violations.append(Violation(
            rule=RuleKind.NO_CYCLES,
            module=scc[0],
            detail=detail,
            evidence=evidence,
        ))

    return violations


def check_no_file_cycles(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    """File-level circular imports (madge --circular equivalent). Operates on the
    resolved file -> file graph, so it catches cycles between files inside the
    same module that module-level cycle detection cannot see."""
    if not config.rules.no_file_cycles:
        return []

    adjacency: dict[str, set[str]] = {
        src: {tgt for tgt in targets if tgt != src}
        for src, targets in graph.file_edges.items()
    }

    sccs = _strongly_connected_components(adjacency)

    violations: list[Violation] = []
    for scc in sccs:
        if len(scc) < 2:
            continue
        cycle = _representative_cycle(scc, adjacency)
        cycle_str = " -> ".join(cycle + [cycle[0]])
        if len(scc) > len(set(cycle)):
            listed = ", ".join(scc[:10]) + (", ..." if len(scc) > 10 else "")
            detail = (
                f"Circular file dependency among {len(scc)} files "
                f"{{{listed}}}: {cycle_str}"
            )
        else:
            detail = f"Circular file dependency: {cycle_str}"
        evidence: list[dict] = []
        for i in range(len(cycle)):
            src = cycle[i]
            tgt = cycle[(i + 1) % len(cycle)]
            evidence.extend(_evidence_for_edge(graph.file_edge_details, src, tgt))
        violations.append(Violation(
            rule=RuleKind.NO_FILE_CYCLES,
            module=scc[0],
            detail=detail,
            evidence=evidence,
        ))

    return violations


def check_enforce_layers(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    if not config.rules.enforce_layers or not config.layers.order:
        return []

    layer_rank = {layer: i for i, layer in enumerate(config.layers.order)}
    module_layer = {m.name: m.layer for m in config.modules if m.layer}

    violations: list[Violation] = []
    direct_layer_violations: set[tuple[str, str]] = set()

    for src_mod, deps in graph.module_edges.items():
        src_layer = module_layer.get(src_mod)
        if not src_layer or src_layer not in layer_rank:
            continue
        src_rank = layer_rank[src_layer]

        for dep_mod in deps:
            dep_layer = module_layer.get(dep_mod)
            if not dep_layer or dep_layer not in layer_rank:
                continue
            dep_rank = layer_rank[dep_layer]

            if dep_rank > src_rank:
                direct_layer_violations.add((src_mod, dep_mod))
                evidence = _evidence_for_edge(graph.edge_details, src_mod, dep_mod)
                violations.append(Violation(
                    rule=RuleKind.ENFORCE_LAYERS,
                    module=src_mod,
                    detail=f"Layer violation: '{src_mod}' ({src_layer}) imports '{dep_mod}' ({dep_layer})",
                    evidence=evidence,
                ))

    if config.rules.transitive:
        module_names = {m.name for m in config.modules}
        for src_mod in module_names:
            src_layer = module_layer.get(src_mod)
            if not src_layer or src_layer not in layer_rank:
                continue
            src_rank = layer_rank[src_layer]

            transitive = graph.get_transitive_dependencies(src_mod)
            for dep_mod, path in transitive.items():
                if (src_mod, dep_mod) in direct_layer_violations:
                    continue
                dep_layer = module_layer.get(dep_mod)
                if not dep_layer or dep_layer not in layer_rank:
                    continue
                dep_rank = layer_rank[dep_layer]

                if dep_rank > src_rank:
                    evidence = _transitive_evidence(graph.edge_details, path)
                    chain = " → ".join(path)
                    violations.append(Violation(
                        rule=RuleKind.ENFORCE_LAYERS,
                        module=src_mod,
                        detail=f"Transitive layer violation: '{src_mod}' ({src_layer}) transitively imports '{dep_mod}' ({dep_layer}) via {chain}",
                        evidence=evidence,
                    ))

    return violations


def check_enforce_cannot_depend_on(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    if not config.rules.enforce_cannot_depend_on:
        return []

    forbidden: dict[str, set[str]] = {}
    for mod in config.modules:
        forbidden[mod.name] = set(mod.cannot_depend_on)

    violations: list[Violation] = []
    direct_forbidden: set[tuple[str, str]] = set()

    for src_mod, deps in graph.module_edges.items():
        if src_mod not in forbidden:
            continue
        for dep_mod in deps:
            if _matches_any(dep_mod, forbidden.get(src_mod, set())):
                direct_forbidden.add((src_mod, dep_mod))
                evidence = _evidence_for_edge(graph.edge_details, src_mod, dep_mod)
                violations.append(Violation(
                    rule=RuleKind.ENFORCE_CANNOT_DEPEND_ON,
                    module=src_mod,
                    detail=f"Forbidden dependency: '{src_mod}' imports '{dep_mod}' (cannot_depend_on: {sorted(forbidden.get(src_mod, set()))})",
                    evidence=evidence,
                ))

    if config.rules.transitive:
        for src_mod in forbidden:
            if not forbidden[src_mod]:
                continue
            transitive = graph.get_transitive_dependencies(src_mod)
            for dep_mod, path in transitive.items():
                if _matches_any(dep_mod, forbidden[src_mod]) and (src_mod, dep_mod) not in direct_forbidden:
                    evidence = _transitive_evidence(graph.edge_details, path)
                    chain = " → ".join(path)
                    violations.append(Violation(
                        rule=RuleKind.ENFORCE_CANNOT_DEPEND_ON,
                        module=src_mod,
                        detail=f"Forbidden transitive dependency: '{src_mod}' transitively imports '{dep_mod}' via {chain}",
                        evidence=evidence,
                    ))

    return violations


def check_can_only_depend_on(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    """Allowlist: a module with `can_only_depend_on` set may import only those
    internal modules (plus itself). Any other internal dependency is a violation."""
    if not config.rules.enforce_can_only_depend_on:
        return []

    module_names = {m.name for m in config.modules}
    allow: dict[str, list[str]] = {
        m.name: list(m.can_only_depend_on)
        for m in config.modules
        if m.can_only_depend_on is not None
    }
    if not allow:
        return []

    violations: list[Violation] = []
    for src_mod, allowed in allow.items():
        for dep_mod in graph.module_edges.get(src_mod, {}):
            if dep_mod == src_mod or dep_mod not in module_names:
                continue
            if _matches_any(dep_mod, allowed):
                continue
            evidence = _evidence_for_edge(graph.edge_details, src_mod, dep_mod)
            violations.append(Violation(
                rule=RuleKind.CAN_ONLY_DEPEND_ON,
                module=src_mod,
                detail=f"Disallowed dependency: '{src_mod}' imports '{dep_mod}' (can_only_depend_on: {sorted(allowed)})",
                evidence=evidence,
            ))
    return violations


def check_independence(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    """Each declared group of modules must be mutually independent: no module in
    the group may import any other module in the same group (direct, or transitive
    when `transitive` is enabled)."""
    groups = config.rules.independence
    if not groups:
        return []

    violations: list[Violation] = []
    for group in groups:
        members = [m for m in group]
        member_set = set(members)
        for src_mod in members:
            others = member_set - {src_mod}
            for dep_mod in graph.module_edges.get(src_mod, {}):
                if dep_mod in others:
                    evidence = _evidence_for_edge(graph.edge_details, src_mod, dep_mod)
                    violations.append(Violation(
                        rule=RuleKind.INDEPENDENCE,
                        module=src_mod,
                        detail=f"Independence violation: '{src_mod}' imports '{dep_mod}' (declared independent: {sorted(member_set)})",
                        evidence=evidence,
                    ))
            if config.rules.transitive:
                direct = set(graph.module_edges.get(src_mod, {}))
                for dep_mod, path in graph.get_transitive_dependencies(src_mod).items():
                    if dep_mod in others and dep_mod not in direct:
                        evidence = _transitive_evidence(graph.edge_details, path)
                        chain = " → ".join(path)
                        violations.append(Violation(
                            rule=RuleKind.INDEPENDENCE,
                            module=src_mod,
                            detail=f"Independence violation (transitive): '{src_mod}' reaches '{dep_mod}' via {chain}",
                            evidence=evidence,
                        ))
    return violations


def check_must_not_reach(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    """Reachability contract: no `source` module may transitively reach any
    `target` module. Targets accept exact names or globs."""
    constraints = config.rules.must_not_reach
    if not constraints:
        return []

    violations: list[Violation] = []
    for c in constraints:
        for src_mod in c.source:
            transitive = graph.get_transitive_dependencies(src_mod)
            for dep_mod, path in transitive.items():
                if _matches_any(dep_mod, c.target):
                    evidence = _transitive_evidence(graph.edge_details, path)
                    chain = " → ".join(path)
                    violations.append(Violation(
                        rule=RuleKind.MUST_NOT_REACH,
                        module=src_mod,
                        detail=f"Reachability violation: '{src_mod}' must not reach '{dep_mod}' (via {chain})",
                        evidence=evidence,
                    ))
    return violations


def check_no_orphans(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    """Flag modules with no incoming and no outgoing internal edges — dead or
    forgotten modules. Warning severity by default (entrypoints are legitimately
    orphaned); exclude them via `exclude_from_orphans`."""
    if not config.rules.no_orphans:
        return []

    excluded = set(config.rules.exclude_from_orphans)
    module_names = {m.name for m in config.modules}

    has_incoming: set[str] = set()
    for src_mod, deps in graph.module_edges.items():
        for dep_mod in deps:
            if dep_mod in module_names:
                has_incoming.add(dep_mod)

    violations: list[Violation] = []
    for mod in config.modules:
        if mod.name in excluded:
            continue
        outgoing = {d for d in graph.module_edges.get(mod.name, {}) if d in module_names}
        if not outgoing and mod.name not in has_incoming:
            violations.append(Violation(
                rule=RuleKind.NO_ORPHANS,
                module=mod.name,
                detail=f"Orphan module: '{mod.name}' has no incoming or outgoing dependencies",
                severity=Severity.WARNING,
            ))
    return violations


def check_max_public_surface(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    threshold = config.rules.max_public_surface
    if threshold is None:
        return []

    violations: list[Violation] = []
    for mod in config.modules:
        total = len(graph.symbols_per_module.get(mod.name, set()))
        external = len(graph.externally_used_symbols.get(mod.name, set()))
        if total == 0:
            continue
        ratio = external / total
        if ratio > threshold:
            violations.append(Violation(
                rule=RuleKind.MAX_PUBLIC_SURFACE,
                module=mod.name,
                detail=f"Public surface {ratio:.2f} exceeds threshold {threshold} ({external}/{total} symbols used externally)",
                severity=Severity.WARNING,
            ))

    return violations


def check_min_cohesion(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    threshold = config.rules.min_cohesion
    if threshold is None:
        return []

    violations: list[Violation] = []
    for mod in config.modules:
        internal = graph.module_internal_edges.get(mod.name, 0)
        external = graph.module_external_edges.get(mod.name, 0)
        total = internal + external
        if total == 0:
            continue
        ratio = internal / total
        if ratio < threshold:
            violations.append(Violation(
                rule=RuleKind.MIN_COHESION,
                module=mod.name,
                detail=f"Cohesion {ratio:.2f} below threshold {threshold} ({internal} internal / {total} total edges)",
                severity=Severity.WARNING,
            ))

    return violations


def compute_module_metrics(graph: DependencyGraph, config: GovernanceConfig) -> list[ModuleMetrics]:
    metrics: list[ModuleMetrics] = []
    for mod in config.modules:
        total_symbols = len(graph.symbols_per_module.get(mod.name, set()))
        ext_symbols = len(graph.externally_used_symbols.get(mod.name, set()))
        internal = graph.module_internal_edges.get(mod.name, 0)
        external = graph.module_external_edges.get(mod.name, 0)
        total_edges = internal + external

        metrics.append(ModuleMetrics(
            name=mod.name,
            total_symbols=total_symbols,
            externally_used_symbols=ext_symbols,
            internal_edges=internal,
            external_edges=external,
            public_surface_ratio=round(ext_symbols / total_symbols, 4) if total_symbols > 0 else None,
            cohesion_ratio=round(internal / total_edges, 4) if total_edges > 0 else None,
        ))
    return metrics


ALL_RULES = [
    check_no_cycles,
    check_no_file_cycles,
    check_enforce_layers,
    check_enforce_cannot_depend_on,
    check_can_only_depend_on,
    check_independence,
    check_must_not_reach,
    check_no_orphans,
    check_max_public_surface,
    check_min_cohesion,
]


_SEVERITY_BY_NAME = {s.value: s for s in Severity}


def apply_severity_overrides(violations: list[Violation], config: GovernanceConfig) -> list[Violation]:
    """Apply per-rule severity overrides from config. `severity = {rule: level}`
    where level is error/warning/info/off. "off" drops the rule's violations."""
    overrides = config.rules.severity
    if not overrides:
        return violations

    result: list[Violation] = []
    for v in violations:
        level = overrides.get(v.rule.value)
        if level is None:
            result.append(v)
            continue
        level = level.lower()
        if level == "off":
            continue
        new_sev = _SEVERITY_BY_NAME.get(level)
        if new_sev is not None and new_sev != v.severity:
            v = v.model_copy(update={"severity": new_sev})
        result.append(v)
    return result


def run_all_rules(graph: DependencyGraph, config: GovernanceConfig) -> list[Violation]:
    """Run every rule and apply severity overrides. Single entry point so all
    callers (full scan, diff, auto) get identical rule + severity behavior."""
    violations: list[Violation] = []
    for rule_fn in ALL_RULES:
        violations.extend(rule_fn(graph, config))
    return apply_severity_overrides(violations, config)


def _strongly_connected_components(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC algorithm with sorted iteration for deterministic output.
    Returns components as sorted member lists, ordered by (size, members)."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    result: list[list[str]] = []

    neighbors = {n: sorted(adjacency.get(n, set())) for n in adjacency}

    import sys as _sys
    # File-level graphs can hold tens of thousands of nodes and Tarjan recurses
    # once per node on a chain, so scale the limit with graph size.
    _sys.setrecursionlimit(max(10000, len(adjacency) + 1000, _sys.getrecursionlimit()))

    def strongconnect(v: str):
        index_of[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in neighbors.get(v, ()):
            if w not in adjacency:
                continue
            if w not in index_of:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index_of[w])
        if low[v] == index_of[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(sorted(comp))

    for node in sorted(adjacency):
        if node not in index_of:
            strongconnect(node)

    result.sort(key=lambda c: (len(c), c))
    return result


def _representative_cycle(scc: list[str], adjacency: dict[str, set[str]]) -> list[str]:
    """Find a short representative cycle within an SCC: BFS from the smallest
    member back to itself using only intra-SCC edges. Deterministic (sorted)."""
    from collections import deque

    members = set(scc)
    start = scc[0]
    # BFS for the shortest path start -> start (length >= 2).
    queue: deque[list[str]] = deque([[start]])
    visited: set[str] = set()
    while queue:
        path = queue.popleft()
        node = path[-1]
        for nxt in sorted(adjacency.get(node, set())):
            if nxt not in members:
                continue
            if nxt == start and len(path) >= 2:
                return path
            if nxt == start and len(path) == 1:
                return [start]  # self-loop (shouldn't happen for modules)
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append(path + [nxt])
    return scc  # fallback: SCC guarantees a cycle exists
