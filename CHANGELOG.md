# Changelog

## Unreleased

### File-level dependency graph (`--graph`)

A drill-down view of the import graph, built from the same extractors and the
same import resolution the rules use, so its numbers agree with `--auto` by
construction. Started life as a one-off script pointed at one monorepo; this is
that analysis with every repo-specific assumption replaced by detection.

- **`--graph PATH`** emits a findings report (`--format text`), the raw payload
  (`--format json`) or a self-contained viewer (`--format html`);
  `--graph-out DIR` writes `findings.md` + `graph.html` together.
- **Folder cycles at every nesting depth.** `no_cycles` only sees the module
  granularity a config declares, so a cycle between two sibling subfolders three
  levels down is invisible to it. Reported per level, with the import-site count.
- **Cycles are judged on runtime edges.** Type-only imports are listed separately
  instead of being counted as loops — the rule `no_file_cycles` already worked
  this way, and now the reports agree with it.
- **Dead code vs test-only helpers.** Test files are scanned in a second pass:
  they are not graph nodes, but what they import is what separates genuinely
  unreferenced code from a helper only the suite uses.
- **Entry-point detection replaces per-repo path lists.** Framework routes
  (Next.js, Expo Router, Vite), configs, ambient declarations, and every file a
  workspace package names in its `exports` map are treated as run-not-imported.
  Which conventions fired is printed in the report. `--entry GLOB` extends it.
- **Symbol-level attribution through barrels.** `export { x } from "./y"` and
  `export * from "./y"` chains are walked so usage lands on the file that
  declares the symbol — otherwise every barrelled module reads as unused.
- **The graph is readable and movable.** Boxes drag (positions kept per level, with
  a reset), and edges re-route as they move. Edges leave and land on assigned slots
  along the box edges instead of all meeting at the centre, side-by-side boxes are
  joined by an arc over the row one way and under it the other — so a mutual import
  reads as two arcs rather than one line drawn twice — and back-edges are staggered
  around the right-hand side. Every arrow is now one width and one head size: weight
  drove stroke width before, which made a heavy edge and a light one different
  *shapes* and neither legible where they crossed.
- **Hovering an arrow says what it carries**: import sites, how many are type-only,
  whether it closes a cycle, and each imported name with what that name is — function,
  class, type, component, hook, re-export — read off the declaration in the file that
  exports it (naming convention as the fallback under `--no-sources`). Clicking an
  arrow lists every site; `edge labels` puts a short form on the line itself.
- **Cycle focus in the viewer.** The header's cycle count is a button: it dims
  everything outside the loops, numbers the ring in order on the boxes, and lists
  each cycle as modules-in-sequence plus the `file:line` imports that close it,
  each one a click away from the import statement to delete. Loops are ranked so
  the shortest ring inside a large tangle is what you see — a 20-module component
  is unreadable, the 3-hop loop inside it is the thing to break. Where the loop is
  between folders and no single file ring exists (`a/x → b/y`, `b/z → a/w`), it
  says so and shows the import closing each hop. Arrowheads no longer scale with
  edge weight, and in-graph badges (`cyc`, `hub`, `uncalled`) render filled
  instead of blank — `.node rect` was overriding their colour.
- **The graph untangles itself.** Layering decided which row a box belonged to and
  nothing decided its column, so each row was sorted by degree — an order that
  ignores where a box's neighbours sit and crosses edges for no reason. Rows are
  now ordered by the second half of Sugiyama: weighted-median sweeps in both
  directions, then adjacent-swap refinement, keeping the best-scoring pass. Every
  edge that skips a row is split into one placeholder per row it crosses, which
  is what lets the sweeps account for a long edge at all, and each placeholder
  reserves a corridor in its row, so the edge is routed through the gap between
  two boxes instead of straight over them. On this repo's own `code_governance/`
  level that is 64 edge crossings down to 7. Dragging a box still overrides its
  position — it just should not be the only way to read the diagram.
- **Corridors run straight.** Ordering says who is left of whom; it does not say
  where. Packing each row flush left meant a corridor reserved in four rows landed
  in a different column in each of them, and the edge wove down the page through
  lanes that never lined up. X is now its own pass: each box is pulled towards the
  median of what it connects to in the row above, then the row below, most-connected
  first, with placeholders outranking real boxes — a straight long edge is worth
  more than a centred node. Corridor wander drops from 239px to 46px and the
  direction changes along an edge from 27 to 6.
- **A layer that does not fit becomes rows, not a wrapped band.** Over-wide layers
  used to flow-wrap at draw time, which quietly stopped them being layers: edges
  inside the band turned into same-row arcs, and neither the ordering nor the x
  pass could line a corridor up against a box that had landed in a different band.
  The split now happens before ordering, into ordinary consecutive rows. Nothing is
  lost by it — nodes of one layer do not depend on each other — and it holds at any
  canvas width, down to one box per row.
