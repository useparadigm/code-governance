from __future__ import annotations

from pathlib import Path

import pytest

from code_governance.languages.typescript import TypeScriptPatterns
from code_governance.schemas import GovernanceConfig, Language, ModuleConfig


def _make_patterns(tmp_path: Path, tsconfig_body: str | None = None) -> TypeScriptPatterns:
    if tsconfig_body is not None:
        (tmp_path / "tsconfig.json").write_text(tsconfig_body)
    cfg = GovernanceConfig(
        root=".",
        language=Language.TYPESCRIPT,
        modules=[
            ModuleConfig(name="api", path="src/api/"),
            ModuleConfig(name="core", path="src/core/"),
            ModuleConfig(name="db", path="src/db/"),
        ],
    )
    p = TypeScriptPatterns()
    p.initialize(tmp_path, cfg)
    return p


def _importable_map() -> dict[str, str]:
    return {
        "src/core/models": "core",
        "src/core/index": "core",
        "src/db/repository": "db",
        "src/db/index": "db",
        "src/api/routes": "api",
    }


def test_resolve_relative_sibling(tmp_path):
    p = _make_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    assert p.resolve_import("./models", "src/core/service.ts", cfg, _importable_map(), {}) == "core"


def test_resolve_relative_parent(tmp_path):
    p = _make_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    assert p.resolve_import("../db/repository", "src/core/service.ts", cfg, _importable_map(), {}) == "db"


def test_resolve_relative_to_index(tmp_path):
    p = _make_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    # Import './db' from a file in src/; should find src/db/index
    assert p.resolve_import("../db", "src/api/routes.ts", cfg, _importable_map(), {}) == "db"


def test_alias_wildcard(tmp_path):
    p = _make_patterns(
        tmp_path,
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
    )
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    assert p.resolve_import("@/core/models", "src/api/routes.ts", cfg, _importable_map(), {}) == "core"


def test_bare_specifier_returns_none(tmp_path):
    p = _make_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    assert p.resolve_import("lodash", "src/api/routes.ts", cfg, _importable_map(), {}) is None
    assert p.resolve_import("@scope/pkg", "src/api/routes.ts", cfg, _importable_map(), {}) is None


def test_missing_file_returns_none(tmp_path):
    p = _make_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    assert p.resolve_import("./ghost", "src/core/service.ts", cfg, _importable_map(), {}) is None


def test_file_to_importable_strips_all_ts_js_extensions(tmp_path):
    p = _make_patterns(tmp_path)
    assert p.file_to_importable("src/a/b.ts") == "src/a/b"
    assert p.file_to_importable("src/a/b.tsx") == "src/a/b"
    assert p.file_to_importable("src/a/b.js") == "src/a/b"
    assert p.file_to_importable("src/a/b.jsx") == "src/a/b"
    assert p.file_to_importable("src/a/b.py") is None


# ── Implicit base-URL fallback (zero-config --auto, no tsconfig) ──────────


def _auto_patterns(tmp_path: Path) -> TypeScriptPatterns:
    """No tsconfig — exercises the implicit src-relative fallback."""
    cfg = GovernanceConfig(language=Language.TYPESCRIPT, root=".")
    p = TypeScriptPatterns()
    p.initialize(tmp_path, cfg)
    return p


def test_implicit_bare_specifier_resolves(tmp_path):
    p = _auto_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    imap = {"scenes/urls": "scenes", "lib/api": "lib"}
    assert p.resolve_import("scenes/urls", "lib/foo.ts", cfg, imap, {}) == "scenes"
    assert p.resolve_import("lib/api", "scenes/foo.ts", cfg, imap, {}) == "lib"


def test_implicit_tilde_and_at_alias_resolves(tmp_path):
    p = _auto_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    imap = {"toolbar/config": "toolbar", "queries/schema": "queries"}
    assert p.resolve_import("~/toolbar/config", "lib/a.ts", cfg, imap, {}) == "toolbar"
    assert p.resolve_import("@/queries/schema", "lib/a.ts", cfg, imap, {}) == "queries"


def test_implicit_does_not_resolve_third_party(tmp_path):
    p = _auto_patterns(tmp_path)
    cfg = GovernanceConfig(language=Language.TYPESCRIPT)
    imap = {"lib/api": "lib"}
    # bare packages and scoped packages must not match local modules
    assert p.resolve_import("react", "lib/a.ts", cfg, imap, {}) is None
    assert p.resolve_import("@posthog/icons", "lib/a.ts", cfg, imap, {}) is None
    assert p.resolve_import("kea-router", "lib/a.ts", cfg, imap, {}) is None


# ── Configured mode with root subdir + tsconfig alias (Next.js / homi) ─────


def _write_homi(tmp_path: Path, alias_import: str, base_url: str, paths: str):
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "tsconfig.json").write_text(
        '{ "compilerOptions": { "baseUrl": "%s", "paths": %s } }' % (base_url, paths)
    )
    (tmp_path / "src" / "app" / "page.tsx").write_text(
        f"import {{ a }} from '{alias_import}'\nexport const x = a\n"
    )
    (tmp_path / "src" / "lib" / "util.ts").write_text("export const a = 1\n")
    (tmp_path / "governance.toml").write_text(
        '[governance]\nroot = "src"\nlanguage = "typescript"\n'
        '[[modules]]\nname = "app"\npath = "app/"\ncannot_depend_on = ["lib"]\n'
        '[[modules]]\nname = "lib"\npath = "lib/"\n'
        "[rules]\nenforce_cannot_depend_on = true\n"
    )


def test_configured_alias_resolves_with_root_subdir(tmp_path):
    # Regression: root="src" + tsconfig `@/*` -> aliases resolved to repo-root
    # paths that never matched source-root-relative importables, so app->lib
    # edges were dropped and cannot_depend_on could never fire (false PASS).
    from code_governance.engine import run_governance
    _write_homi(tmp_path, "@/lib/util", ".", '{ "@/*": ["./src/*"] }')
    report = run_governance(tmp_path / "governance.toml")
    app = next(m for m in report.metrics if m.name == "app")
    assert app.external_edges == 1  # app -> lib resolved
    forbidden = [v for v in report.violations if v.rule.value == "enforce_cannot_depend_on"]
    assert len(forbidden) == 1 and not report.passed


def test_configured_alias_baseurl_src(tmp_path):
    from code_governance.engine import run_governance
    _write_homi(tmp_path, "@/lib/util", "src", '{ "@/*": ["*"] }')
    report = run_governance(tmp_path / "governance.toml")
    app = next(m for m in report.metrics if m.name == "app")
    assert app.external_edges == 1
    assert not report.passed
