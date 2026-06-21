# Changelog

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
- **Deterministic output.** Cycle detection no longer depends on set/hash
  ordering; results are identical across runs and machines (previously 12–19
  cycles across runs of the same repo). Cycles are canonicalized to start at
  their smallest member.

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
