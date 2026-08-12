from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

from ast_grep_py import SgNode

from code_governance.languages.tsconfig import TsConfig, load_tsconfig
from code_governance.schemas import ClassInfo, ExportInfo, FileExtractionResult, ImportInfo

if TYPE_CHECKING:
    from code_governance.schemas import GovernanceConfig


_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")

# Specifiers a bundler turns into an asset, not a module edge. `.json` is here
# too: it is never a scanned node, so the only thing it can resolve to is a
# wrong neighbour.
_ASSET_EXTENSIONS = frozenset({
    ".css", ".scss", ".sass", ".less", ".styl",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg",
    ".json", ".md", ".txt", ".csv", ".wasm", ".graphql", ".gql",
})

_MAX_WORKSPACE_LOOKUP_DEPTH = 3


def discover_workspace_packages(root: Path) -> list[tuple[str, str, dict]]:
    """(package name, root-relative dir, exports map) for every workspace package
    under `root`, longest name first so `@x/a-b` wins over `@x/a`.

    Reads the `workspaces` field of the nearest enclosing package.json. Only packages
    that live under `root` are returned — anything outside it has no importables to
    match anyway."""
    manifest = _find_workspace_manifest(root)
    if manifest is None:
        return []
    manifest_dir, patterns = manifest

    found: list[tuple[str, str, dict]] = []
    seen: set[Path] = set()
    for pattern in patterns:
        try:
            matches = sorted(manifest_dir.glob(f"{pattern.rstrip('/')}/package.json"))
        except (ValueError, OSError):
            continue
        for pkg_json in matches:
            pkg_dir = pkg_json.parent.resolve()
            if pkg_dir in seen:
                continue
            seen.add(pkg_dir)
            try:
                rel_dir = pkg_dir.relative_to(root)
            except ValueError:
                continue  # package lives outside the scanned source root
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: failed to parse {pkg_json}: {e}", file=sys.stderr)
                continue
            name = data.get("name")
            if not isinstance(name, str) or not name:
                continue
            found.append((name, rel_dir.as_posix(), _exports_map(data)))
    found.sort(key=lambda item: (-len(item[0]), item[0]))
    return found


def _find_workspace_manifest(root: Path) -> Optional[tuple[Path, list[str]]]:
    for candidate in [root, *list(root.parents)[:_MAX_WORKSPACE_LOOKUP_DEPTH]]:
        pkg_json = candidate / "package.json"
        if not pkg_json.exists():
            continue
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        workspaces = data.get("workspaces")
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages")
        if isinstance(workspaces, list):
            patterns = [w for w in workspaces if isinstance(w, str)]
            if patterns:
                return candidate, patterns
    return None


def _exports_map(package_json: dict) -> dict[str, str]:
    """Flatten package.json `exports` / `main` into subpath -> file target."""
    out: dict[str, str] = {}
    exports = package_json.get("exports")
    if isinstance(exports, str):
        out["."] = exports
    elif isinstance(exports, dict):
        for subpath, target in exports.items():
            if not isinstance(subpath, str) or not subpath.startswith("."):
                continue
            resolved = _first_string_target(target)
            if resolved:
                out[subpath] = resolved
    main = package_json.get("main")
    if isinstance(main, str) and "." not in out:
        out["."] = main
    return out


def _first_string_target(target) -> Optional[str]:
    """Conditional exports nest by condition (`import`/`require`/`default`); any of
    them points at the same module for graph purposes, so take the first."""
    if isinstance(target, str):
        return target
    if isinstance(target, dict):
        for value in target.values():
            resolved = _first_string_target(value)
            if resolved:
                return resolved
    if isinstance(target, list):
        for value in target:
            resolved = _first_string_target(value)
            if resolved:
                return resolved
    return None


