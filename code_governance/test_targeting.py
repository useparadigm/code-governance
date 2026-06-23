from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from code_governance.extractor import extract_directory
from code_governance.languages.typescript import TypeScriptPatterns
from code_governance.schemas import FileExtractionResult, GovernanceConfig, Language


@dataclass
class FileGraph:
    """File-level dependency graph with forward and reverse edges."""

    forward: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    reverse: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    all_files: set[str] = field(default_factory=set)


@dataclass
class JestScope:
    config_path: str
    root_dir: str
    test_files: list[str] = field(default_factory=list)


@dataclass
class AffectedTestsResult:
    changed_files: list[str]
    affected_files: list[str]
    test_files: list[str]
    scopes: list[JestScope]
    commands: list[str]


_TEST_SUFFIXES = [
    ".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx",
    ".spec.js", ".spec.jsx", ".test.js", ".test.jsx",
]

_JEST_CONFIG_NAMES = [
    "jest.config.ts", "jest.config.js", "jest.config.mjs",
]


def _is_test_file(path: str) -> bool:
    return any(path.endswith(s) for s in _TEST_SUFFIXES)


def _strip_extension(path: str) -> str:
    for suffix in _TEST_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)]
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"):
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")


def _lookup_file(candidates: list[str], importable_to_file: dict[str, str]) -> Optional[str]:
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in importable_to_file:
            return importable_to_file[candidate]
        # Strip extension if candidate has one (alias targets may include .ts)
        for ext in _TS_EXTENSIONS:
            if candidate.endswith(ext):
                stripped = candidate[: -len(ext)]
                if stripped in importable_to_file:
                    return importable_to_file[stripped]
                break
        index_key = f"{candidate}/index"
        if index_key in importable_to_file:
            return importable_to_file[index_key]
    return None


def build_file_graph(
    extractions: list[FileExtractionResult],
    patterns: TypeScriptPatterns,
) -> FileGraph:
    importable_to_file: dict[str, str] = {}
    for ext in extractions:
        importable = patterns.file_to_importable(ext.file_path)
        if importable:
            importable_to_file[importable] = ext.file_path

    graph = FileGraph()
    graph.all_files = {ext.file_path for ext in extractions}

    dummy_config = GovernanceConfig(language=Language.TYPESCRIPT)

    for ext in extractions:
        for imp in ext.imports:
            candidates = patterns._expand_candidates(
                imp.source_module, ext.file_path, dummy_config
            )
            resolved = _lookup_file(candidates, importable_to_file)
            if resolved and resolved != ext.file_path:
                graph.forward[ext.file_path].add(resolved)
                graph.reverse[resolved].add(ext.file_path)

    return graph


def find_affected_files(graph: FileGraph, changed_files: set[str]) -> set[str]:
    affected: set[str] = set()
    queue: deque[str] = deque(changed_files & graph.all_files)

    while queue:
        current = queue.popleft()
        if current in affected:
            continue
        affected.add(current)
        for dependent in graph.reverse.get(current, set()):
            if dependent not in affected:
                queue.append(dependent)

    return affected


def find_test_files(affected: set[str], all_files: set[str]) -> set[str]:
    tests: set[str] = set()

    for f in affected:
        if _is_test_file(f):
            tests.add(f)
            continue

        base = _strip_extension(f)
        for suffix in _TEST_SUFFIXES:
            candidate = base + suffix
            if candidate in all_files:
                tests.add(candidate)

        parts = f.rsplit("/", 1)
        if len(parts) == 2:
            dir_part, file_part = parts
            file_base = _strip_extension(file_part)
            for suffix in _TEST_SUFFIXES:
                candidate = f"{dir_part}/__tests__/{file_base}{suffix}"
                if candidate in all_files:
                    tests.add(candidate)

    return tests


def _should_skip_dir(path: Path) -> bool:
    skip = {
        "__pycache__", ".git", "node_modules", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    }
    return any(part in skip for part in path.parts)


def discover_jest_scopes(repo_root: Path) -> list[JestScope]:
    scopes: list[JestScope] = []
    for name in _JEST_CONFIG_NAMES:
        for config_path in repo_root.rglob(name):
            if _should_skip_dir(config_path):
                continue
            rel = str(config_path.relative_to(repo_root))
            root_dir = str(config_path.parent.relative_to(repo_root))
            if root_dir == ".":
                root_dir = ""
            scopes.append(JestScope(config_path=rel, root_dir=root_dir))
    # Deepest first so most-specific config wins
    scopes.sort(key=lambda s: -s.root_dir.count("/") if s.root_dir else 1)
    return scopes


