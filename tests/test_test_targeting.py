from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from code_governance.languages.typescript import TypeScriptPatterns
from code_governance.schemas import GovernanceConfig, Language
from code_governance.test_targeting import (
    AffectedTestsResult,
    FileGraph,
    JestScope,
    build_file_graph,
    discover_jest_scopes,
    discover_nx_projects,
    find_affected_files,
    find_test_files,
    format_jest_commands,
    format_nx_commands,
    group_tests_by_jest_scope,
    run_affected_tests,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "nx_workspace"


@pytest.fixture
def nx_patterns():
    """TypeScriptPatterns initialized against the NX workspace fixture."""
    p = TypeScriptPatterns()
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    p.initialize(FIXTURE_DIR, cfg)
    return p


@pytest.fixture
def nx_extractions(nx_patterns):
    """All file extractions from the NX workspace fixture (including test files)."""
    from code_governance.extractor import extract_directory

    return extract_directory(
        FIXTURE_DIR, Language.TYPESCRIPT, exclude_test_files=False, patterns=nx_patterns
    )


@pytest.fixture
def nx_graph(nx_extractions, nx_patterns):
    """File-level dependency graph from the NX workspace fixture."""
    return build_file_graph(nx_extractions, nx_patterns)


class TestBuildFileGraph:
    def test_all_source_files_discovered(self, nx_graph):
        expected = {
            "libs/shared/src/util.ts",
            "libs/shared/src/index.ts",
            "libs/marketplace/src/product.service.ts",
            "libs/marketplace/src/index.ts",
            "apps/backend/src/handler.ts",
            "apps/backend/src/handler.spec.ts",
        }
        # Jest configs are also .ts files, so they may be in all_files
        assert expected.issubset(nx_graph.all_files)

    def test_barrel_reexport_edge(self, nx_graph):
        # shared/index.ts re-exports from ./util → forward edge
        assert "libs/shared/src/util.ts" in nx_graph.forward.get(
            "libs/shared/src/index.ts", set()
        )

    def test_cross_project_alias_edge(self, nx_graph):
        # marketplace/product.service.ts imports @org/shared → resolves to shared/src/index.ts
        fwd = nx_graph.forward.get("libs/marketplace/src/product.service.ts", set())
        assert "libs/shared/src/index.ts" in fwd

    def test_cross_project_alias_edge_backend(self, nx_graph):
        # backend/handler.ts imports @org/marketplace → resolves to marketplace/src/index.ts
        fwd = nx_graph.forward.get("apps/backend/src/handler.ts", set())
        assert "libs/marketplace/src/index.ts" in fwd

    def test_test_file_imports_source(self, nx_graph):
        # handler.spec.ts imports ./handler
        fwd = nx_graph.forward.get("apps/backend/src/handler.spec.ts", set())
        assert "apps/backend/src/handler.ts" in fwd

    def test_reverse_edges_consistent(self, nx_graph):
        for src, targets in nx_graph.forward.items():
            for tgt in targets:
                assert src in nx_graph.reverse.get(tgt, set()), (
                    f"Missing reverse edge: {tgt} should have {src} in reverse"
                )


class TestFindAffectedFiles:
    def test_change_leaf_affects_chain(self, nx_graph):
        # Changing util.ts should affect everything up the chain
        affected = find_affected_files(nx_graph, {"libs/shared/src/util.ts"})
        assert "libs/shared/src/util.ts" in affected
        assert "libs/shared/src/index.ts" in affected
        assert "libs/marketplace/src/product.service.ts" in affected
        assert "libs/marketplace/src/index.ts" in affected
        assert "apps/backend/src/handler.ts" in affected
        assert "apps/backend/src/handler.spec.ts" in affected

    def test_change_handler_only_affects_local(self, nx_graph):
        affected = find_affected_files(nx_graph, {"apps/backend/src/handler.ts"})
        assert "apps/backend/src/handler.ts" in affected
        assert "apps/backend/src/handler.spec.ts" in affected
        # Should NOT affect upstream libs
        assert "libs/shared/src/util.ts" not in affected
        assert "libs/marketplace/src/product.service.ts" not in affected

    def test_unknown_file_ignored(self, nx_graph):
        affected = find_affected_files(nx_graph, {"nonexistent/file.ts"})
        assert len(affected) == 0


class TestFindTestFiles:
    def test_affected_test_file_included(self):
        affected = {"apps/backend/src/handler.ts", "apps/backend/src/handler.spec.ts"}
        all_files = affected | {"libs/shared/src/util.ts"}
        tests = find_test_files(affected, all_files)
        assert "apps/backend/src/handler.spec.ts" in tests

    def test_convention_based_discovery(self):
        affected = {"apps/backend/src/handler.ts"}
        all_files = {"apps/backend/src/handler.ts", "apps/backend/src/handler.spec.ts"}
        tests = find_test_files(affected, all_files)
        assert "apps/backend/src/handler.spec.ts" in tests

    def test_test_suffix_discovery(self):
        affected = {"src/foo.ts"}
        all_files = {"src/foo.ts", "src/foo.test.ts", "src/foo.spec.tsx"}
        tests = find_test_files(affected, all_files)
        assert "src/foo.test.ts" in tests
        assert "src/foo.spec.tsx" in tests

    def test_dunder_tests_dir(self):
        affected = {"src/foo.ts"}
        all_files = {"src/foo.ts", "src/__tests__/foo.spec.ts"}
        tests = find_test_files(affected, all_files)
        assert "src/__tests__/foo.spec.ts" in tests

    def test_no_test_files(self):
        affected = {"src/foo.ts"}
        all_files = {"src/foo.ts"}
        tests = find_test_files(affected, all_files)
        assert len(tests) == 0


class TestDiscoverJestScopes:
    def test_finds_all_configs(self):
        scopes = discover_jest_scopes(FIXTURE_DIR)
        config_paths = {s.config_path for s in scopes}
        assert "libs/shared/jest.config.ts" in config_paths
        assert "libs/marketplace/jest.config.ts" in config_paths
        assert "apps/backend/jest.config.ts" in config_paths

    def test_sorted_deepest_first(self):
        scopes = discover_jest_scopes(FIXTURE_DIR)
        depths = [s.root_dir.count("/") if s.root_dir else -1 for s in scopes]
        assert depths == sorted(depths, reverse=True)


class TestGroupTestsByJestScope:
    def test_assigns_to_correct_scope(self):
        scopes = [
            JestScope("apps/backend/jest.config.ts", "apps/backend"),
            JestScope("libs/shared/jest.config.ts", "libs/shared"),
        ]
        test_files = {"apps/backend/src/handler.spec.ts", "libs/shared/src/util.spec.ts"}
        grouped = group_tests_by_jest_scope(test_files, scopes)

        backend = next(s for s in grouped if s.root_dir == "apps/backend")
        shared = next(s for s in grouped if s.root_dir == "libs/shared")
        assert "apps/backend/src/handler.spec.ts" in backend.test_files
        assert "libs/shared/src/util.spec.ts" in shared.test_files

    def test_empty_scopes_filtered(self):
        scopes = [
            JestScope("apps/backend/jest.config.ts", "apps/backend"),
            JestScope("libs/shared/jest.config.ts", "libs/shared"),
        ]
        test_files = {"apps/backend/src/handler.spec.ts"}
        grouped = group_tests_by_jest_scope(test_files, scopes)
        assert len(grouped) == 1
        assert grouped[0].root_dir == "apps/backend"


class TestFormatJestCommands:
    def test_format(self):
        scopes = [
            JestScope("apps/backend/jest.config.ts", "apps/backend", ["apps/backend/src/handler.spec.ts"]),
        ]
        cmds = format_jest_commands(scopes)
        assert len(cmds) == 1
        assert "--config apps/backend/jest.config.ts" in cmds[0]
        assert "--findRelatedTests" in cmds[0]
        assert "apps/backend/src/handler.spec.ts" in cmds[0]


class TestEndToEnd:
    def test_full_pipeline(self, nx_extractions, nx_patterns):
        """Simulate changing util.ts and verify handler.spec.ts is found."""
        graph = build_file_graph(nx_extractions, nx_patterns)
        changed = {"libs/shared/src/util.ts"}
        affected = find_affected_files(graph, changed)
        tests = find_test_files(affected, graph.all_files)
        scopes = discover_jest_scopes(FIXTURE_DIR)
        grouped = group_tests_by_jest_scope(tests, scopes)

        # handler.spec.ts should be in the results
        assert "apps/backend/src/handler.spec.ts" in tests

        # It should be grouped under the backend jest config
        backend_scope = next(
            (s for s in grouped if "backend" in s.config_path), None
        )
        assert backend_scope is not None
        assert "apps/backend/src/handler.spec.ts" in backend_scope.test_files

        # Commands should be generated
        commands = format_jest_commands(grouped)
        assert any("backend" in cmd for cmd in commands)


class TestDiscoverNxProjects:
    def test_finds_projects_from_project_json(self):
        mapping = discover_nx_projects(FIXTURE_DIR)
        assert mapping["apps/backend"] == "backend"
        assert mapping["libs/shared"] == "shared"
        assert mapping["libs/marketplace"] == "marketplace"

    def test_fallback_to_directory_name(self, tmp_path):
        # No project.json, but has jest.config.ts
        (tmp_path / "apps" / "my-app").mkdir(parents=True)
        (tmp_path / "apps" / "my-app" / "jest.config.ts").write_text("export default {};")
        mapping = discover_nx_projects(tmp_path)
        assert mapping["apps/my-app"] == "my-app"

    def test_workspace_json_legacy(self, tmp_path):
        (tmp_path / "workspace.json").write_text(
            '{"version": 2, "projects": {"api": "apps/api", "lib": "libs/lib"}}'
        )
        mapping = discover_nx_projects(tmp_path)
        assert mapping["apps/api"] == "api"
        assert mapping["libs/lib"] == "lib"


class TestFormatNxCommands:
    def test_generates_nx_run_commands(self):
        scopes = [
            JestScope("apps/backend/jest.config.ts", "apps/backend", ["apps/backend/src/handler.spec.ts"]),
        ]
        nx_projects = {"apps/backend": "backend"}
        cmds = format_nx_commands(scopes, nx_projects)
        assert len(cmds) == 1
        assert cmds[0] == "npx nx run backend:test -- --findRelatedTests apps/backend/src/handler.spec.ts"

    def test_falls_back_to_jest_when_no_project(self):
        scopes = [
            JestScope("unknown/jest.config.ts", "unknown", ["unknown/foo.spec.ts"]),
        ]
        cmds = format_nx_commands(scopes, {})
        assert len(cmds) == 1
        assert cmds[0].startswith("npx jest")
        assert "--config unknown/jest.config.ts" in cmds[0]


class TestEndToEndNx:
    def test_full_pipeline_with_nx(self, nx_extractions, nx_patterns):
        """Simulate changing util.ts with nx=True and verify nx run commands."""
        graph = build_file_graph(nx_extractions, nx_patterns)
        changed = {"libs/shared/src/util.ts"}
        affected = find_affected_files(graph, changed)
        tests = find_test_files(affected, graph.all_files)
        scopes = discover_jest_scopes(FIXTURE_DIR)
        grouped = group_tests_by_jest_scope(tests, scopes)
        nx_projects = discover_nx_projects(FIXTURE_DIR)
        commands = format_nx_commands(grouped, nx_projects)

        # Should have nx run commands, not plain jest
        backend_cmd = next((c for c in commands if "backend" in c), None)
        assert backend_cmd is not None
        assert "npx nx run backend:test" in backend_cmd
        assert "--findRelatedTests" in backend_cmd
        assert "apps/backend/src/handler.spec.ts" in backend_cmd
