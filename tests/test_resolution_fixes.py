"""Regression tests for import-resolution and module-discovery hardening.

Covers the fixes that eliminated large classes of false negatives on real
codebases (posthog, django, sklearn, httpx, flask):

  * absolute self-imports (`from pkg.sub import x`) resolved in auto-scan
  * top-level module granularity (no per-directory explosion / nesting cycles)
  * single-underscore dirs/files kept (`_transports`, `_internal`)
  * bare-relative / bare-absolute submodule imports (`from . import sub`)
  * root-level relative imports (`from .b import x` in a package __init__)
"""

from __future__ import annotations

from pathlib import Path

from code_governance.dep_graph import build_dependency_graph
from code_governance.engine import _discover_modules, run_auto_scan
from code_governance.extractor import extract_directory
from code_governance.schemas import GovernanceConfig, Language


def _write(root: Path, rel: str, body: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# ── Module discovery ──────────────────────────────────────────────────────


def test_top_level_granularity_groups_nested_dirs(tmp_path):
    pkg = tmp_path / "pkg"
    _write(pkg, "a/__init__.py")
    _write(pkg, "a/deep/nested/mod.py")
    _write(pkg, "b/svc.py")
    mods = {m.name for m in _discover_modules(pkg, {".py"}, max_depth=1)}
    assert mods == {"a", "b"}  # a/deep/nested collapsed into a


def test_depth_zero_is_unlimited(tmp_path):
    pkg = tmp_path / "pkg"
    _write(pkg, "a/top.py")
    _write(pkg, "a/deep/mod.py")
    mods = {m.name for m in _discover_modules(pkg, {".py"}, max_depth=0)}
    assert mods == {"a", "a.deep"}  # every source dir is its own module


def test_single_underscore_dirs_and_files_kept(tmp_path):
    pkg = tmp_path / "httpxlike"
    _write(pkg, "_api.py")
    _write(pkg, "_client.py")
    _write(pkg, "_transports/base.py")
    mods = {m.name for m in _discover_modules(pkg, {".py"}, max_depth=1)}
    # loose underscore files -> root module; _transports kept as its own module
    assert "_transports" in mods
    assert "httpxlike" in mods  # root module named after dir


def test_dunder_and_hidden_dirs_skipped(tmp_path):
    pkg = tmp_path / "pkg"
    _write(pkg, "real/mod.py")
    _write(pkg, "__pycache__/junk.py")
    _write(pkg, ".hidden/x.py")
    mods = {m.name for m in _discover_modules(pkg, {".py"}, max_depth=1)}
    assert mods == {"real"}


# ── Absolute self-imports ─────────────────────────────────────────────────


def test_absolute_self_import_resolves_in_auto(tmp_path):
    pkg = tmp_path / "myapp"
    _write(pkg, "models/user.py", "class User: pass\n")
    _write(pkg, "api/routes.py", "from myapp.models.user import User\n")
    report = run_auto_scan(pkg)
    names = {m.name for m in report.metrics}
    assert {"models", "api"} <= names
    # api -> models edge must exist (would be dropped without package_prefix)
    api = next(m for m in report.metrics if m.name == "api")
    assert api.external_edges >= 1


# ── Bare submodule imports + root-level relatives ─────────────────────────


def _graph_for(tmp_path):
    pkg = tmp_path / "pkg"
    _write(pkg, "__init__.py", "from . import a\nfrom .b import thing\n")
    _write(pkg, "a/__init__.py", "from pkg.b import other\nfrom ..b import deep\n")
    _write(pkg, "a/use.py", "from . import sibling\nfrom .sibling import X\n")
    _write(pkg, "a/sibling.py", "X = 1\n")
    _write(pkg, "b/__init__.py", "thing = 1\nother = 2\ndeep = 3\n")
    config = GovernanceConfig(
        root=".",
        language=Language.PYTHON,
        package_prefix="pkg",
        modules=_discover_modules(pkg, {".py"}, max_depth=1),
    )
    extractions = extract_directory(pkg, Language.PYTHON, exclude_test_files=True)
    return build_dependency_graph(extractions, config)


def test_bare_relative_submodule_import(tmp_path):
    graph = _graph_for(tmp_path)
    root_deps = graph.get_module_dependencies("pkg")
    # `from . import a` and `from .b import thing` at package root
    assert "a" in root_deps
    assert "b" in root_deps


def test_upward_relative_and_bare_absolute(tmp_path):
    graph = _graph_for(tmp_path)
    a_deps = graph.get_module_dependencies("a")
    # `from pkg.b import other` and `from ..b import deep`
    assert "b" in a_deps


def test_same_module_relative_is_internal(tmp_path):
    graph = _graph_for(tmp_path)
    # a/use.py `from . import sibling` stays inside module a (not a cross edge)
    a_deps = graph.get_module_dependencies("a")
    assert "a" not in a_deps  # no self cross-edge
    assert graph.module_internal_edges.get("a", 0) >= 1


def test_symbol_import_does_not_invent_module_edge(tmp_path):
    # `from . import SomeClass` where SomeClass is not a submodule must not
    # create an edge to a phantom module.
    pkg = tmp_path / "pkg"
    _write(pkg, "core/__init__.py", "Thing = 1\n")
    _write(pkg, "core/files.py", "from . import Thing\n")
    config = GovernanceConfig(
        root=".", language=Language.PYTHON, package_prefix="pkg",
        modules=_discover_modules(pkg, {".py"}, max_depth=1),
    )
    extractions = extract_directory(pkg, Language.PYTHON, exclude_test_files=True)
    graph = build_dependency_graph(extractions, config)
    core_deps = graph.get_module_dependencies("core")
    assert core_deps == set()  # resolves to self (internal), no phantom edge


# ── Stdlib shadowing ──────────────────────────────────────────────────────


def test_bare_stdlib_import_does_not_link_local_shadow(tmp_path):
    # local `json` package + `import json` (stdlib) must NOT create an edge.
    pkg = tmp_path / "app"
    _write(pkg, "json/__init__.py", "x = 1\n")
    _write(pkg, "core/svc.py", "import json\nimport logging\nfrom app.core.h import H\n")
    _write(pkg, "core/h.py", "H = 1\n")
    report = run_auto_scan(pkg)
    core = next(m for m in report.metrics if m.name == "core")
    assert core.external_edges == 0  # json/logging are stdlib, not local


def test_package_prefixed_shadow_still_resolves(tmp_path):
    # `from app.json import x` IS the local json package and must resolve.
    pkg = tmp_path / "app"
    _write(pkg, "json/__init__.py", "x = 1\n")
    _write(pkg, "core/svc.py", "from app.json import x\n")
    report = run_auto_scan(pkg)
    core = next(m for m in report.metrics if m.name == "core")
    assert core.external_edges >= 1
