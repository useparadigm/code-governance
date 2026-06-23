from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

from ast_grep_py import SgNode

from code_governance.schemas import ClassInfo, FileExtractionResult, ImportInfo

if TYPE_CHECKING:
    from code_governance.schemas import GovernanceConfig

# Authoritative stdlib top-level module names (Python 3.10+). Used to keep bare
# absolute imports that shadow a local package name from creating false edges.
_STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()))


class PythonPatterns:
    language = "python"
    extensions = (".py",)
    test_file_patterns: list[tuple[str, str]] = [
        ("test_", ""),
        ("", "_test.py"),
        ("conftest.py", ""),
    ]

    def initialize(self, repo_root: Path, config: "GovernanceConfig") -> None:
        pass

    def extract(self, root: SgNode, file_path: str) -> FileExtractionResult:
        imports = self._extract_imports(root)
        classes = self._extract_classes(root)
        symbols = self._extract_symbols(root)
        return FileExtractionResult(
            file_path=file_path,
            imports=imports,
            classes=classes,
            symbols=symbols,
        )

    def file_to_importable(self, file_path: str) -> Optional[str]:
        p = PurePosixPath(file_path)
        if p.suffix == ".py":
            return str(p.with_suffix("")).replace("/", ".")
        return None

    def resolve_import(
        self,
        import_source: str,
        importing_file: str,
        config: "GovernanceConfig",
        importable_map: dict[str, str],
        module_files: dict[str, str],
        imported_name: Optional[str] = None,
    ) -> Optional[str]:
        is_relative = import_source.startswith(".")
        if is_relative:
            resolved = _resolve_relative_import(import_source, importing_file)
            # "" is a valid result (the package root); only None means out of bounds.
            if resolved is not None:
                import_source = resolved
        else:
            # A bare absolute import (`import json`, `from logging import x`) whose
            # top-level name is a standard-library module resolves to the stdlib in
            # Python 3, never to a local module of the same name — that would only
            # be reachable via a relative or package-prefixed import. Treat it as
            # external to avoid false edges when a local package shadows a stdlib
            # name (json, types, email, logging, queue, ...).
            top = import_source.split(".", 1)[0]
            if top in _STDLIB_MODULES and top != config.package_prefix:
                return None

        # Base candidates: the import source as written, plus the same source with
        # the source-root package name / configured prefix stripped, since file
        # importables are stored relative to the source root.
        bases = [import_source]
        root_pkg = config.root.rstrip("/").replace("/", ".")
        if root_pkg and root_pkg != "." and import_source.startswith(root_pkg + "."):
            bases.append(import_source[len(root_pkg) + 1:])
        if config.package_prefix:
            if import_source.startswith(config.package_prefix + "."):
                bases.append(import_source[len(config.package_prefix) + 1:])
            elif import_source == config.package_prefix:
                bases.append("")

        # `from X import name` may import a submodule rather than a symbol. Try the
        # submodule-qualified path first (more specific); if it isn't a real module
        # it simply won't match and we fall back to X itself. This also resolves
        # `from . import sub` and `from pkg import sub`, where X is the package root.
        candidates: list[str] = []
        if imported_name and imported_name != "*" and "." not in imported_name:
            for b in bases:
                candidates.append(f"{b}.{imported_name}" if b else imported_name)
        candidates.extend(b for b in bases if b)

        for candidate in candidates:
            hit = self._match_candidate(candidate, importable_map, config)
            if hit:
                return hit

        return None

    def _match_candidate(
        self,
        candidate: str,
        importable_map: dict[str, str],
        config: "GovernanceConfig",
    ) -> Optional[str]:
        if candidate in importable_map:
            return importable_map[candidate]

        # Case A — candidate is deeper than a known importable (`a.b.c` imported,
        # `a.b` is the real module file): match the longest existing ancestor.
        # O(depth), deterministic, and resolves to the closest module.
        parts = candidate.split(".")
        for k in range(len(parts) - 1, 0, -1):
            ancestor = ".".join(parts[:k])
            if ancestor in importable_map:
                return importable_map[ancestor]

        # Case B — candidate is a package that contains known importables
        # (`from . import subpkg` → candidate `subpkg`, importable `subpkg.x`).
        # Served from a prefix index built once per importable_map (sorted, so the
        # choice is deterministic), avoiding an O(N) scan on every lookup.
        prefix_index = self._prefix_index(importable_map)
        hit = prefix_index.get(candidate)
        if hit is not None:
            return hit

        for mod in sorted(config.modules, key=lambda m: m.path):
            mod_prefix = mod.path.rstrip("/").replace("/", ".")
            if not mod_prefix or mod_prefix == ".":
                continue
            if candidate == mod_prefix or candidate.startswith(mod_prefix + "."):
                return mod.name

        return None

    def _prefix_index(self, importable_map: dict[str, str]) -> dict[str, str]:
        """Map every package prefix of every importable to a module, built once per
        importable_map and memoized. Sorted iteration + setdefault makes the choice
        deterministic when a prefix spans modules."""
        if getattr(self, "_prefix_index_map", None) is importable_map:
            return self._prefix_index_cache
        index: dict[str, str] = {}
        for dotted in sorted(importable_map):
            mod = importable_map[dotted]
            parts = dotted.split(".")
            for k in range(1, len(parts)):
                index.setdefault(".".join(parts[:k]), mod)
        self._prefix_index_map = importable_map
        self._prefix_index_cache = index
        return index

    def _extract_imports(self, root: SgNode) -> list[ImportInfo]:
        results: list[ImportInfo] = []

        for node in root.find_all(kind="import_statement"):
            line = node.range().start.line + 1
            raw = node.text()
            for child in node.children():
                if child.kind() in ("dotted_name", "aliased_import"):
                    mod = child.text().split(" as ")[0]
                    results.append(ImportInfo(source_module=mod, line=line, raw_statement=raw))

        for node in root.find_all(kind="import_from_statement"):
            line = node.range().start.line + 1
            raw = node.text()
            mod_node = None
            names: list[str] = []
            for child in node.children():
                if child.kind() in ("dotted_name", "relative_import"):
                    if mod_node is None:
                        mod_node = child
                    else:
                        names.append(child.text())
                elif child.kind() == "wildcard_import":
                    names.append("*")

            mod = mod_node.text() if mod_node else ""
            if names:
                for name in names:
                    results.append(ImportInfo(source_module=mod, imported_name=name, line=line, raw_statement=raw))
            else:
                results.append(ImportInfo(source_module=mod, line=line, raw_statement=raw))

        return results

    def _extract_classes(self, root: SgNode) -> list[ClassInfo]:
        results: list[ClassInfo] = []
        for node in root.find_all(kind="class_definition"):
            name_node = node.field("name")
            if not name_node:
                continue
            name = name_node.text()
            bases: list[str] = []
            superclasses = node.field("superclasses")
            if superclasses:
                for child in superclasses.children():
                    if child.kind() not in ("(", ")", ","):
                        bases.append(child.text())
            results.append(ClassInfo(name=name, base_classes=bases))
        return results

    def _extract_symbols(self, root: SgNode) -> list[str]:
        symbols: list[str] = []
        for node in root.find_all(kind="class_definition"):
            name = node.field("name")
            if name:
                symbols.append(name.text())
        for node in root.find_all(kind="function_definition"):
            name = node.field("name")
            if name:
                symbols.append(name.text())
        return symbols


def _resolve_relative_import(import_source: str, importing_file: str) -> Optional[str]:
    """Resolve a relative import to a dotted path relative to the source root.

    `dir_parts` is the package containing the importing file (its directory, since
    file importables live in their own directory). N leading dots drop (N-1)
    trailing components from that package. Returns "" for the package root and
    None only when the dots reach above the source root.
    """
    dots = 0
    for ch in import_source:
        if ch == ".":
            dots += 1
        else:
            break

    remainder = import_source[dots:]
    dir_parts = PurePosixPath(importing_file).parts[:-1]

    if dots - 1 > len(dir_parts):
        return None

    base_parts = list(dir_parts[: len(dir_parts) - (dots - 1)])
    if remainder:
        base_parts.extend(remainder.split("."))
    return ".".join(base_parts)
