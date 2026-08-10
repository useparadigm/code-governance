"""Build a file-level dependency payload for the drill-down viewer.

The linter's own graph answers "does this repo break its rules". This answers
"what does this repo actually look like" — every file, every resolved import, at
whatever directory depth you care to look. Same extractors, same import
resolution (tsconfig aliases, workspace packages, dynamic `import()`), so the
numbers agree with `--auto` by construction.

What it adds over :mod:`code_governance.dep_graph`:
  - per-edge type-only counts, so cycles can be judged at runtime semantics while
    coupling still counts every import
  - exported symbols per file, with barrel re-exports walked back to the file
    that declares the name
  - a second pass over test files, so test-only helpers are not called dead code
  - entry-point detection, so framework routes are not called dead code either
  - source text, for the click-through code view

Nothing here is repo-specific: projects come from workspace manifests, entry
points from detected framework markers, aliases from tsconfig.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterator, Optional

from ast_grep_py import SgRoot

from code_governance.dep_graph import _build_importable_file_map, _build_module_files_map
from code_governance.extractor import _is_test_file, _should_skip
from code_governance.languages import get_patterns
from code_governance.schemas import FileExtractionResult, GovernanceConfig, Language, RulesConfig

if TYPE_CHECKING:
    from code_governance.languages import LanguagePatterns

# Per-edge line samples and symbol references are truncated: the viewer shows a
# handful and the payload ships to the browser, so unbounded lists would cost
# megabytes to render a list nobody scrolls to the end of.
_MAX_EDGE_LINES = 20
_MAX_EDGE_SYMBOLS = 120
_MAX_BARREL_DEPTH = 6


@dataclass(frozen=True)
class EntryRule:
    """Files that are executed by a framework or tool rather than imported.

    They have no importers by design, so counting them as dead code would bury
    the real findings. ``markers`` gates the rule on the framework actually being
    present; a rule with no markers is a language default and always applies.
    """
    name: str
    markers: tuple[str, ...] = ()
    stems: frozenset[str] = frozenset()
    filenames: frozenset[str] = frozenset()
    suffixes: tuple[str, ...] = ()
    under: tuple[str, ...] = ()
    dirs: frozenset[str] = frozenset()


_TS_ROUTE_STEMS = frozenset({
    "page", "layout", "route", "template", "error", "loading", "not-found",
    "default", "global-error", "opengraph-image", "twitter-image", "icon",
    "apple-icon", "sitemap", "robots", "manifest",
})

_ENTRY_RULES: dict[str, tuple[EntryRule, ...]] = {
    "typescript": (
        EntryRule(
            name="typescript",
            stems=frozenset({"index"}),
            suffixes=(".d.ts",),
            dirs=frozenset({"scripts", "bin", "tools", "plugins", "e2e"}),
        ),
        EntryRule(name="config", suffixes=(
            ".config.ts", ".config.tsx", ".config.js", ".config.mjs", ".config.cjs",
            ".config.mts", ".config.cts", ".setup.ts", ".setup.js",
        )),
        EntryRule(
            name="next",
            markers=("next.config.js", "next.config.mjs", "next.config.ts",
                     "next.config.cjs", "next.config.mts"),
            stems=_TS_ROUTE_STEMS,
            under=("app", "pages"),
        ),
        EntryRule(
            name="next-root",
            markers=("next.config.js", "next.config.mjs", "next.config.ts",
                     "next.config.cjs", "next.config.mts"),
            stems=frozenset({"middleware", "instrumentation", "instrumentation-client"}),
        ),
        # Expo Router: every file under `app/` is a route the bundler owns.
        EntryRule(name="expo", markers=("app.json", "app.config.js", "app.config.ts"), under=("app",)),
        EntryRule(name="vite", markers=("vite.config.js", "vite.config.ts", "vite.config.mjs"),
                  stems=frozenset({"main"})),
    ),
    "python": (
        EntryRule(
            name="python",
            filenames=frozenset({
                "__init__.py", "__main__.py", "conftest.py", "setup.py",
                "manage.py", "wsgi.py", "asgi.py",
            }),
            dirs=frozenset({"scripts", "bin", "tools", "migrations"}),
        ),
    ),
}


@dataclass
class ScannedFile:
    rel_path: str
    source: str
    extraction: FileExtractionResult
    exports: list = field(default_factory=list)


def build_graph_payload(
    source_root: str | Path,
    *,
    language: Optional[Language] = None,
    include_sources: bool = True,
    entry_globs: tuple[str, ...] = (),
) -> dict:
    """Scan ``source_root`` and return the viewer payload as a plain dict."""
    root = Path(source_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    from code_governance.engine import _discover_modules, _LANG_EXTENSIONS, detect_language

    lang = language or detect_language(root)
    config = GovernanceConfig(
        root=".",
        language=lang,
        package_prefix=root.name or None,
        modules=_discover_modules(root, _LANG_EXTENSIONS[lang.value], max_depth=1),
        rules=RulesConfig(no_cycles=True, no_file_cycles=True, exclude_test_files=True),
    )
    patterns = get_patterns(lang, repo_root=root, config=config)

    scanned: list[ScannedFile] = []
    test_paths: list[str] = []
    for rel, path in _iter_source_files(root, patterns):
        if _is_test_file(rel, patterns):
            test_paths.append(rel)
            continue
        parsed = _parse(path, rel, patterns)
        if parsed is not None:
            scanned.append(parsed)

    files = [s.rel_path for s in scanned]
    index = {f: i for i, f in enumerate(files)}
    extractions = [s.extraction for s in scanned]
    importable_files = _build_importable_file_map(extractions, config, patterns)
    module_files = _build_module_files_map(config)

    def resolve(spec: str, importing_file: str, name: Optional[str] = None) -> Optional[int]:
        target = patterns.resolve_import(spec, importing_file, config, importable_files, module_files, name)
        if target is None or target == importing_file:
            return None
        # The resolvers can fall back to module names or directory prefixes;
        # anything that is not a scanned file is dropped rather than fabricated.
        return index.get(target)

    edges, stats = _build_edges(scanned, index, resolve)
    test_refs = _scan_test_refs(root, test_paths, patterns, resolve)
    symbol_users = _attribute_symbols(scanned, edges, resolve)
    entry_rules, presets = _active_entry_rules(root, patterns.language)
    published = _published_entry_files(root, lang, index)
    if published:
        presets = presets + ["package-exports"]
    projects = _project_map(root, files, lang)

    payload = {
        "root": root.name,
        "language": lang.value,
        "files": files,
        "symbols": [len(s.extraction.symbols) for s in scanned],
        "lines": [_line_count(s.source) for s in scanned],
        "edges": [
            [s, t, len(v["lines"]), sorted(v["lines"])[:_MAX_EDGE_LINES], v["type_only"],
             v["symbols"][:_MAX_EDGE_SYMBOLS]]
            for (s, t), v in sorted(edges.items())
        ],
        "exports": [[[e.name, e.line, e.source] for e in s.exports] for s in scanned],
        "symbol_users": symbol_users,
        "links": _import_links(edges, len(files)),
        "test_refs": sorted(test_refs),
        "entries": sorted(
            published | {i for i, f in enumerate(files) if _is_entry(f, entry_rules, entry_globs)}
        ),
        "projects": projects,
        "stats": {
            **stats,
            "files": len(files),
            "test_files": len(test_paths),
            "entry_presets": presets + (["custom"] if entry_globs else []),
        },
    }
    if include_sources:
        payload["sources"] = [s.source for s in scanned]
    return payload


def _iter_source_files(root: Path, patterns: "LanguagePatterns") -> Iterator[tuple[str, Path]]:
    """(repo-relative posix path, absolute path) in a stable order — rglob order is
    filesystem-dependent and would make edge attribution non-reproducible."""
    seen: set[str] = set()
    for ext in patterns.extensions:
        for path in sorted(root.rglob(f"*{ext}")):
            if _should_skip(path):
                continue
            rel = path.relative_to(root).as_posix()
            # Hidden dirs and dotfiles are tooling, not source. Checked on the
            # repo-relative path so a repo living under a hidden parent directory
            # is still scanned.
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            yield rel, path


def _parse(path: Path, rel: str, patterns: "LanguagePatterns") -> Optional[ScannedFile]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        root_node = SgRoot(source, patterns.language).root()
        return ScannedFile(
            rel_path=rel,
            source=source,
            extraction=patterns.extract(root_node, rel),
            exports=patterns.extract_exports(root_node, rel),
        )
    except Exception as e:
        print(f"Warning: failed to parse {path}: {e}", file=sys.stderr)
        return None


def _build_edges(scanned: list[ScannedFile], index: dict[str, int], resolve) -> tuple[dict, dict]:
    """(src, dst) -> {lines, type_only, symbols}.

    One `import { a, b } from "./x"` is one edge site, not two: the extractors
    emit an ImportInfo per name, so weight is counted once per (specifier, line)
    while every named symbol is still recorded for the symbol view.
    """
    edges: dict[tuple[int, int], dict] = {}
    total = resolved = 0
    for s in scanned:
        src = index[s.rel_path]
        counted: set[tuple[str, int]] = set()
        seen: set[tuple[str, int, Optional[str]]] = set()
        for imp in s.extraction.imports:
            key = (imp.source_module, imp.line, imp.imported_name)
            if key in seen:
                continue
            seen.add(key)
            first_on_line = (imp.source_module, imp.line) not in counted
            if first_on_line:
                counted.add((imp.source_module, imp.line))
                total += 1
            target = resolve(imp.source_module, s.rel_path, imp.imported_name)
            if target is None:
                continue
            if first_on_line:
                resolved += 1
            rec = edges.setdefault((src, target), {"lines": [], "type_only": 0, "symbols": []})
            if first_on_line:
                rec["lines"].append(imp.line)
                if imp.type_only:
                    rec["type_only"] += 1
            if imp.imported_name:
                rec["symbols"].append([imp.line, imp.imported_name])
    return edges, {"import_sites": total, "resolved_sites": resolved, "edges": len(edges)}


def _scan_test_refs(root: Path, test_paths: list[str], patterns, resolve) -> set[int]:
    """Test files are not graph nodes — they would swamp it — but what they import
    is what separates genuinely dead code from a test-only helper."""
    refs: set[int] = set()
    for rel in test_paths:
        try:
            source = (root / rel).read_text(encoding="utf-8", errors="replace")
            result = patterns.extract(SgRoot(source, patterns.language).root(), rel)
        except Exception:
            continue
        for imp in result.imports:
            target = resolve(imp.source_module, rel, imp.imported_name)
            if target is not None:
                refs.add(target)
    return refs


def _attribute_symbols(scanned: list[ScannedFile], edges: dict, resolve) -> list[dict]:
    """file -> {symbol: [[importer, line], ...]}.

    A barrel that re-exports a name is not the symbol's home. `export { x } from
    "./y"` and `export * from "./y"` chains are walked so usage lands on the file
    that declares the symbol — otherwise every barrelled module reads as unused.
    """
    users: dict[tuple[int, str], list[list[int]]] = {}
    for (src, dst), v in edges.items():
        for line, name in v["symbols"]:
            users.setdefault((dst, name), []).append([src, line])

    exports = [s.exports for s in scanned]
    star_sources: list[list[int]] = [[] for _ in scanned]
    origin: dict[tuple[int, str], int] = {}
    for fi, s in enumerate(scanned):
        for exp in s.exports:
            if not exp.source:
                continue
            target = resolve(exp.source, s.rel_path, None if exp.name == "*" else exp.name)
            if target is None:
                continue
            if exp.name == "*":
                star_sources[fi].append(target)
            else:
                origin[(fi, exp.name)] = target

    def declares(fi: int, name: str) -> bool:
        return any(e.name == name and not e.source for e in exports[fi])

    def home(fi: int, name: str, depth: int = 0) -> int:
        if depth > _MAX_BARREL_DEPTH:
            return fi
        direct = origin.get((fi, name))
        if direct is not None:
            return home(direct, name, depth + 1)
        for ti in star_sources[fi]:
            found = home(ti, name, depth + 1)
            if declares(found, name):
                return found
        return fi

    for (fi, name), sites in list(users.items()):
        target = home(fi, name)
        if target != fi:
            users.setdefault((target, name), []).extend(sites)

    out: list[dict] = [{} for _ in scanned]
    for (fi, name), sites in users.items():
        out[fi][name] = sorted(sites)
    return out


def _import_links(edges: dict, file_count: int) -> list[list[list[int]]]:
    """Per-file [line, target] pairs, so the code view can make import lines clickable."""
    links: list[list[list[int]]] = [[] for _ in range(file_count)]
    for (s, t), v in edges.items():
        for line in v["lines"]:
            links[s].append([line, t])
    for entry in links:
        entry.sort()
    return links


def _active_entry_rules(root: Path, language: str) -> tuple[list[EntryRule], list[str]]:
    """Entry rules whose framework markers are actually present, plus the names of
    the presets that fired (reported so the exclusion list is auditable)."""
    key = "python" if language == "python" else "typescript"
    active: list[EntryRule] = []
    for rule in _ENTRY_RULES[key]:
        if rule.markers and not any(_marker_exists(root, m) for m in rule.markers):
            continue
        active.append(rule)
    return active, [r.name for r in active]


def _marker_exists(root: Path, marker: str) -> bool:
    """Marker files live at the repo root or one level into a workspace app, so a
    bounded glob beats an rglob over a large tree."""
    if (root / marker).exists():
        return True
    for depth in ("*", "*/*"):
        for hit in root.glob(f"{depth}/{marker}"):
            if not _should_skip(hit):
                return True
    return False


def _is_entry(rel: str, rules: list[EntryRule], custom_globs: tuple[str, ...] = ()) -> bool:
    p = PurePosixPath(rel)
    parts = p.parts
    dirs = set(parts[:-1])
    name = p.name
    stem = name.split(".")[0]
    for rule in rules:
        if rule.dirs and dirs & rule.dirs:
            return True
        if rule.filenames and name in rule.filenames:
            return True
        if rule.suffixes and name.endswith(rule.suffixes):
            return True
        if rule.stems:
            if stem in rule.stems and (not rule.under or dirs & set(rule.under)):
                return True
        elif rule.under and dirs & set(rule.under):
            return True
    return any(_glob_match(g, rel) for g in custom_globs)


def _glob_match(pattern: str, rel: str) -> bool:
    """Whole-path glob. `PurePath.match` anchors at the right and treats `**` as a
    single segment, so `ai/**` would silently match nothing — these patterns are
    user-supplied and failing quietly is worse than the extra regex."""
    return _compile_glob(pattern).match(rel) is not None


@lru_cache(maxsize=256)
def _compile_glob(pattern: str) -> "re.Pattern[str]":
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i + 1: i + 2] == "*":
                out.append(".*")
                i += 2
                if pattern[i: i + 1] == "/":
                    i += 1  # `**/` also matches zero directories
                    out.append("(?:^|)")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _published_entry_files(root: Path, language: Language, index: dict[str, int]) -> set[int]:
    """Files a workspace package names in its `exports` / `main` map.

    A package's declared entry points are its public API. Nothing inside the repo
    has to import them — a platform-conditional entry like `./browser` may only
    ever be reached by a bundler — so they are entry points, not dead code.
    """
    if language != Language.TYPESCRIPT:
        return set()
    from code_governance.languages.typescript import discover_workspace_packages

    out: set[int] = set()
    for _name, pkg_dir, exports in discover_workspace_packages(root):
        for target in exports.values():
            if not isinstance(target, str) or "*" in target:
                continue
            rel = f"{pkg_dir.rstrip('/')}/{target.lstrip('./')}".lstrip("/")
            hit = index.get(rel)
            if hit is not None:
                out.add(hit)
    return out


def _project_map(root: Path, files: list[str], language: Language) -> list[str]:
    """The unit coupling is reported per: a workspace package when the repo has
    them, otherwise the top-level directory. Replaces per-repo path conventions."""
    dirs: list[str] = []
    if language == Language.TYPESCRIPT:
        from code_governance.languages.typescript import discover_workspace_packages
        dirs = [rel for _name, rel, _exports in discover_workspace_packages(root) if rel not in (".", "")]
    dirs.sort(key=len, reverse=True)

    out: list[str] = []
    for f in files:
        owner = next((d for d in dirs if f == d or f.startswith(d + "/")), None)
        if owner is None:
            parts = f.split("/")
            owner = parts[0] if len(parts) > 1 else "."
        out.append(owner)
    return out


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
