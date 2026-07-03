"""File-level cycle detection (no_file_cycles) — the madge --circular equivalent.

Module-level cycle detection can never see a cycle between two files inside the
same module; these tests pin down that the file-level rule does.
"""
from pathlib import Path

from code_governance.config import load_config
from code_governance.dep_graph import build_dependency_graph
from code_governance.engine import config_to_toml, run_auto_scan
from code_governance.extractor import extract_directory
from code_governance.languages import get_patterns
from code_governance.rules import check_no_cycles, check_no_file_cycles
from code_governance.schemas import (
    GovernanceConfig,
    Language,
    ModuleConfig,
    RuleKind,
    RulesConfig,
)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _scan(root: Path, config: GovernanceConfig):
    patterns = get_patterns(config.language, repo_root=root, config=config)
    extractions = extract_directory(root, config.language, config.rules.exclude_test_files, patterns=patterns)
    return build_dependency_graph(extractions, config, patterns=patterns)


def _py_config(**rules) -> GovernanceConfig:
    return GovernanceConfig(
        root=".",
        language=Language.PYTHON,
        package_prefix="pkg",
        modules=[ModuleConfig(name="pkg", path="pkg/")],
        rules=RulesConfig(no_file_cycles=True, **rules),
    )


def test_detects_file_cycle_within_single_module(tmp_path):
    _write(tmp_path, {
        "pkg/a.py": "from pkg.b import B\nclass A: pass\n",
        "pkg/b.py": "from pkg.a import A\nclass B: pass\n",
        "pkg/c.py": "from pkg.a import A\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)

    # Module-level rule is structurally blind to this cycle.
    assert check_no_cycles(graph, config) == []

    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    v = violations[0]
    assert v.rule == RuleKind.NO_FILE_CYCLES
    assert "pkg/a.py" in v.detail
    assert "pkg/b.py" in v.detail
    assert "pkg/c.py" not in v.detail
    assert v.evidence
    assert all(e["source_file"] in ("pkg/a.py", "pkg/b.py") for e in v.evidence)


def test_detects_package_init_cycle(tmp_path):
    # `import pkg` executes pkg/__init__.py, which imports pkg/a.py back.
    _write(tmp_path, {
        "pkg/__init__.py": "from pkg.a import A\n",
        "pkg/a.py": "import pkg\nclass A: pass\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    assert "pkg/__init__.py" in violations[0].detail
    assert "pkg/a.py" in violations[0].detail


def test_relative_imports_resolve_to_files(tmp_path):
    _write(tmp_path, {
        "pkg/a.py": "from .b import B\nclass A: pass\n",
        "pkg/b.py": "from . import a\nclass B: pass\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    assert "pkg/a.py" in violations[0].detail


def test_no_false_positive_on_init_reexports(tmp_path):
    # The standard re-export pattern: __init__ imports submodules, submodules
    # import each other acyclically. No cycle must be reported.
    _write(tmp_path, {
        "pkg/__init__.py": "from pkg.a import A\nfrom pkg.b import B\n",
        "pkg/a.py": "from .b import B\nclass A: pass\n",
        "pkg/b.py": "import os\nclass B: pass\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_self_import_is_not_a_cycle(tmp_path):
    _write(tmp_path, {
        "pkg/a.py": "from pkg.a import x\nx = 1\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_disabled_by_default_and_skips_graph_pass(tmp_path):
    _write(tmp_path, {
        "pkg/a.py": "from pkg.b import B\n",
        "pkg/b.py": "from pkg.a import A\n",
    })
    config = _py_config()
    config.rules.no_file_cycles = False
    graph = _scan(tmp_path, config)
    assert graph.file_edges == {}
    assert check_no_file_cycles(graph, config) == []


def test_typescript_file_cycle(tmp_path):
    _write(tmp_path, {
        "src/feature/x.ts": "import { y } from './y'\nexport const x = 1\n",
        "src/feature/y.ts": "import { x } from './x'\nexport const y = 2\n",
        "src/index.ts": "export { x } from './feature/x'\n",
    })
    config = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[ModuleConfig(name="src", path="src/")],
        rules=RulesConfig(no_file_cycles=True),
    )
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    assert "src/feature/x.ts" in violations[0].detail
    assert "src/feature/y.ts" in violations[0].detail
    assert "src/index.ts" not in violations[0].detail


def test_typescript_directory_import_resolves_to_index(tmp_path):
    # `import './widgets'` executes widgets/index.ts; a cycle through the index
    # barrel is a real runtime import loop and must be caught.
    _write(tmp_path, {
        "src/widgets/index.ts": "export { w } from './w'\n",
        "src/widgets/w.ts": "import '../app'\nexport const w = 1\n",
        "src/app.ts": "import { w } from './widgets'\nexport const app = w\n",
    })
    config = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[ModuleConfig(name="src", path="src/")],
        rules=RulesConfig(no_file_cycles=True),
    )
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    detail = violations[0].detail
    assert "src/widgets/index.ts" in detail
    assert "src/widgets/w.ts" in detail
    assert "src/app.ts" in detail


def test_type_checking_imports_do_not_count(tmp_path):
    # Deliberate circular *type* imports are a standard, runtime-safe pattern.
    _write(tmp_path, {
        "pkg/a.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.b import B\n"
            "class A: pass\n"
        ),
        "pkg/b.py": "from pkg.a import A\nclass B: pass\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_import_on_line_after_type_checking_block_counts(tmp_path):
    # Range math regression guard: an import on the very next line after the
    # TYPE_CHECKING block is a real runtime import.
    _write(tmp_path, {
        "pkg/a.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.c import C\n"
            "from pkg.b import B\n"
        ),
        "pkg/b.py": "from pkg.a import A\n",
        "pkg/c.py": "x = 1\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    assert "pkg/b.py" in violations[0].detail
    assert "pkg/c.py" not in violations[0].detail


def test_type_checking_else_branch_still_counts(tmp_path):
    # `else:` of a TYPE_CHECKING block executes at runtime — the classic
    # fallback-import pattern must keep its edge.
    _write(tmp_path, {
        "pkg/a.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.c import C\n"
            "else:\n"
            "    from pkg.b import B\n"
        ),
        "pkg/b.py": "from pkg.a import A\n",
        "pkg/c.py": "x = 1\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    assert "pkg/b.py" in violations[0].detail
    assert "pkg/c.py" not in violations[0].detail


def test_typescript_type_only_imports_do_not_count(tmp_path):
    _write(tmp_path, {
        "src/x.ts": "import type { Y } from './y'\nexport const x = 1\n",
        "src/y.ts": "import { x } from './x'\nexport type Y = number\n",
        "src/p.ts": "export type { Q } from './q'\nexport const p = 1\n",
        "src/q.ts": "import { p } from './p'\nexport type Q = string\n",
    })
    config = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[ModuleConfig(name="src", path="src/")],
        rules=RulesConfig(no_file_cycles=True),
    )
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_typescript_export_type_star_does_not_count(tmp_path):
    # `export type * from ...` parses its `type` token as an ERROR node in the
    # grammar; it must still be recognized as type-only (erased at runtime).
    _write(tmp_path, {
        "src/x.ts": "export type * from './y'\nexport const x = 1\n",
        "src/y.ts": "import { x } from './x'\nexport type Y = number\n",
    })
    config = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[ModuleConfig(name="src", path="src/")],
        rules=RulesConfig(no_file_cycles=True),
    )
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_violation_files_lists_full_scc(tmp_path):
    # SCC {a, b, c} where the representative cycle is a -> b -> a: member c is
    # off the representative path but must still appear in violation.files so
    # --diff keeps the violation when only c changed.
    _write(tmp_path, {
        "pkg/a.py": "from pkg.b import B\n",
        "pkg/b.py": "from pkg.a import A\nfrom pkg.c import C\n",
        "pkg/c.py": "from pkg.a import A\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1
    v = violations[0]
    assert set(v.files) == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
    evidence_files = {e["source_file"] for e in v.evidence}
    assert "pkg/c.py" not in evidence_files  # off the representative cycle


def test_namespace_package_import_creates_no_edge(tmp_path):
    # `import pkg` on a namespace package (no __init__.py) executes nothing at
    # runtime — it must not resolve to an arbitrary child file and fabricate a
    # cycle. Here pkg/a.py imports pkg.b (real edge) and pkg/b.py imports the
    # bare namespace package back: no runtime loop exists.
    _write(tmp_path, {
        "pkg/a.py": "from pkg.b import B\nclass A: pass\n",
        "pkg/b.py": "import pkg\nclass B: pass\n",
    })
    config = _py_config()
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_typescript_per_specifier_type_imports_do_not_count(tmp_path):
    # `import { type Foo }` / `export { type Foo }` specifiers are erased at
    # compile time; a type-only back-reference paired with a runtime import
    # from the other side is not a runtime cycle.
    _write(tmp_path, {
        "src/x.ts": "import { type Y } from './y'\nexport const x = 1\n",
        "src/y.ts": "import { x } from './x'\nexport type Y = number\n",
        "src/p.ts": "export { type Q } from './q'\nexport const p = 1\n",
        "src/q.ts": "import { p } from './p'\nexport type Q = string\n",
    })
    config = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[ModuleConfig(name="src", path="src/")],
        rules=RulesConfig(no_file_cycles=True),
    )
    graph = _scan(tmp_path, config)
    assert check_no_file_cycles(graph, config) == []


def test_typescript_mixed_specifiers_keep_runtime_edge(tmp_path):
    # `import { type Y, y }` still carries a runtime specifier — edge stays.
    _write(tmp_path, {
        "src/x.ts": "import { type Y, y } from './y'\nexport const x = y\n",
        "src/y.ts": "import { x } from './x'\nexport const y = 2\nexport type Y = number\n",
    })
    config = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[ModuleConfig(name="src", path="src/")],
        rules=RulesConfig(no_file_cycles=True),
    )
    graph = _scan(tmp_path, config)
    violations = check_no_file_cycles(graph, config)
    assert len(violations) == 1


def test_auto_scan_reports_file_cycles(tmp_path):
    _write(tmp_path, {
        "app/a.py": "from app.b import B\nclass A: pass\n",
        "app/b.py": "from app.a import A\nclass B: pass\n",
    })
    report = run_auto_scan(tmp_path)
    kinds = {v.rule for v in report.violations}
    assert RuleKind.NO_FILE_CYCLES in kinds
    # Both files live in one module, so the module-level rule stays silent.
    assert RuleKind.NO_CYCLES not in kinds
    assert not report.passed


def test_root_package_init_cycle_detected(tmp_path):
    # --auto pointed at a package directory itself: `import myapp` from a
    # child module executes the root __init__.py, which imports back — cycle.
    root = tmp_path / "myapp"
    _write(root, {
        "__init__.py": "from myapp.a import A\n",
        "a.py": "import myapp\nclass A: pass\n",
    })
    report = run_auto_scan(root)
    file_cycles = [v for v in report.violations if v.rule == RuleKind.NO_FILE_CYCLES]
    assert len(file_cycles) == 1
    assert "__init__.py" in file_cycles[0].detail
    assert "a.py" in file_cycles[0].detail


def test_config_toml_roundtrip_includes_no_file_cycles(tmp_path):
    config = _py_config()
    toml_str = config_to_toml(config)
    assert "no_file_cycles = true" in toml_str
    config_file = tmp_path / "governance.toml"
    config_file.write_text(toml_str)
    loaded = load_config(config_file)
    assert loaded.rules.no_file_cycles is True


def test_deterministic_output(tmp_path):
    # Three-file cycle plus a two-file cycle: violations must come out in a
    # stable order with stable representative cycles across runs.
    files = {
        "pkg/a.py": "from pkg.b import B\n",
        "pkg/b.py": "from pkg.c import C\n",
        "pkg/c.py": "from pkg.a import A\n",
        "pkg/m.py": "from pkg.n import N\n",
        "pkg/n.py": "from pkg.m import M\n",
    }
    _write(tmp_path, files)
    config = _py_config()

    def run():
        graph = _scan(tmp_path, config)
        return [(v.detail, tuple(sorted(e["source_file"] for e in v.evidence))) for v in check_no_file_cycles(graph, config)]

    first = run()
    assert len(first) == 2
    for _ in range(3):
        assert run() == first