- **The chrome gets out of the way.** Two full-width legend bands and four counts
  in the header were permanent furniture around a diagram that needs the room. The
  counts (folders, files, edges, lines of code) hang off the breadcrumb and appear
  on hover; the glyph keys, badges and edge meanings live in one card behind a
  `legend` button in the bottom-left corner (hover to read, click to pin). The
  cycle count stays in the header — it is a button, not a number.
- **`hide leaves`** drops the files that import nothing at this level. It is a
  second pass over the level rather than a render-time filter, so counts, layers
  and cycle detection agree with what is drawn, and a hidden file cannot come back
  as a ghost box. One pass, not to fixpoint: peeling until nothing is left would
  empty the graph rather than simplify it. Badges still describe the code, not the
  filtered view.
- **Retracing the files you opened.** The code pane carries back *and* forward,
  both greyed when there is nowhere to go, with an `✕` on the right to close it —
  back no longer doubles as close. In `imports` / `imported by`, one click shows a
  file while the panel stays on the one you are studying, so you can read five
  callers without losing the list; double-click follows it to the import site. A
  `view` button by the panel's filename puts it back on screen, at the line you
  left.
- **Hovering an import points at it in the diagram.** A resolved import line in
  the source, or a row in either list, lights both boxes and the arrow between
  them, lifts that arrow above the boxes it would otherwise pass behind, and
  scrolls the canvas just far enough to hold the pair. Following one keeps it lit
  after the click, through re-renders and drags, until you follow another.
- **Coupling is grouped by workspace package** when the repo has them, top-level
  directory otherwise. `ExportInfo` and `LanguagePatterns.extract_exports` are
  new, implemented for both languages (Python infers surface from `__all__`,
  top-level definitions, and relative re-exports).

### Accuracy on a large Django + React monorepo

Found by running `--graph` against PostHog (6988 Python files, 6915 TypeScript,
one repo). Every number below is from that run.

- **`extends` no longer re-anchors an inherited `baseUrl`.** `frontend/tsconfig.json`
  holding nothing but `{"extends": "../tsconfig.json"}` made the parent's
  `baseUrl: "frontend/"` resolve under the *child's* directory, so every
  `lib/*`, `scenes/*` and `~/*` target pointed at `frontend/frontend/`. Aliases
  resolved at 0%; `src/types.ts` reported 5 importers against a real 1518. Both
  `baseUrl` and `paths` are now anchored to the config that declares them, as
  `tsc` does. Frontend edges: 5069 → 21618.
- **`from pkg import sub as alias` keeps its name.** The Python extractor read
  plain names but skipped `aliased_import` nodes, so the name that distinguishes
  a submodule from a symbol was lost and the edge landed on the package
  `__init__.py` — leaving the submodule with no importers and a place in the
  dead list.
- **Django entry points.** Gated on `manage.py`: `apps.py`, `urls.py`, `admin.py`,
  `**/management/commands/*.py` and `**/templatetags/*.py` are reached by string
  (`ROOT_URLCONF`, `INSTALLED_APPS`) or by autodiscovery, never by import. Without
  them 124 of PostHog's 148 management commands were reported as dead code. Dead
  list: 337 → 161. `EntryRule` takes whole-path `globs` for conventions a single
  path segment cannot express.
- **Asset specifiers are not module edges.** `import './Spinner.scss'` had its
  extension stripped and fell back to the directory index, resolving onto the
  sibling barrel — which re-exports the importer, inventing a two-file cycle.
  `.scss`, `.css`, images, fonts, media and `.json` now resolve to nothing.
  Frontend file cycles: 28 → 17.
- **The resolve rate is reported.** It was computed into `stats` and never shown.
  `summary_line` and the findings header now print it, because a collapsed
  percentage is what distinguishes "this repo has few dependencies" from "the
  aliases are not being read" — the failure that hid the tsconfig bug above.

### Fixed

- **Python files reported an empty public surface.** Without `__all__`, the export
  list is every top-level definition, but the "is this nested?" check compared two
  *wrappers* around the same syntax node with `is` — never true, so every class and
  function was treated as nested and dropped. Exports, unused-symbol detection and
  the viewer's symbol panel were empty for every plain Python module.
- **Per-package tsconfig aliases.** Only the source root's `tsconfig.json` was
  read, so in a monorepo where each app aliases `@/*` to its own `src`, every
  app's aliases resolved through one config — mapping imports onto another app's
  files or onto nothing. On a two-app Next.js + Expo repo this dropped ~70% of
  edges (1390 resolved, vs 4633 with the nearest-ancestor config). The tsconfig
  that applies is now the nearest one above the importing file, as `tsc` does.
- **JS/TS build output is skipped when scanning.** `.next`, `.turbo`, `.expo`,
  `.vercel`, `.nuxt`, `.svelte-kit`, `.output`, `.parcel-cache`, `coverage` and
  `storybook-static` hold generated code that mirrors real source, so scanning
  them double-counted every file they shadow.

