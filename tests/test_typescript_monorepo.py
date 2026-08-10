"""Monorepo-shaped TypeScript resolution: workspace packages, dynamic imports,
source-root tsconfig discovery, and test-file detection beyond .ts/.tsx."""

from __future__ import annotations

import json
from pathlib import Path

from code_governance.engine import discover_dependencies
from code_governance.extractor import extract_directory, extract_file
from code_governance.languages.typescript import TypeScriptPatterns, discover_workspace_packages
from code_governance.schemas import Language


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _monorepo(tmp_path: Path) -> Path:
    """apps/web + two workspace packages, wired the way npm workspaces do it."""
    _write(tmp_path / "package.json", json.dumps({
        "name": "root", "private": True, "workspaces": ["apps/*", "packages/*"],
    }))
    _write(tmp_path / "packages/ui/package.json", json.dumps({
        "name": "@acme/ui",
        "exports": {".": "./src/index.ts", "./button": "./src/button.ts"},
    }))
    _write(tmp_path / "packages/ui/src/index.ts", "export const ui = 1;\n")
    _write(tmp_path / "packages/ui/src/button.ts", "export const button = 1;\n")
    _write(tmp_path / "packages/util/package.json", json.dumps({
        "name": "@acme/util", "main": "./src/main.ts",
    }))
    _write(tmp_path / "packages/util/src/main.ts", "export const util = 1;\n")
    _write(tmp_path / "apps/web/package.json", json.dumps({"name": "@acme/web"}))
    _write(tmp_path / "apps/web/src/app.ts", (
        'import { ui } from "@acme/ui";\n'
        'import { button } from "@acme/ui/button";\n'
        'import { util } from "@acme/util";\n'
        'import { unrelated } from "react";\n'
        "export const app = [ui, button, util, unrelated];\n"
    ))
    _write(tmp_path / "governance.toml", (
        '[governance]\nroot = "."\nlanguage = "typescript"\n\n'
        '[[modules]]\nname = "web"\npath = "apps/web/"\n\n'
        '[[modules]]\nname = "ui"\npath = "packages/ui/"\n\n'
        '[[modules]]\nname = "util"\npath = "packages/util/"\n\n'
        "[rules]\nno_cycles = true\n"
    ))
    return tmp_path


def test_workspace_packages_discovered(tmp_path):
    packages = discover_workspace_packages(_monorepo(tmp_path))
    # longest name first, so `@acme/util` can never be shadowed by a `@acme/ut` prefix
    assert [(name, d) for name, d, _ in packages] == [
        ("@acme/util", "packages/util"),
        ("@acme/web", "apps/web"),
        ("@acme/ui", "packages/ui"),
    ]


def test_workspace_package_imports_become_edges(tmp_path):
    root = _monorepo(tmp_path)
    report = discover_dependencies(root / "governance.toml")
    targets = {t.target: t.count for t in report.dependencies.get("web", [])}
    # both the root export and the ./button subpath export resolve
    assert targets.get("ui") == 2
    assert targets.get("util") == 1


def test_bare_third_party_specifier_stays_external(tmp_path):
    """`react` must not be mistaken for a workspace package."""
    root = _monorepo(tmp_path)
    report = discover_dependencies(root / "governance.toml")
    assert {t.target for t in report.dependencies.get("web", [])} == {"ui", "util"}


def test_tsconfig_is_found_at_the_source_root(tmp_path):
    """The config file may live outside the code it governs — aliases still resolve."""
    src = tmp_path / "repo"
    _write(src / "tsconfig.json", json.dumps({"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}))
    _write(src / "src/api/handler.ts", 'import { db } from "@/core/db";\nexport const handler = db;\n')
    _write(src / "src/core/db.ts", "export const db = 1;\n")
    cfg = tmp_path / "elsewhere" / "governance.toml"
    _write(cfg, (
        '[governance]\nroot = "../repo"\nlanguage = "typescript"\n\n'
        '[[modules]]\nname = "api"\npath = "src/api/"\n\n'
        '[[modules]]\nname = "core"\npath = "src/core/"\n'
    ))

    report = discover_dependencies(cfg)
    assert {t.target for t in report.dependencies.get("api", [])} == {"core"}


def test_dynamic_import_is_extracted():
    result = extract_file("a.ts", (
        "export async function load() {\n"
        '  const { heavy } = await import("./heavy");\n'
        "  return heavy;\n"
        "}\n"
    ), Language.TYPESCRIPT)
    dynamic = [i for i in result.imports if i.source_module == "./heavy"]
    assert len(dynamic) == 1
    assert dynamic[0].line == 2


def test_dynamic_import_becomes_an_edge(tmp_path):
    _write(tmp_path / "src/api/handler.ts",
           'export const load = () => import("../core/heavy");\n')
    _write(tmp_path / "src/core/heavy.ts", "export const heavy = 1;\n")
    cfg = tmp_path / "governance.toml"
    _write(cfg, (
        '[governance]\nroot = "."\nlanguage = "typescript"\n\n'
        '[[modules]]\nname = "api"\npath = "src/api/"\n\n'
        '[[modules]]\nname = "core"\npath = "src/core/"\n'
    ))

    report = discover_dependencies(cfg)
    assert {t.target for t in report.dependencies.get("api", [])} == {"core"}


def test_import_in_type_position_is_flagged_type_only():
    """`typeof import("x")` and `: import("x").T` parse as call_expressions too, but
    they are erased at compile time — a real dependency, never a runtime import."""
    result = extract_file("a.ts", (
        'type Mod = typeof import("./other");\n'
        'const value: import("./shape").Shape = load();\n'
        'const eager = await import("./heavy");\n'
    ), Language.TYPESCRIPT)
    by_source = {i.source_module: i.type_only for i in result.imports}
    assert by_source == {"./other": True, "./shape": True, "./heavy": False}


def test_mjs_and_mts_test_files_are_excluded(tmp_path):
    _write(tmp_path / "app.config.test.mjs", "export const spec = 1;\n")
    _write(tmp_path / "helpers.test.mts", "export const spec = 2;\n")
    _write(tmp_path / "real.mjs", "export const real = 3;\n")

    scanned = {r.file_path for r in extract_directory(tmp_path, Language.TYPESCRIPT)}
    assert scanned == {"real.mjs"}

    with_tests = {r.file_path for r in extract_directory(tmp_path, Language.TYPESCRIPT, exclude_test_files=False)}
    assert with_tests == {"real.mjs", "app.config.test.mjs", "helpers.test.mts"}


def test_workspace_resolution_is_inert_without_a_workspace_manifest(tmp_path):
    patterns = TypeScriptPatterns()
    from code_governance.schemas import GovernanceConfig
    patterns.initialize(tmp_path, GovernanceConfig(root=".", language=Language.TYPESCRIPT))
    assert patterns._apply_workspace("@acme/ui") == []
