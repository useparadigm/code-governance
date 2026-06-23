from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Language(str, Enum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RuleKind(str, Enum):
    NO_CYCLES = "no_cycles"
    ENFORCE_LAYERS = "enforce_layers"
    ENFORCE_CANNOT_DEPEND_ON = "enforce_cannot_depend_on"
    CAN_ONLY_DEPEND_ON = "can_only_depend_on"
    INDEPENDENCE = "independence"
    MUST_NOT_REACH = "must_not_reach"
    NO_ORPHANS = "no_orphans"
    MAX_PUBLIC_SURFACE = "max_public_surface"
    MIN_COHESION = "min_cohesion"


class ModuleConfig(BaseModel):
    name: str
    path: str
    # Blacklist: this module must not import any of these (exact names or globs,
    # e.g. "billing", "tests.*"). Complementary to can_only_depend_on.
    cannot_depend_on: list[str] = []
    # Allowlist: if set, this module may ONLY import modules in this list (plus
    # itself); any other internal dependency is a violation. None = not enforced.
    can_only_depend_on: Optional[list[str]] = None
    layer: Optional[str] = None


class LayersConfig(BaseModel):
    order: list[str] = []


class ReachConstraint(BaseModel):
    """`source` modules must not transitively reach any `target` module."""
    source: list[str] = []
    target: list[str] = []


class RulesConfig(BaseModel):
    no_cycles: bool = True
    enforce_layers: bool = False
    enforce_cannot_depend_on: bool = True
    # Enabled, but inert unless a module sets `can_only_depend_on` (default None).
    enforce_can_only_depend_on: bool = True
    no_orphans: bool = False
    independence: list[list[str]] = []
    must_not_reach: list[ReachConstraint] = []
    max_public_surface: Optional[float] = None
    min_cohesion: Optional[float] = None
    transitive: bool = False
    exclude_from_cycles: list[str] = []
    exclude_from_orphans: list[str] = []
    exclude_test_files: bool = True
    # Per-rule severity overrides: {rule_name: "error"|"warning"|"info"|"off"}.
    # "off" suppresses the rule's violations entirely. Unlisted rules keep their
    # built-in default severity. Only error-severity violations fail the run.
    severity: dict[str, str] = {}


class GovernanceConfig(BaseModel):
    root: str = "."
    language: Language = Language.PYTHON
    package_prefix: Optional[str] = None
    modules: list[ModuleConfig] = []
    layers: LayersConfig = LayersConfig()
    rules: RulesConfig = RulesConfig()


@dataclass
class ImportInfo:
    source_module: str
    imported_name: Optional[str] = None
    line: int = 0
    raw_statement: str = ""


@dataclass
class EdgeDetail:
    source_file: str
    source_module: str
    target_module: str
    imported_name: Optional[str]
    line: int
    raw_statement: str


@dataclass
class ClassInfo:
    name: str
    base_classes: list[str] = field(default_factory=list)


@dataclass
class FileExtractionResult:
    file_path: str
    imports: list[ImportInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)


class Violation(BaseModel):
    rule: RuleKind
    module: str
    detail: str
    severity: Severity = Severity.ERROR
    evidence: list[dict] = []


class ModuleMetrics(BaseModel):
    name: str
    total_symbols: int = 0
    externally_used_symbols: int = 0
    internal_edges: int = 0
    external_edges: int = 0
    public_surface_ratio: Optional[float] = None
    cohesion_ratio: Optional[float] = None


class DependencyTarget(BaseModel):
    target: str
    count: int
    files: list[dict] = []


class DiscoverReport(BaseModel):
    config_path: str
    language: Language
    module_count: int
    total_files_scanned: int
    dependencies: dict[str, list[DependencyTarget]] = {}
    metrics: list["ModuleMetrics"] = []


class GovernanceReport(BaseModel):
    config_path: str
    language: Language
    module_count: int
    total_files_scanned: int
    violations: list[Violation] = []
    metrics: list[ModuleMetrics] = []

    @property
    def passed(self) -> bool:
        # Only error-severity violations fail the run; warnings/info are advisory.
        return not any(v.severity == Severity.ERROR for v in self.violations)
