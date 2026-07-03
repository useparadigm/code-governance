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
    # Resolved file -> file edges (both sides are scanned source files). Only
    # populated when rules.no_file_cycles is enabled — building it costs a
    # second resolution pass.
    file_edges: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    file_edge_details: list[EdgeDetail] = field(default_factory=list)
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

    if config.rules.no_file_cycles:
        _build_file_edges(graph, extractions, config, patterns, module_files)

    return graph


def _build_file_edges(
    graph: DependencyGraph,
    extractions: list[FileExtractionResult],
    config: GovernanceConfig,
    patterns: "LanguagePatterns",
    module_files: dict[str, str],
) -> None:
    """Resolve every import to a concrete source *file* and record file -> file
    edges. Runs as a separate pass with a file-valued importable map so the
    per-map prefix-index memoization in the resolvers stays hot for each pass.

    The resolvers can fall back to module names or directory prefixes; anything
    that is not an actual scanned file is dropped, so the file graph contains
    only real file -> file relationships (no fabricated edges).
    """
    importable_files = _build_importable_file_map(extractions, patterns)
    all_files = {ext.file_path for ext in extractions}

    for ext in extractions:
        for imp in ext.imports:
            # Type-only imports (TYPE_CHECKING blocks, `import type`) are erased
            # at runtime and cannot cause an import loop. They still count as
            # module-level coupling, so only the file graph skips them.
            if imp.type_only:
                continue
            target_file = patterns.resolve_import(
                imp.source_module, ext.file_path, config, importable_files, module_files,
                imp.imported_name,
            )
            if not target_file or target_file == ext.file_path or target_file not in all_files:
                continue
            graph.file_edges[ext.file_path][target_file] += 1
            graph.file_edge_details.append(EdgeDetail(
                source_file=ext.file_path,
                source_module=ext.file_path,
                target_module=target_file,
                imported_name=imp.imported_name,
                line=imp.line,
                raw_statement=imp.raw_statement,
            ))


# Marks a package/directory prefix that has no entry file (__init__.py /
# index.*). Importing it executes nothing, so resolution must yield no edge;
# the sentinel is never a scanned file, so _build_file_edges drops it.
_NO_ENTRY_FILE = "\0"


def _build_importable_file_map(
    extractions: list[FileExtractionResult],
    patterns: "LanguagePatterns",
) -> dict[str, str]:
    """Importable name -> file path. Sorted iteration + setdefault keeps the
    mapping deterministic if two files ever share an importable name."""
    sep = "." if patterns.language == "python" else "/"
    mapping: dict[str, str] = {}
    for ext in sorted(extractions, key=lambda e: e.file_path):
        importable = patterns.file_to_importable(ext.file_path)
        if not importable:
            continue
        mapping.setdefault(importable, ext.file_path)
        # Importing a Python package executes its __init__.py, so the package
        # name itself must resolve to that file (Python importables are dotted;
        # this suffix never appears in TS "/"-separated importables).
        if importable.endswith(".__init__"):
            mapping.setdefault(importable[: -len(".__init__")], ext.file_path)

    # Close the map over every package/directory prefix. Without this, a bare
    # `import pkg` on a namespace package (or a directory import without an
    # index file) would fall through to the resolvers' prefix-index fallback
    # and fabricate an edge to an arbitrary child file. An exact sentinel hit
    # short-circuits that fallback; directories WITH an entry file resolve to
    # it (Python __init__ registered above, TS index.* here).
    prefixes: set[str] = set()
    for key in list(mapping):
        parts = key.split(sep)
        for k in range(1, len(parts)):
            prefixes.add(sep.join(parts[:k]))
    for prefix in prefixes:
        if prefix in mapping:
            continue
        index_key = f"{prefix}/index" if sep == "/" else None
        if index_key and mapping.get(index_key):
            mapping[prefix] = mapping[index_key]
        else:
            mapping[prefix] = _NO_ENTRY_FILE
    return mapping


def _build_file_to_module_map(
    extractions: list[FileExtractionResult],
    config: GovernanceConfig,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    # Sort modules so specific paths match before "." catch-all
    sorted_mods = sorted(config.modules, key=lambda m: (m.path == ".", m.path), reverse=False)
    catch_all = next((m for m in config.modules if m.path in (".", "./")), None)

    for ext in extractions:
        matched = False
        for mod in sorted_mods:
            mod_path = mod.path.rstrip("/")
            if mod_path == ".":
                continue
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
    sorted_mods = sorted(config.modules, key=lambda m: (m.path == ".", m.path), reverse=False)
    catch_all = next((m for m in config.modules if m.path in (".", "./")), None)

    for ext in extractions:
        matched_mod = None
        for mod in sorted_mods:
            mod_path = mod.path.rstrip("/")
            if mod_path == ".":
                continue
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