class TypeScriptPatterns:
    language = "tsx"
    extensions = _EXTENSIONS
    test_file_patterns: list[tuple[str, str]] = [
        ("", f".{kind}{ext}") for kind in ("test", "spec") for ext in _EXTENSIONS
    ]

    def __init__(self) -> None:
        self._tsconfig: Optional[TsConfig] = None
        self._repo_root: Optional[Path] = None
        self._workspace_packages: list[tuple[str, str, dict]] = []
        self._scoped_tsconfigs: list[tuple[str, TsConfig]] = []

    def initialize(self, repo_root: Path, config: "GovernanceConfig") -> None:
        self._repo_root = Path(repo_root).resolve()
        self._tsconfig = self._find_tsconfig(self._repo_root)
        self._workspace_packages = discover_workspace_packages(self._repo_root)
        self._scoped_tsconfigs = self._discover_scoped_tsconfigs(self._repo_root)

    @staticmethod
    def _find_tsconfig(root: Path) -> Optional[TsConfig]:
        """Source root first, then ancestors — monorepo packages routinely keep their
        aliases in a tsconfig one or two levels up (`tsconfig.base.json` at the repo
        root). Targets that resolve outside the source root simply never match an
        importable, so an unrelated ancestor config cannot invent edges."""
        names = ["tsconfig.json", "tsconfig.base.json"]
        for candidate in [root, *list(root.parents)[:3]]:
            found = load_tsconfig(candidate, names)
            if found is not None:
                return found
        return None

    @staticmethod
    def _discover_scoped_tsconfigs(root: Path) -> list[tuple[str, TsConfig]]:
        """Every nested tsconfig, deepest first.

        In a monorepo each app and package declares its own `@/*`, all pointing at
        different directories. Resolving every alias through the root config maps
        `@/lib/x` in one app onto another app's file, or onto nothing — so the
        config that applies is the nearest one above the *importing* file, which is
        also how tsc itself resolves.
        """
        found: list[tuple[str, TsConfig]] = []
        seen: set[Path] = set()
        for depth in ("*", "*/*", "*/*/*"):
            for path in sorted(root.glob(f"{depth}/tsconfig.json")):
                config_dir = path.parent
                if config_dir in seen or _should_skip_dir(config_dir, root):
                    continue
                seen.add(config_dir)
                loaded = load_tsconfig(config_dir, "tsconfig.json")
                if loaded is not None and loaded.paths:
                    found.append((config_dir.relative_to(root).as_posix(), loaded))
        found.sort(key=lambda item: (-item[0].count("/"), item[0]))
        return found

    def _tsconfig_for(self, importing_file: str) -> Optional[TsConfig]:
        for rel_dir, cfg in self._scoped_tsconfigs:
            if importing_file == rel_dir or importing_file.startswith(rel_dir + "/"):
                return cfg
        return self._tsconfig

    def _to_scan_relative(self, absolute: Path) -> str:
        """Express an absolute path relative to the scanned source root, so it
        matches source-root-relative importables. ``_repo_root`` is the scanned
        source root (the engine passes ``repo_root/config.root``); alias targets
        normalized against anything else never match an importable key and every
        resolved edge is silently dropped."""
        if self._repo_root is not None:
            try:
                return str(absolute.relative_to(self._repo_root)).replace("\\", "/")
            except ValueError:
                pass
        return str(absolute).replace("\\", "/")

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

    def extract_exports(self, root: SgNode, file_path: str) -> list[ExportInfo]:
        """Every name this file exports, in source order.

        `export { a as b }` records `b` — the public name, which is what an
        importer writes and therefore the only name the two ends can be matched
        on. A re-export keeps its `from "./x"` specifier so barrels can be walked
        back to the declaring file.
        """
        out: list[ExportInfo] = []
        seen: set[str] = set()

        def add(name: str, line: int, source: Optional[str] = None) -> None:
            if name in seen:
                return
            seen.add(name)
            out.append(ExportInfo(name=name, line=line, source=source))

        for node in root.find_all(kind="export_statement"):
            line = node.range().start.line + 1
            if node.text()[:20].startswith("export default"):
                add("default", line)
                continue
            source = _export_source(node)
            if source is not None and any(
                c.kind() == "namespace_export" or c.text() == "*" for c in node.children()
            ):
                # `export * from "./x"` exports names this file never spells out;
                # recorded unnamed so the barrel walk can follow it.
                out.append(ExportInfo(name="*", line=line, source=source))
                continue
            for child in node.children():
                for name in _exported_declaration_names(child):
                    add(name, line, source)
        return out

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
        # `import './Spinner.scss'` is a bundler asset, not a module. Stripping the
        # extension and falling back to the directory index resolves it onto the
        # sibling barrel — which re-exports the importer, inventing a cycle.
        if PurePosixPath(import_source).suffix in _ASSET_EXTENSIONS:
            return None
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

        tsconfig = self._tsconfig_for(importing_file)
        alias_targets = self._apply_alias(import_source, tsconfig)
        workspace_targets = self._apply_workspace(import_source)
        if alias_targets:
            out.extend(alias_targets)
        elif workspace_targets:
            out.extend(workspace_targets)
        elif import_source.startswith("."):
            rel = self._resolve_relative(import_source, importing_file)
            if rel:
                out.append(rel)
        elif import_source.startswith("/"):
            out.append(import_source.lstrip("/"))
        else:
            has_base_url = bool(tsconfig and tsconfig.base_url)
            if has_base_url:
                out.append(self._from_base_url(import_source, tsconfig))
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

    def _apply_workspace(self, import_source: str) -> list[str]:
        """`@scope/pkg/sub` -> the workspace package's source file.

        npm/pnpm/yarn workspace packages look like third-party specifiers, so without
        this every cross-package edge in a monorepo is silently dropped."""
        if not self._workspace_packages or import_source.startswith("."):
            return []
        for name, pkg_dir, exports in self._workspace_packages:
            if import_source != name and not import_source.startswith(name + "/"):
                continue
            rest = import_source[len(name):].lstrip("/")
            subpath = "." if not rest else f"./{rest}"
            candidates: list[str] = []
            target = exports.get(subpath)
            if target is None:
                for pattern, value in exports.items():
                    if "*" not in pattern:
                        continue
                    prefix, _, suffix = pattern.partition("*")
                    if subpath.startswith(prefix) and subpath.endswith(suffix):
                        matched = subpath[len(prefix): len(subpath) - len(suffix)] if suffix else subpath[len(prefix):]
                        candidates.append(value.replace("*", matched))
            else:
                candidates.append(target)
            # exports maps are optional — fall back to the conventional layouts
            candidates.append(f"src/{rest}" if rest else "src")
            candidates.append(rest or "index")
            return [f"{pkg_dir}/{c.lstrip('./')}".strip("/") for c in candidates]
        return []

    def _apply_alias(self, import_source: str, tsconfig: Optional[TsConfig]) -> list[str]:
        if not tsconfig or not tsconfig.paths:
            return []
        results: list[str] = []
        for pattern, targets in tsconfig.paths.items():
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if import_source.startswith(prefix) and import_source.endswith(suffix):
                    matched = import_source[len(prefix): len(import_source) - len(suffix)] if suffix else import_source[len(prefix):]
                    for target in targets:
                        replaced = target.replace("*", matched)
                        results.append(self._normalize_to_repo_relative(replaced, tsconfig))
            elif import_source == pattern:
                for target in targets:
                    results.append(self._normalize_to_repo_relative(target, tsconfig))
        return results

    def _normalize_to_repo_relative(self, target: str, tsconfig: Optional[TsConfig]) -> str:
        if tsconfig is None or self._repo_root is None:
            return target.lstrip("./")
        base = tsconfig.config_dir
        if tsconfig.base_url:
            base = (base / tsconfig.base_url).resolve()
        absolute = (base / target).resolve()
        return self._to_scan_relative(absolute)

    def _from_base_url(self, import_source: str, tsconfig: Optional[TsConfig]) -> str:
        if tsconfig is None or self._repo_root is None or not tsconfig.base_url:
            return import_source
        base = (tsconfig.config_dir / tsconfig.base_url).resolve()
        absolute = (base / import_source).resolve()
        if self._repo_root is None:
            return import_source
        try:
            return str(absolute.relative_to(self._repo_root)).replace("\\", "/")
        except ValueError:
            # Outside the scanned root — it can never match an importable, so keep
            # the original specifier rather than emitting an absolute path.
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
                for name, spec_type_only in names:
                    results.append(ImportInfo(source_module=source, imported_name=name, line=line, raw_statement=raw, type_only=type_only or spec_type_only))
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
                for name, spec_type_only in names:
                    results.append(ImportInfo(source_module=source, imported_name=name, line=line, raw_statement=raw, type_only=type_only or spec_type_only))
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

        # `await import("./heavy")` — code-split call sites are real runtime edges,
        # and they are exactly the ones a static-only pass reports as dead code.
        for node in root.find_all(kind="call_expression"):
            fn = node.field("function")
            if fn is None or fn.text() != "import":
                continue
            args = node.field("arguments")
            if args is None:
                continue
            source = _first_string_child(args)
            if source is None:
                continue
            results.append(ImportInfo(
                source_module=source,
                line=node.range().start.line + 1,
                raw_statement=node.text(),
                # `typeof import("x")` / `const x: import("x").T` parse as calls too,
                # but they are erased at compile time like `import type`.
                type_only=_in_type_position(node),
            ))

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


