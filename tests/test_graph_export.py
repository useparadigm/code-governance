"""The file-level graph export and the findings it feeds.

These pin down the behaviour that made the export worth generalising out of a
one-off script: per-package alias resolution, cycles judged on runtime edges, and
entry-point detection that keeps framework routes out of the dead-code list.
"""
import json

from code_governance.graph_export import _glob_match, build_graph_payload
from code_governance.graph_findings import (
    Graph,
    file_cycles,
    folder_cycles,
    orphans,
    project_coupling,
    render_markdown,
)


def _write(root, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _monorepo(root) -> None:
    """Two apps that each alias `@/*` to their own src — the shape that made a
    single repo-wide tsconfig resolve one app's imports onto the other's files."""
    _write(root, {
        "package.json": json.dumps({"name": "root", "workspaces": ["apps/*", "packages/*"]}),
        "apps/web/package.json": json.dumps({"name": "@acme/web"}),
        "apps/web/tsconfig.json": json.dumps({"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}),
        "apps/web/src/page.ts": "import { helper } from '@/lib/helper';\nexport const page = helper;\n",
        "apps/web/src/lib/helper.ts": "export const helper = 1;\n",
        "apps/mobile/package.json": json.dumps({"name": "@acme/mobile"}),
        "apps/mobile/tsconfig.json": json.dumps({"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}),
        "apps/mobile/src/screen.ts": "import { helper } from '@/lib/helper';\nexport const screen = helper;\n",
        "apps/mobile/src/lib/helper.ts": "export const helper = 2;\n",
    })


def _edge_map(payload) -> dict[tuple[str, str], int]:
    files = payload["files"]
    return {(files[s], files[t]): count for s, t, count, *_ in payload["edges"]}


def test_alias_resolves_against_the_importing_packages_tsconfig(tmp_path):
    _monorepo(tmp_path)
    edges = _edge_map(build_graph_payload(tmp_path, include_sources=False))

    assert edges[("apps/web/src/page.ts", "apps/web/src/lib/helper.ts")] == 1
    assert edges[("apps/mobile/src/screen.ts", "apps/mobile/src/lib/helper.ts")] == 1
    # the same `@/lib/helper` specifier must not cross the app boundary
    assert ("apps/web/src/page.ts", "apps/mobile/src/lib/helper.ts") not in edges


def test_type_only_sites_are_counted_but_kept_out_of_cycles(tmp_path):
    _write(tmp_path, {
        "src/a.ts": "import type { B } from './b';\nexport const a = (b: B) => b;\n",
        "src/b.ts": "import { a } from './a';\nexport type B = number;\nexport const b = a;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    by_pair = {(payload["files"][s], payload["files"][t]): (count, type_only)
               for s, t, count, _lines, type_only, *_ in payload["edges"]}

    # the coupling is still recorded — it just cannot close a runtime loop
    assert by_pair[("src/a.ts", "src/b.ts")] == (1, 1)
    assert by_pair[("src/b.ts", "src/a.ts")] == (1, 0)

    g = Graph(payload)
    assert file_cycles(g) == []
    assert len(file_cycles(g, value_only=False)) == 1


def test_folder_cycle_is_found_below_module_granularity(tmp_path):
    _write(tmp_path, {
        "src/x/one.ts": "import { two } from '../y/two';\nexport const one = two;\n",
        "src/y/two.ts": "import { other } from '../x/other';\nexport const two = other;\n",
        "src/x/other.ts": "export const other = 1;\n",
    })
    g = Graph(build_graph_payload(tmp_path, include_sources=False))

    assert file_cycles(g) == []  # no single file imports itself back
    cycles = folder_cycles(g, g.value_weight)
    assert [(path, members) for path, members, _ in cycles] == [("src", ["x", "y"])]


def test_framework_routes_and_published_entries_are_not_dead_code(tmp_path):
    _write(tmp_path, {
        "next.config.js": "module.exports = {};\n",
        "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"]}),
        "packages/img/package.json": json.dumps({
            "name": "@acme/img", "exports": {"./browser": "./src/browser.ts"},
        }),
        # nothing in the repo imports it; the bundler picks it by condition
        "packages/img/src/browser.ts": "export const compress = 1;\n",
        "app/dashboard/page.tsx": "export default function Page() { return null; }\n",
        "src/orphan.ts": "export const unused = 1;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    entries = {payload["files"][i] for i in payload["entries"]}

    assert "app/dashboard/page.tsx" in entries
    assert "packages/img/src/browser.ts" in entries
    assert "src/orphan.ts" not in entries

    dead, _test_only = orphans(Graph(payload))
    assert [payload["files"][i] for i in dead] == ["src/orphan.ts"]


def test_django_conventions_are_not_dead_code(tmp_path):
    """Django reaches these by string or by autodiscovery — `manage.py <name>`,
    `INSTALLED_APPS`, `ROOT_URLCONF` — so nothing ever imports them."""
    _write(tmp_path, {
        "manage.py": "import sys\n",
        "posthog/apps.py": "class Config:\n    pass\n",
        "posthog/urls.py": "urlpatterns = []\n",
        "posthog/admin.py": "admin_site = 1\n",
        "posthog/management/commands/backfill.py": "class Command:\n    pass\n",
        "posthog/templatetags/assets.py": "def tag():\n    pass\n",
        "posthog/orphan.py": "def unused():\n    pass\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    entries = {payload["files"][i] for i in payload["entries"]}

    assert "posthog/management/commands/backfill.py" in entries
    assert "posthog/templatetags/assets.py" in entries
    assert "posthog/apps.py" in entries
    assert "posthog/orphan.py" not in entries
    assert "django" in payload["stats"]["entry_presets"]

    dead, _test_only = orphans(Graph(payload))
    assert [payload["files"][i] for i in dead] == ["posthog/orphan.py"]


def test_django_rules_stay_off_without_manage_py(tmp_path):
    _write(tmp_path, {
        "pkg/apps.py": "class Config:\n    pass\n",
        "pkg/orphan.py": "def unused():\n    pass\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    dead = {payload["files"][i] for i in orphans(Graph(payload))[0]}

    assert dead == {"pkg/apps.py", "pkg/orphan.py"}
    assert "django" not in payload["stats"]["entry_presets"]


def test_submodule_import_behind_an_alias_keeps_its_target_alive(tmp_path):
    """`from pkg import sub as alias` must land on `sub.py`, not on the package
    `__init__.py` — otherwise the submodule reads as dead."""
    _write(tmp_path, {
        "manage.py": "import sys\n",
        "pkg/__init__.py": "",
        "pkg/apps.py": "from pkg.api import registrations as reg\n",
        "pkg/api/__init__.py": "",
        "pkg/api/registrations.py": "def register():\n    pass\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    edges = _edge_map(payload)

    assert ("pkg/apps.py", "pkg/api/registrations.py") in edges
    assert ("pkg/apps.py", "pkg/api/__init__.py") not in edges

    dead, _test_only = orphans(Graph(payload))
    assert [payload["files"][i] for i in dead] == []


def test_asset_imports_do_not_create_a_barrel_cycle(tmp_path):
    _write(tmp_path, {
        "src/Spinner/Spinner.tsx": "import './Spinner.scss';\nexport const Spinner = 1;\n",
        "src/Spinner/Spinner.scss": ".spinner {}\n",
        "src/Spinner/index.ts": "export * from './Spinner';\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    edges = _edge_map(payload)

    assert ("src/Spinner/index.ts", "src/Spinner/Spinner.tsx") in edges
    assert ("src/Spinner/Spinner.tsx", "src/Spinner/index.ts") not in edges
    assert file_cycles(Graph(payload)) == []


def test_custom_entry_globs_suppress_a_directory(tmp_path):
    _write(tmp_path, {
        "generated/a.ts": "export const a = 1;\n",
        "src/orphan.ts": "export const unused = 1;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False, entry_globs=("generated/**",))
    dead, _ = orphans(Graph(payload))

    assert [payload["files"][i] for i in dead] == ["src/orphan.ts"]


def test_test_only_helpers_are_reported_apart_from_dead_code(tmp_path):
    _write(tmp_path, {
        "src/helper.ts": "export const helper = 1;\n",
        "src/dead.ts": "export const dead = 1;\n",
        "src/helper.test.ts": "import { helper } from './helper';\nhelper;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    dead, test_only = orphans(Graph(payload))

    assert [payload["files"][i] for i in dead] == ["src/dead.ts"]
    assert [payload["files"][i] for i in test_only] == ["src/helper.ts"]
    assert "src/helper.test.ts" not in payload["files"]  # tests are not graph nodes


def test_symbol_usage_is_attributed_through_a_barrel(tmp_path):
    _write(tmp_path, {
        "src/impl.ts": "export const thing = 1;\n",
        "src/index.ts": "export { thing } from './impl';\n",
        "src/consumer.ts": "import { thing } from './index';\nexport const used = thing;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    index = {f: i for i, f in enumerate(payload["files"])}
    users = payload["symbol_users"]

    # usage lands on the file that declares `thing`, not the barrel it came through
    assert "thing" in users[index["src/impl.ts"]]
    assert users[index["src/impl.ts"]]["thing"][0][0] == index["src/consumer.ts"]


def test_python_exports_use_dunder_all_and_relative_reexports(tmp_path):
    _write(tmp_path, {
        "pkg/__init__.py": "from .impl import Thing\n",
        "pkg/impl.py": "__all__ = ['Thing']\n\nclass Thing:\n    pass\n\nclass Hidden:\n    pass\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    exports = {f: payload["exports"][i] for i, f in enumerate(payload["files"])}

    assert [name for name, _line, _src in exports["pkg/impl.py"]] == ["Thing"]
    assert [(name, src) for name, _line, src in exports["pkg/__init__.py"]] == [("Thing", ".impl")]


def test_dynamic_import_keeps_a_file_out_of_the_dead_list(tmp_path):
    _write(tmp_path, {
        "src/index.ts": "export async function go() { return import('./heavy'); }\n",
        "src/heavy.ts": "export const heavy = 1;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    dead, _ = orphans(Graph(payload))

    # `heavy` is reached only by a code-split call site — a static-only pass would
    # call it dead
    assert [payload["files"][i] for i in dead] == []


def test_coupling_is_grouped_by_workspace_package(tmp_path):
    _monorepo(tmp_path)
    _write(tmp_path, {
        "packages/shared/package.json": json.dumps({"name": "@acme/shared"}),
        "packages/shared/src/index.ts": "export const shared = 1;\n",
        "apps/web/src/uses-shared.ts": "import { shared } from '@acme/shared';\nexport const x = shared;\n",
    })
    payload = build_graph_payload(tmp_path, include_sources=False)
    rows = {name: (ce, ca) for name, ce, ca, _i, _internal in project_coupling(Graph(payload))}

    assert rows["apps/web"][0] == 1  # one import leaving web
    assert rows["packages/shared"][1] == 1  # arriving at shared


def test_report_renders_without_a_viewer(tmp_path):
    _write(tmp_path, {"src/a.ts": "export const a = 1;\n"})
    md = render_markdown(build_graph_payload(tmp_path, include_sources=False), viewer=None)

    assert "## 1. Import cycles between files" in md
    assert "None. Every import chain that survives compilation terminates." in md
    assert ".html" not in md


def test_glob_matcher_spans_and_stops_at_separators():
    assert _glob_match("ai/**", "ai/x/y.js")
    assert _glob_match("ai/**", "ai/y.js")
    assert not _glob_match("ai/**", "apps/ai/y.js")
    assert _glob_match("**/shims/*", "a/b/shims/x.ts")
    assert _glob_match("**/shims/*", "shims/x.ts")
    assert _glob_match("*.config.ts", "next.config.ts")
    assert not _glob_match("*.config.ts", "a/next.config.ts")
    assert not _glob_match("src/*.ts", "src/a/b.ts")