def group_tests_by_jest_scope(
    test_files: set[str],
    jest_scopes: list[JestScope],
) -> list[JestScope]:
    result: dict[str, JestScope] = {
        s.config_path: JestScope(s.config_path, s.root_dir) for s in jest_scopes
    }

    for test_file in sorted(test_files):
        for scope in jest_scopes:
            prefix = scope.root_dir + "/" if scope.root_dir else ""
            if test_file.startswith(prefix) or not scope.root_dir:
                result[scope.config_path].test_files.append(test_file)
                break

    return [s for s in result.values() if s.test_files]


def format_jest_commands(scopes: list[JestScope]) -> list[str]:
    commands: list[str] = []
    for scope in scopes:
        files = " ".join(scope.test_files)
        commands.append(
            f"npx jest --config {scope.config_path} --findRelatedTests {files}"
        )
    return commands


def discover_nx_projects(repo_root: Path) -> dict[str, str]:
    """Map directory paths (relative to repo root) to NX project names.

    Strategy:
    1. Read project.json files for the authoritative ``name`` field.
    2. Fall back to workspace.json at the repo root (legacy NX).
    3. Final fallback: use the directory basename as the project name.
    """
    mapping: dict[str, str] = {}

    # Strategy 1: modern NX — per-project project.json
    for pj in repo_root.rglob("project.json"):
        if _should_skip_dir(pj):
            continue
        try:
            data = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
            name = data.get("name")
            rel_dir = str(pj.parent.relative_to(repo_root))
            if rel_dir == ".":
                rel_dir = ""
            if name:
                mapping[rel_dir] = name
        except Exception:
            continue

    if mapping:
        return mapping

    # Strategy 2: legacy NX — workspace.json at root
    ws_path = repo_root / "workspace.json"
    if ws_path.exists():
        try:
            data = json.loads(ws_path.read_text(encoding="utf-8", errors="replace"))
            projects = data.get("projects", {})
            if isinstance(projects, dict):
                for name, path_or_cfg in projects.items():
                    path = path_or_cfg if isinstance(path_or_cfg, str) else path_or_cfg.get("root", "")
                    mapping[path.rstrip("/")] = name
        except Exception:
            pass

    if mapping:
        return mapping

    # Strategy 3: fallback — use directory basename
    for name in _JEST_CONFIG_NAMES:
        for cfg in repo_root.rglob(name):
            if _should_skip_dir(cfg):
                continue
            rel_dir = str(cfg.parent.relative_to(repo_root))
            if rel_dir == ".":
                continue
            mapping[rel_dir] = Path(rel_dir).name

    return mapping


def format_nx_commands(scopes: list[JestScope], nx_projects: dict[str, str]) -> list[str]:
    commands: list[str] = []
    for scope in scopes:
        project_name = nx_projects.get(scope.root_dir)
        files = " ".join(scope.test_files)
        if not project_name:
            commands.append(
                f"npx jest --config {scope.config_path} --findRelatedTests {files}"
            )
            continue
        commands.append(
            f"npx nx run {project_name}:test -- --findRelatedTests {files}"
        )
    return commands


def execute_commands(commands: list[str], repo_root: Path) -> int:
    worst = 0
    for cmd in commands:
        result = subprocess.run(cmd, shell=True, cwd=str(repo_root))
        worst = max(worst, result.returncode)
    return worst


def _get_changed_files(repo_root: Path, git_ref: str) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", git_ref],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        print(f"Warning: git diff failed: {result.stderr.strip()}", file=sys.stderr)
        return set()

    ts_extensions = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"}
    changed: set[str] = set()
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line and any(line.endswith(ext) for ext in ts_extensions):
            changed.add(line)
    return changed


def run_affected_tests(
    repo_root: Path,
    git_ref: str = "HEAD",
    *,
    nx: bool = False,
) -> AffectedTestsResult:
    repo_root = repo_root.resolve()

    patterns = TypeScriptPatterns()
    dummy_config = GovernanceConfig(language=Language.TYPESCRIPT)
    patterns.initialize(repo_root, dummy_config)

    extractions = extract_directory(
        repo_root, Language.TYPESCRIPT, exclude_test_files=False, patterns=patterns
    )

    graph = build_file_graph(extractions, patterns)

    changed_files = _get_changed_files(repo_root, git_ref)

    affected = find_affected_files(graph, changed_files)

    test_files = find_test_files(affected, graph.all_files)

    jest_scopes = discover_jest_scopes(repo_root)
    grouped = group_tests_by_jest_scope(test_files, jest_scopes)

    if nx:
        nx_projects = discover_nx_projects(repo_root)
        commands = format_nx_commands(grouped, nx_projects)
    else:
        commands = format_jest_commands(grouped)

    return AffectedTestsResult(
        changed_files=sorted(changed_files),
        affected_files=sorted(affected),
        test_files=sorted(test_files),
        scopes=grouped,
        commands=commands,
    )
