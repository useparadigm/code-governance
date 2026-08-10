# Changelog

## Unreleased

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
