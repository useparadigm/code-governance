from pathlib import Path

from code_governance.config import load_config
from code_governance.dep_graph import build_dependency_graph
from code_governance.engine import run_auto_scan
from code_governance.extractor import extract_directory
from code_governance.languages import get_patterns
from code_governance.schemas import GovernanceConfig, ModuleConfig

NESTED_PROJECT = Path(__file__).parent / "fixtures" / "nested_project"


def test_build_graph_finds_modules(sample_config):
    config = load_config(sample_config)
    source_root = sample_config.parent
    extractions = extract_directory(source_root, config.language)
    graph = build_dependency_graph(extractions, config)

    # api imports from core, db, utils
    api_deps = graph.get_module_dependencies("api")
    assert "core" in api_deps
    assert "db" in api_deps
    assert "utils" in api_deps

    # db imports from core
    db_deps = graph.get_module_dependencies("db")
    assert "core" in db_deps


def test_build_graph_tracks_symbols(sample_config):
    config = load_config(sample_config)
    source_root = sample_config.parent
    extractions = extract_directory(source_root, config.language)
    graph = build_dependency_graph(extractions, config)

    core_symbols = graph.symbols_per_module.get("core", set())
    assert "User" in core_symbols
    assert "Project" in core_symbols
    assert "validate_email" in core_symbols


def test_build_graph_edge_details(sample_config):
    config = load_config(sample_config)
    source_root = sample_config.parent
    extractions = extract_directory(source_root, config.language)
    graph = build_dependency_graph(extractions, config)

    # Check that edge details contain file-level info
    api_to_core = [e for e in graph.edge_details if e.source_module == "api" and e.target_module == "core"]
    assert len(api_to_core) > 0
    assert any(e.source_file == "api/routes.py" for e in api_to_core)


def test_cycle_detected_in_graph(sample_config):
    """core/service.py imports from db, and db imports from core — cycle."""
    config = load_config(sample_config)
    source_root = sample_config.parent
    extractions = extract_directory(source_root, config.language)
    graph = build_dependency_graph(extractions, config)

    core_deps = graph.get_module_dependencies("core")
    db_deps = graph.get_module_dependencies("db")
    assert "db" in core_deps
    assert "core" in db_deps


def test_nested_modules_attributed_to_deepest(tmp_path):
    """When a parent module path is a prefix of a child module path, files
    inside the child must be attributed to the child — not swallowed by the
    parent. Regression test for the recursive-discovery case where deeply
    nested modules were reporting 0 symbols / 0 edges."""
    (tmp_path / "analysis" / "ga" / "automated" / "api").mkdir(parents=True)
    (tmp_path / "analysis" / "ga" / "foo.py").write_text("def root_fn():\n    pass\n")
    (tmp_path / "analysis" / "ga" / "automated" / "bar.py").write_text("def auto_fn():\n    pass\n")
    (tmp_path / "analysis" / "ga" / "automated" / "api" / "baz.py").write_text("def api_fn():\n    pass\n")

    config = GovernanceConfig(
        root=".",
        modules=[
            ModuleConfig(name="analysis.ga", path="analysis/ga/"),
            ModuleConfig(name="analysis.ga.automated", path="analysis/ga/automated/"),
            ModuleConfig(name="analysis.ga.automated.api", path="analysis/ga/automated/api/"),
        ],
    )
    patterns = get_patterns(config.language, repo_root=tmp_path, config=config)
    extractions = extract_directory(tmp_path, config.language, True, patterns=patterns)
    graph = build_dependency_graph(extractions, config, patterns=patterns)

    assert graph.symbols_per_module["analysis.ga"] == {"root_fn"}
    assert graph.symbols_per_module["analysis.ga.automated"] == {"auto_fn"}
    assert graph.symbols_per_module["analysis.ga.automated.api"] == {"api_fn"}


def test_nested_modules_resolve_imports_to_deepest(tmp_path):
    """An import path matching multiple module prefixes should resolve to the
    deepest (most specific) module, not the shallowest ancestor."""
    (tmp_path / "analysis" / "ga" / "automated" / "api").mkdir(parents=True)
    (tmp_path / "analysis" / "ga" / "automated" / "api" / "baz.py").write_text("VALUE = 1\n")
    (tmp_path / "consumer").mkdir()
    (tmp_path / "consumer" / "use.py").write_text(
        "from analysis.ga.automated.api.baz import VALUE\n"
    )

    config = GovernanceConfig(
        root=".",
        modules=[
            ModuleConfig(name="analysis.ga", path="analysis/ga/"),
            ModuleConfig(name="analysis.ga.automated", path="analysis/ga/automated/"),
            ModuleConfig(name="analysis.ga.automated.api", path="analysis/ga/automated/api/"),
            ModuleConfig(name="consumer", path="consumer/"),
        ],
    )
    patterns = get_patterns(config.language, repo_root=tmp_path, config=config)
    extractions = extract_directory(tmp_path, config.language, True, patterns=patterns)
    graph = build_dependency_graph(extractions, config, patterns=patterns)

    consumer_deps = graph.get_module_dependencies("consumer")
    assert "analysis.ga.automated.api" in consumer_deps
    assert "analysis.ga" not in consumer_deps
    assert "analysis.ga.automated" not in consumer_deps


def test_auto_scan_nested_project_populates_every_module():
    """Smoke test for --auto on a project with nested packages where the parent
    dir also has direct .py files (so it becomes a module too). Pre-fix, the
    parent swallowed all children's files and nested modules reported
    0 symbols / 0 edges — exactly the report shape this fixture reproduces."""
    report = run_auto_scan(NESTED_PROJECT)

    metrics_by_name = {m.name: m for m in report.metrics}

    expected_modules = {
        "analysis",
        "analysis.gains_analysis",
        "analysis.gains_analysis.automated",
        "analysis.gains_analysis.automated.inactive_defaults.api",
    }
    assert expected_modules <= set(metrics_by_name)

    for name in expected_modules:
        assert metrics_by_name[name].total_symbols > 0, (
            f"Module {name} has 0 symbols — deepest-match attribution regressed"
        )

    # analysis/top.py is the ONLY direct file of the top-level analysis module;
    # children must not be swallowed into it.
    assert metrics_by_name["analysis"].total_symbols == 1

    # Cross-module imports must resolve to the deepest target.
    assert metrics_by_name["analysis.gains_analysis"].external_edges >= 1
    assert metrics_by_name["analysis.gains_analysis.automated"].external_edges >= 1
