from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

from ast_grep_py import SgNode

from code_governance.languages.tsconfig import TsConfig, load_tsconfig
from code_governance.schemas import ClassInfo, FileExtractionResult, ImportInfo

if TYPE_CHECKING:
    from code_governance.schemas import GovernanceConfig


_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")


class TypeScriptPatterns:
    language = "tsx"
    extensions = _EXTENSIONS
    test_file_patterns: list[tuple[str, str]] = [
        ("", ext) for ext in (
            ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
            ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
        )
    ]

    def __init__(self) -> None:
        self._tsconfig: Optional[TsConfig] = None
        self._repo_root: Optional[Path] = None

    def initialize(self, repo_root: Path, config: "GovernanceConfig") -> None:
        self._repo_root = Path(repo_root).resolve()
        self._tsconfig = load_tsconfig(self._repo_root, ["tsconfig.json", "tsconfig.base.json"])

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
        if p.suffix in _EXTENSIONS:
            return str(p.with_suffix(""))
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
        candidates = self._expand_candidates(import_source, importing_file, config)
        for cand in candidates:
            resolved = self._lookup(cand, importable_map)
            if resolved:
                return resolved
        return None

    def _expand_candidates(
        self, import_source: str, importing_file: str, config: "GovernanceConfig"
    ) -> list[str]:
        out: list[str] = []

        alias_targets = self._apply_alias(import_source)
        if alias_targets:
            out.extend(alias_targets)
        elif import_source.startswith("."):
            rel = self._resolve_relative(import_source, importing_file)
            if rel:
                out.append(rel)
        elif import_source.startswith("/"):
            out.append(import_source.lstrip("/"))
        else:
            has_base_url = bool(self._tsconfig and self._tsconfig.base_url)
            if has_base_url:
                out.append(self._from_base_url(import_source))
            # Implicit base-URL fallback: many codebases import local modules with
            # bare specifiers (`scenes/urls`, `lib/api`) or a src-root alias
            # (`~/types`, `@/queries`) backed by tsconfig `baseUrl`/`paths`. When no
            # tsconfig is discovered (the common case for zero-config --auto on a
            # nested source dir), treat these as paths relative to the source root.
            # Importable keys are source-root-relative, so this matches exactly;
            # true third-party packages (`react`, `@posthog/icons`) simply find no
            # matching file and produce no edge. Skipped when a tsconfig baseUrl is
            # configured, since that already declares how bare specifiers resolve.
            if not has_base_url:
                out.extend(self._implicit_src_relative(import_source))
        return out

    @staticmethod
    def _implicit_src_relative(import_source: str) -> list[str]:
        for alias in ("~/", "@/"):
            if import_source.startswith(alias):
                return [import_source[len(alias):]]
        if import_source in ("~", "@"):
            return []
        first = import_source[0]
        # bare specifier with a path segment (local module), not a scoped package
        # (@scope/pkg) and not a single-word bare package (`react`, `kea`).
        if (first.isalnum() or first == "_") and "/" in import_source:
            return [import_source]
        return []

    def _apply_alias(self, import_source: str) -> list[str]:
        if not self._tsconfig or not self._tsconfig.paths:
            return []
        results: list[str] = []
        for pattern, targets in self._tsconfig.paths.items():
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if import_source.startswith(prefix) and import_source.endswith(suffix):
                    matched = import_source[len(prefix): len(import_source) - len(suffix)] if suffix else import_source[len(prefix):]
                    for target in targets:
                        replaced = target.replace("*", matched)
                        results.append(self._normalize_to_repo_relative(replaced))
            elif import_source == pattern:
                for target in targets:
                    results.append(self._normalize_to_repo_relative(target))
        return results

    def _normalize_to_repo_relative(self, target: str) -> str:
        if self._tsconfig is None or self._repo_root is None:
            return target.lstrip("./")
        base = self._tsconfig.config_dir
        if self._tsconfig.base_url:
            base = (base / self._tsconfig.base_url).resolve()
        absolute = (base / target).resolve()
        try:
            return str(absolute.relative_to(self._repo_root)).replace("\\", "/")
        except ValueError:
            return str(absolute).replace("\\", "/")

    def _from_base_url(self, import_source: str) -> str:
        if self._tsconfig is None or self._repo_root is None or not self._tsconfig.base_url:
            return import_source
        base = (self._tsconfig.config_dir / self._tsconfig.base_url).resolve()
        absolute = (base / import_source).resolve()
        try:
            return str(absolute.relative_to(self._repo_root)).replace("\\", "/")
        except ValueError:
            return import_source

    def _resolve_relative(self, import_source: str, importing_file: str) -> Optional[str]:
        importing_dir = PurePosixPath(importing_file).parent
        parts = list(importing_dir.parts)
        specifier = import_source
        while specifier.startswith("./") or specifier.startswith("../"):
            if specifier.startswith("../"):
                if not parts:
                    return None
                parts.pop()
                specifier = specifier[3:]
            else:
                specifier = specifier[2:]
        base = "/".join(parts)
        if specifier:
            return f"{base}/{specifier}" if base else specifier
        return base or None

    def _lookup(self, candidate: str, importable_map: dict[str, str]) -> Optional[str]:
        if not candidate:
            return None
        if candidate in importable_map:
            return importable_map[candidate]
        index_key = f"{candidate}/index"
        if index_key in importable_map:
            return importable_map[index_key]
        # Case A — candidate deeper than a known importable: longest ancestor.
        parts = candidate.split("/")
        for k in range(len(parts) - 1, 0, -1):
            ancestor = "/".join(parts[:k])
            if ancestor in importable_map:
                return importable_map[ancestor]
        # Case B — candidate is a directory containing known importables. Served
        # from a memoized prefix index (sorted -> deterministic), not an O(N) scan.
        return self._prefix_index(importable_map).get(candidate)

    def _prefix_index(self, importable_map: dict[str, str]) -> dict[str, str]:
        if getattr(self, "_prefix_index_map", None) is importable_map:
            return self._prefix_index_cache
        index: dict[str, str] = {}
        for key in sorted(importable_map):
            mod = importable_map[key]
            parts = key.split("/")
            for k in range(1, len(parts)):
                index.setdefault("/".join(parts[:k]), mod)
        self._prefix_index_map = importable_map
        self._prefix_index_cache = index
        return index

    def _extract_imports(self, root: SgNode) -> list[ImportInfo]:
        results: list[ImportInfo] = []

        for node in root.find_all(kind="import_statement"):
            line = node.range().start.line + 1
            raw = node.text()
            source = _first_string_child(node)
            if source is None:
                continue
            # `import type ...` is erased at compile time — it can never cause a
            # runtime import loop. (Per-specifier `import { type X }` may still
            # carry runtime imports, so only the statement form counts.)
            type_only = _is_type_only_statement(node, "import")
            names = _collect_import_names(node)
            if names:
                for name in names:
                    results.append(ImportInfo(source_module=source, imported_name=name, line=line, raw_statement=raw, type_only=type_only))
            else:
                results.append(ImportInfo(source_module=source, line=line, raw_statement=raw, type_only=type_only))

        for node in root.find_all(kind="export_statement"):
            source = _export_source(node)
            if source is None:
                continue
            line = node.range().start.line + 1
            raw = node.text()
            type_only = _is_type_only_statement(node, "export")
            names = _collect_export_names(node)
            if names:
                for name in names:
                    results.append(ImportInfo(source_module=source, imported_name=name, line=line, raw_statement=raw, type_only=type_only))
            else:
                results.append(ImportInfo(source_module=source, line=line, raw_statement=raw, type_only=type_only))

        for node in root.find_all(pattern="require($MOD)"):
            mod_node = node.get_match("MOD")
            if mod_node is None or mod_node.kind() != "string":
                continue
            text = mod_node.text().strip()
            if len(text) < 2:
                continue
            source = text[1:-1]
            line = node.range().start.line + 1
            raw = node.text()
            results.append(ImportInfo(source_module=source, line=line, raw_statement=raw))

        return results

    def _extract_classes(self, root: SgNode) -> list[ClassInfo]:
        results: list[ClassInfo] = []
        for node in root.find_all(kind="class_declaration"):
            name_node = node.field("name")
            if not name_node:
                continue
            name = name_node.text()
            bases: list[str] = []
            heritage = node.field("heritage") or _find_child_by_kind(node, "class_heritage")
            if heritage:
                for child in heritage.children():
                    if child.kind() in ("extends_clause", "implements_clause"):
                        for sub in child.children():
                            if sub.kind() not in ("extends", "implements", ","):
                                bases.append(sub.text())
            results.append(ClassInfo(name=name, base_classes=bases))
        return results

    def _extract_symbols(self, root: SgNode) -> list[str]:
        symbols: list[str] = []
        for kind in ("class_declaration", "function_declaration", "interface_declaration", "type_alias_declaration"):
            for node in root.find_all(kind=kind):
                name_node = node.field("name")
                if name_node:
                    symbols.append(name_node.text())
        for node in root.find_all(kind="lexical_declaration"):
            for child in node.children():
                if child.kind() == "variable_declarator":
                    name_node = child.field("name")
                    if name_node and name_node.kind() == "identifier":
                        symbols.append(name_node.text())
        return symbols