### TypeScript monorepo accuracy

Found by running the engine against a Next.js + Expo + npm-workspaces monorepo
(2110 files): every cross-package edge was missing and the run still exited
`PASSED`.

- **npm/yarn/pnpm workspace packages resolve.** `import { x } from "@acme/ui"`
  looked like a third-party specifier, so *all* cross-package edges were dropped —
  a monorepo reported every module with `out=0` and no cycles. Workspace globs are
  read from the nearest enclosing `package.json`, and subpath imports
  (`@acme/ui/button`) resolve through the package's `exports` map, including
  conditional and wildcard entries, with `main` and `src/` layout fallbacks.
- **tsconfig is discovered from the source root, not the config file's directory.**
  A `governance.toml` kept outside the code it governs (`root = "../repo"`) found
  no `tsconfig.json`, so every `@/*` alias silently failed to resolve — zero edges,
  zero violations, exit 0. Alias targets are also normalized against the source
  root, which is what importable keys are relative to. Ancestors are searched as a
  fallback for packages whose aliases live in a root `tsconfig.base.json`.
- **Dynamic `import()` produces edges.** `await import("./heavy")` was invisible,
  which made lazily-loaded modules look unreferenced. Type-position forms
  (`typeof import("x")`, `const x: import("x").T`) are recorded as `type_only`, so
  they never count toward runtime cycles.
- **Test-file detection covers `.mjs`/`.cjs`/`.mts`/`.cts`.** Patterns only listed
  `.ts/.tsx/.js/.jsx`, so `app.config.test.mjs` and similar suites were scanned as
  production source.

- **File-level cycle detection (`no_file_cycles`).** New rule that detects
  circular imports between individual files — the madge `--circular` equivalent.
  Module-level cycle detection is structurally blind to cycles inside a single
  module (and `--depth` only tunes directory granularity); this rule closes that
  gap. Resolves imports to concrete files (including Python package `__init__.py`
  and TypeScript `index.*` barrels), reports one violation per strongly connected
  component with a representative shortest cycle and per-hop evidence, and is
  fully deterministic. Opt-in via `[rules] no_file_cycles = true` in
  governance.toml (existing setups unaffected); enabled by default in `--auto`
  scans. The file graph is only built when the rule is on — no extra resolution
  cost otherwise.

## 0.5.0

### Accuracy (fewer false negatives / false positives)

Validated against real codebases: flask, requests, httpx, pydantic, fastapi,
django, scikit-learn, and PostHog (Python + TypeScript frontend).

- **Absolute self-imports now resolve in `--auto`.** `from myapp.models import X`
  was treated as third-party and dropped, losing the majority of edges on real
  projects (PostHog reported 1 cycle among 1189 modules). `--auto` now derives the
  package name from the source root, recovering those edges.
- **Top-level module granularity by default.** Per-directory discovery exploded
  module counts (PostHog 1189, Django 190) and produced spurious parent/child
  containment cycles. `--auto` now uses top-level packages (PostHog 41, Django 17),
  a clean partition with no nesting cycles. `--depth N` controls granularity
  (`0` = unlimited, the old behavior).
- **Underscore packages no longer dropped.** Single-underscore dirs/files
  (`_internal`, `_transports`, httpx's all-underscore layout) are kept; only
  dunder and hidden directories are skipped.
- **Bare and root-level relative imports resolve.** `from . import submodule`,
  `from pkg import submodule`, and upward `from ..pkg import X` from a package
  `__init__` are now handled, with submodule-vs-symbol discrimination.
- **TypeScript zero-config resolution.** Bare specifiers (`scenes/urls`) and
  `~/` / `@/` src-root aliases resolve relative to the source root even when no
  `tsconfig.json` is found (PostHog frontend: 1 → 7 real cycles, ~17.5k edges).
- **Deterministic output.** Cycle detection no longer depends on set/hash or
  filesystem ordering; results are identical across runs and machines (previously
  12–19 cycles across runs of the same repo). Source files are scanned in sorted
  order and prefix matches resolve to the closest importable.
- **Cycles reported per strongly connected component.** Instead of enumerating
  the (exponentially many) elementary cycles in a dense cluster, each entangled
  group of modules is reported once with a representative loop. No FN (every
  cyclic module is surfaced), no flood, fully deterministic.

### New rules

- `can_only_depend_on` — per-module allowlist (complements the `cannot_depend_on`
  blacklist; closes the "new module is implicitly allowed everywhere" hole).
- `independence` — groups of modules that must not import each other (direct +
  transitive).
- `must_not_reach` — reachability contracts (a source must not transitively reach
  a target; glob targets supported).
- `no_orphans` — flag modules with no incoming or outgoing dependencies.
- Glob matching in `cannot_depend_on` / `must_not_reach` targets (`"tests_*"`).
- Per-rule severity overrides (`[rules.severity]`: `error|warning|info|off`). Only
  error-severity violations fail the run; warnings and info are advisory.