_SKIP_DIR_PARTS = frozenset({"node_modules", "dist", "build", "coverage"})


def _should_skip_dir(path: Path, root: Path) -> bool:
    """Dependency and build trees ship their own tsconfigs; those aliases describe
    someone else's source layout, not this repo's."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(p in _SKIP_DIR_PARTS or p.startswith(".") for p in parts)


_EXPORT_DECL_KINDS = {
    "function_declaration", "generator_function_declaration", "class_declaration",
    "abstract_class_declaration", "interface_declaration", "type_alias_declaration",
    "enum_declaration",
}


def _exported_declaration_names(child: SgNode) -> list[str]:
    """Public names contributed by one child of an `export_statement`."""
    kind = child.kind()
    if kind == "export_clause":
        names = []
        for spec in child.children():
            if spec.kind() != "export_specifier":
                continue
            field = spec.field("alias") or spec.field("name")
            if field:
                names.append(field.text())
        return names
    if kind in _EXPORT_DECL_KINDS:
        field = child.field("name")
        return [field.text()] if field else []
    if kind in ("lexical_declaration", "variable_declaration"):
        names = []
        for decl in child.children():
            if decl.kind() != "variable_declarator":
                continue
            field = decl.field("name")
            if field and field.kind() == "identifier":
                names.append(field.text())
        return names
    return []


_TYPE_CONTEXT_KINDS = {
    "type_query", "type_annotation", "type_alias_declaration", "type_arguments",
    "generic_type", "interface_declaration", "opting_type_annotation",
    "omitting_type_annotation", "type_predicate", "satisfies_expression",
}


def _in_type_position(node: SgNode, max_depth: int = 6) -> bool:
    parent = node.parent()
    depth = 0
    while parent is not None and depth < max_depth:
        if parent.kind() in _TYPE_CONTEXT_KINDS:
            return True
        parent = parent.parent()
        depth += 1
    return False


def _is_type_only_statement(node: SgNode, keyword: str) -> bool:
    """True for statement-level `import type` / `export type` forms, detected
    from the `type` keyword token immediately after the import/export keyword.
    The grammar exposes the token as kind `type` in clause forms but as an
    ERROR node in `export type * from ...` forms — accept both."""
    children = node.children()
    for i, child in enumerate(children):
        if child.kind() == keyword:
            if i + 1 >= len(children):
                return False
            nxt = children[i + 1]
            return nxt.kind() == "type" or (nxt.kind() == "ERROR" and nxt.text().strip() == "type")
    return False


def _first_string_child(node: SgNode) -> Optional[str]:
    for child in node.children():
        if child.kind() == "string":
            text = child.text().strip()
            if len(text) >= 2 and text[0] in ("'", '"', "`"):
                return text[1:-1]
    return None


def _collect_import_names(node: SgNode) -> list[tuple[str, bool]]:
    """(name, type_only) per specifier. `import { type Foo }` erases Foo at
    compile time, so its specifier carries type_only=True; default and
    namespace imports are always runtime."""
    names: list[tuple[str, bool]] = []
    for child in node.children():
        if child.kind() == "import_clause":
            for sub in child.children():
                if sub.kind() == "identifier":
                    names.append((sub.text(), False))
                elif sub.kind() == "named_imports":
                    for spec in sub.children():
                        if spec.kind() == "import_specifier":
                            name_field = spec.field("name") or spec.field("alias")
                            if name_field:
                                names.append((name_field.text(), _spec_is_type(spec)))
                elif sub.kind() == "namespace_import":
                    for spec in sub.children():
                        if spec.kind() == "identifier":
                            names.append((spec.text(), False))
    return names


def _spec_is_type(spec: SgNode) -> bool:
    children = spec.children()
    return bool(children) and children[0].kind() == "type"


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


def _collect_export_names(node: SgNode) -> list[tuple[str, bool]]:
    """(name, type_only) per specifier — see _collect_import_names."""
    names: list[tuple[str, bool]] = []
    for child in node.children():
        if child.kind() == "export_clause":
            for spec in child.children():
                if spec.kind() == "export_specifier":
                    name_field = spec.field("name") or spec.field("alias")
                    if name_field:
                        names.append((name_field.text(), _spec_is_type(spec)))
        elif child.kind() == "namespace_export":
            names.append(("*", False))
    return names


def _find_child_by_kind(node: SgNode, kind: str) -> Optional[SgNode]:
    for child in node.children():
        if child.kind() == kind:
            return child
    return None