def _is_type_only_statement(node: SgNode, keyword: str) -> bool:
    """True for statement-level `import type` / `export type` forms, detected
    from the `type` keyword token immediately after the import/export keyword."""
    children = node.children()
    for i, child in enumerate(children):
        if child.kind() == keyword:
            return i + 1 < len(children) and children[i + 1].kind() == "type"
    return False


def _first_string_child(node: SgNode) -> Optional[str]:
    for child in node.children():
        if child.kind() == "string":
            text = child.text().strip()
            if len(text) >= 2 and text[0] in ("'", '"', "`"):
                return text[1:-1]
    return None


def _collect_import_names(node: SgNode) -> list[str]:
    names: list[str] = []
    for child in node.children():
        if child.kind() == "import_clause":
            for sub in child.children():
                if sub.kind() == "identifier":
                    names.append(sub.text())
                elif sub.kind() == "named_imports":
                    for spec in sub.children():
                        if spec.kind() == "import_specifier":
                            name_field = spec.field("name") or spec.field("alias")
                            if name_field:
                                names.append(name_field.text())
                elif sub.kind() == "namespace_import":
                    for spec in sub.children():
                        if spec.kind() == "identifier":
                            names.append(spec.text())
    return names


def _export_source(node: SgNode) -> Optional[str]:
    source_field = node.field("source")
    if source_field and source_field.kind() == "string":
        text = source_field.text().strip()
        if len(text) >= 2 and text[0] in ("'", '"', "`"):
            return text[1:-1]
    for child in node.children():
        if child.kind() == "string":
            text = child.text().strip()
            if len(text) >= 2 and text[0] in ("'", '"', "`"):
                return text[1:-1]
    return None


def _collect_export_names(node: SgNode) -> list[str]:
    names: list[str] = []
    for child in node.children():
        if child.kind() == "export_clause":
            for spec in child.children():
                if spec.kind() == "export_specifier":
                    name_field = spec.field("name") or spec.field("alias")
                    if name_field:
                        names.append(name_field.text())
        elif child.kind() == "namespace_export":
            names.append("*")
    return names


def _find_child_by_kind(node: SgNode, kind: str) -> Optional[SgNode]:
    for child in node.children():
        if child.kind() == kind:
            return child
    return None
