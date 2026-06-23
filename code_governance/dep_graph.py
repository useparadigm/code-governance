from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from code_governance.languages import get_patterns
from code_governance.schemas import (
    EdgeDetail,
    FileExtractionResult,
    GovernanceConfig,
)

if TYPE_CHECKING:
    from code_governance.languages import LanguagePatterns


@dataclass
class DepEdge:
    source: str
    target: str
    count: int = 1


@dataclass
class DependencyGraph:
    module_edges: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    file_edges: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    module_internal_edges: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    module_external_edges: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    symbols_per_module: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    externally_used_symbols: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    edge_details: list[EdgeDetail] = field(default_factory=list)

    def get_module_dependencies(self, module_name: str) -> set[str]:
        return set(self.module_edges.get(module_name, {}).keys())

    def get_transitive_dependencies(self, module_name: str) -> dict[str, list[str]]:
        """Return all transitively reachable modules with shortest path from source.

        Returns {reachable_module: [module_name, ..., reachable_module]}.
        Handles cycles via visited set. Excludes self-loops.
        """
        from collections import deque

        result: dict[str, list[str]] = {}
        queue: deque[tuple[str, list[str]]] = deque()
        visited: set[str] = {module_name}

        for neighbor in self.module_edges.get(module_name, {}):
            if neighbor != module_name:
                path = [module_name, neighbor]
                queue.append((neighbor, path))
                visited.add(neighbor)
                result[neighbor] = path

        while queue:
            current, path = queue.popleft()
            for neighbor in self.module_edges.get(current, {}):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                new_path = path + [neighbor]
                result[neighbor] = new_path
                queue.append((neighbor, new_path))

        return result


def build_dependency_graph(
    extractions: list[FileExtractionResult],
    config: GovernanceConfig,
    patterns: Optional["LanguagePatterns"] = None,
) -> DependencyGraph:
    if patterns is None:
        patterns = get_patterns(config.language)
    graph = DependencyGraph()
    file_to_module = _build_file_to_module_map(extractions, config)
    module_files = _build_module_files_map(config)
    importable_map = _build_importable_map(extractions, config, patterns)

    for ext in extractions:
        src_module = file_to_module.get(ext.file_path)
        if not src_module:
            continue

        for sym in ext.symbols:
            graph.symbols_per_module[src_module].add(sym)

        for imp in ext.imports:
            target_module = patterns.resolve_import(
                imp.source_module, ext.file_path, config, importable_map, module_files,
                imp.imported_name,
            )
            if not target_module:
                continue

            graph.file_edges[ext.file_path][imp.source_module] += 1

            if target_module == src_module:
                graph.module_internal_edges[src_module] += 1
            else:
                graph.module_edges[src_module][target_module] += 1
                graph.module_external_edges[src_module] += 1
                if imp.imported_name:
                    graph.externally_used_symbols[target_module].add(imp.imported_name)
                graph.edge_details.append(EdgeDetail(
                    source_file=ext.file_path,
                    source_module=src_module,
                    target_module=target_module,
                    imported_name=imp.imported_name,
                    line=imp.line,
                    raw_statement=imp.raw_statement,
                ))

    return graph


def _sorted_modules_deepest_first(config: GovernanceConfig) -> list:
    """Sort modules so the most specific (deepest) path matches first under
    prefix matching. The "." catch-all is excluded — handle it separately."""
    return sorted(
        (m for m in config.modules if m.path not in (".", "./")),
        key=lambda m: len(m.path.rstrip("/").split("/")),
        reverse=True,
    )


def _build_file_to_module_map(
    extractions: list[FileExtractionResult],
    config: GovernanceConfig,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    sorted_mods = _sorted_modules_deepest_first(config)
    catch_all = next((m for m in config.modules if m.path in (".", "./")), None)

    for ext in extractions:
        matched = False
        for mod in sorted_mods:
            mod_path = mod.path.rstrip("/")
            if ext.file_path == mod_path or ext.file_path.startswith(mod_path + "/"):
                mapping[ext.file_path] = mod.name
                matched = True
                break
        if not matched and catch_all:
            mapping[ext.file_path] = catch_all.name
    return mapping


def _build_module_files_map(config: GovernanceConfig) -> dict[str, str]:
    return {mod.name: mod.path.rstrip("/") for mod in config.modules}


def _build_importable_map(
    extractions: list[FileExtractionResult],
    config: GovernanceConfig,
    patterns: "LanguagePatterns",
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    sorted_mods = _sorted_modules_deepest_first(config)
    catch_all = next((m for m in config.modules if m.path in (".", "./")), None)

    for ext in extractions:
        matched_mod = None
        for mod in sorted_mods:
            mod_path = mod.path.rstrip("/")
            if ext.file_path.startswith(mod_path + "/") or ext.file_path == mod_path:
                matched_mod = mod
                break
        if not matched_mod and catch_all:
            matched_mod = catch_all

        if matched_mod:
            importable = patterns.file_to_importable(ext.file_path)
            if importable:
                mapping[importable] = matched_mod.name
    return mapping


