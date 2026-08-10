from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ast_grep_py import SgRoot

from code_governance.languages import get_patterns
from code_governance.schemas import FileExtractionResult, Language

if TYPE_CHECKING:
    from code_governance.languages import LanguagePatterns


def extract_file(file_path: str, source: str, language: Language) -> FileExtractionResult:
    patterns = get_patterns(language)
    root = SgRoot(source, patterns.language)
    return patterns.extract(root.root(), file_path)


def extract_directory(
    root_dir: str | Path,
    language: Language,
    exclude_test_files: bool = True,
    patterns: Optional["LanguagePatterns"] = None,
) -> list[FileExtractionResult]:
    if patterns is None:
        patterns = get_patterns(language)
    root_path = Path(root_dir)
    results: list[FileExtractionResult] = []

    for ext in patterns.extensions:
        # Sort so file (and therefore importable_map) insertion order is stable
        # across machines/filesystems — rglob order is filesystem-dependent and
        # would otherwise make edge attribution non-reproducible.
        for file_path in sorted(root_path.rglob(f"*{ext}")):
            if _should_skip(file_path):
                continue
            rel_path = str(file_path.relative_to(root_path))
            if exclude_test_files and _is_test_file(rel_path, patterns):
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
                sg_root = SgRoot(source, patterns.language)
                result = patterns.extract(sg_root.root(), rel_path)
                results.append(result)
            except Exception as e:
                print(f"Warning: failed to parse {file_path}: {e}", file=sys.stderr)
                continue

    return results


_TEST_DIR_SEGMENTS = {"tests", "test", "__tests__"}


def _is_test_file(rel_path: str, patterns: "LanguagePatterns") -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in _TEST_DIR_SEGMENTS for p in parts[:-1]):
        return True
    filename = parts[-1]
    for prefix, suffix in patterns.test_file_patterns:
        if prefix and filename.startswith(prefix):
            return True
        if suffix and filename.endswith(suffix):
            return True
    return False


def _should_skip(path: Path) -> bool:
    skip_dirs = {
        "__pycache__", ".git", "node_modules", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".eggs", "bin", "obj",
        # JS/TS build and cache output. These hold generated .ts/.js that mirrors
        # real source, so scanning them double-counts every file they shadow.
        ".next", ".nuxt", ".svelte-kit", ".turbo", ".expo", ".vercel", ".output",
        ".parcel-cache", "coverage", "storybook-static",
    }
    return any(part in skip_dirs for part in path.parts)
