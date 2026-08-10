from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from ast_grep_py import SgNode

from code_governance.languages.python import PythonPatterns
from code_governance.languages.typescript import TypeScriptPatterns
from code_governance.schemas import ExportInfo, FileExtractionResult, Language

if TYPE_CHECKING:
    from code_governance.schemas import GovernanceConfig


@runtime_checkable
class LanguagePatterns(Protocol):
    language: str
    extensions: tuple[str, ...]
    test_file_patterns: list[tuple[str, str]]

    def extract(self, root: SgNode, file_path: str) -> FileExtractionResult: ...

    # Only the graph export needs this, and it costs an extra AST walk, so it is
    # kept off the main extract() path rather than folded into it.
    def extract_exports(self, root: SgNode, file_path: str) -> list[ExportInfo]: ...

    def file_to_importable(self, file_path: str) -> Optional[str]: ...

    def resolve_import(
        self,
        import_source: str,
        importing_file: str,
        config: "GovernanceConfig",
        importable_map: dict[str, str],
        module_files: dict[str, str],
        imported_name: Optional[str] = None,
    ) -> Optional[str]: ...

    def initialize(self, repo_root: Path, config: "GovernanceConfig") -> None: ...


LANGUAGE_PATTERNS: dict[Language, type] = {
    Language.PYTHON: PythonPatterns,
    Language.TYPESCRIPT: TypeScriptPatterns,
}


def get_patterns(
    language: Language,
    *,
    repo_root: Optional[Path] = None,
    config: Optional["GovernanceConfig"] = None,
) -> LanguagePatterns:
    cls = LANGUAGE_PATTERNS.get(language)
    if cls is None:
        raise ValueError(f"Unsupported language: {language}")
    instance = cls()
    if repo_root is not None and config is not None:
        instance.initialize(repo_root, config)
    return instance
