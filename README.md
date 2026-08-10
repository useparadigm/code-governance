<p align="center">
  <img src="code-governance.jpg" alt="code-governance" width="200">
</p>

<h1 align="center">code-governance</h1>

<p align="center"><b>Stop spaghetti imports. Enforce module boundaries in Python and TypeScript.</b></p>

<p align="center">
  <a href="https://pypi.org/project/code-governance/"><img src="https://img.shields.io/pypi/v/code-governance?style=flat&color=orange" alt="PyPI"></a>
  <a href="https://github.com/useparadigm/code-governance/actions"><img src="https://img.shields.io/github/actions/workflow/status/useparadigm/code-governance/tests.yml?style=flat&label=tests" alt="Tests"></a>
  <a href="https://github.com/useparadigm/code-governance/blob/main/LICENSE"><img src="https://img.shields.io/github/license/useparadigm/code-governance?style=flat" alt="License"></a>
</p>

---

## Before / After

| Without governance | With governance |
|---|---|
| `api/` imports from `billing/`, `db/`, `auth/`, `utils/`, `migrations/`... | `api/` has `cannot_depend_on = ["billing", "migrations"]` — forbidden imports blocked |
| One refactor breaks 14 files across 6 modules | Boundaries are explicit, changes stay local |
| New dev: "Can I import this here?" "Uhh... maybe?" | Config says yes or no. CI enforces it. |

## Setup

### Option 1: With Claude Code (recommended)

Install the plugin — Claude handles everything interactively. **Run each command separately** — pasting them all at once will concatenate into a single broken URL:

```
/plugin marketplace add useparadigm/code-governance-plugin
```

```
/plugin install code-governance
```

```
/reload-plugins
```

Then run:

```
/governance-init
```

> Without `/reload-plugins`, the new commands won't be registered yet and `/governance-init` will fail with `Unknown command`.

**What happens:**

1. Claude installs `code-governance` in your project
2. Scans your codebase — discovers modules, maps every import
3. Shows you the dependency map: *"I found 8 modules. `api` imports from `db` directly — should it?"*
4. You discuss architecture: which dependencies are intentional, which are spaghetti
5. Claude creates `governance.toml` based on your answers
6. Enables rules: `no_cycles`, `enforce_cannot_depend_on`, optionally `enforce_layers`
7. If existing violations: *"Want a baseline? I'll accept these and only fail on new ones."*
8. Sets up GitHub Actions — CI check on every PR + `/governance fix` command
9. Done. Every PR is now gated on architecture rules.

**After setup, two more commands available:**

| Command | What it does |
|---------|-------------|
| `/governance-check` | Run checks, explain violations, fix them interactively |
| `/governance-audit` | Architecture health review — metrics, hotspots, suggestions |

### Option 2: Manual

```bash
pip install code-governance
```

**Quick scan** — no config needed, instant results:

```bash
$ governance-ast --auto src/

Governance Report (python)
Modules: 8 | Files scanned: 47

Violations (1):
  [E] [no_cycles] Circular dependency: payments -> notifications -> payments

FAILED (1 error)
```

`--auto` discovers **top-level packages** as modules and resolves absolute
self-imports (`from myapp.models import X`), relative imports (`from . import sub`,
`from ..pkg import Y`), and underscore-prefixed packages (`_internal`). Use
`--depth N` for finer granularity (`--depth 2` splits two levels deep, `--depth 0`
makes every directory its own module).

`--auto` also checks **file-level cycles** (`no_file_cycles`) — circular imports
between individual files, the madge `--circular` equivalent. These are detected
independently of module granularity, so a cycle between two files inside the same
package is caught even at `--depth 1`.

Works on TypeScript too — language auto-detected from source:

```bash
$ governance-ast --auto src/

Governance Report (typescript)
Modules: 6 | Files scanned: 82
...
```

`.ts`, `.tsx`, `.js`, `.jsx`, `.mts`, `.cts`, `.mjs`, `.cjs` all supported.
`tsconfig.json` path aliases (`@/*`, `extends` chains) are honored, and bare
specifiers (`scenes/urls`) plus `~/` / `@/` src-root aliases resolve even without
a `tsconfig.json` in zero-config scans.

**Full setup** — generate config, review, enforce:

```bash
# Generate config: detect modules + seed cannot_depend_on by locking down
# every module pair that does not currently import from each other.
governance-ast --generate --source-root src/

# See the dependency map
governance-ast --discover

# Edit governance.toml — remove dependencies you don't want, add layers
# Then enforce:
governance-ast
```

**Set up CI** — add to `.github/workflows/governance.yml`:

```yaml
- uses: actions/checkout@v4
- name: Governance
  uses: useparadigm/code-governance@main
  with:
    config: governance.toml
    diff: origin/main
```

See [governance-fix workflow](#governance-fix-workflow) to enable the `/governance fix` PR command.

**GitLab CI** — add to `.gitlab-ci.yml`:

```yaml
governance:
  image: python:3.12-slim
  variables:
    GIT_DEPTH: 0
  before_script:
    - pip install code-governance
  script:
    - governance-ast --config governance.toml --diff origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

See [GitLab MR comments](#gitlab-mr-comments) to post the report as an MR note.

## What it catches

| Rule | Example |
|------|---------|
| `no_cycles` | `payments` -> `notifications` -> `payments` |
| `no_file_cycles` | `payments/stripe.py` -> `payments/retry.py` -> `payments/stripe.py` — file-level import loops (madge `--circular` equivalent), even inside one module |
| `enforce_cannot_depend_on` | `api` imports `billing` but `billing` is in `cannot_depend_on` (supports globs: `"tests_*"`) |
| `can_only_depend_on` | `api` declares an allowlist; importing anything outside it fails (safe by default — new modules aren't silently allowed) |
| `enforce_layers` | `db` (infrastructure) imports from `api` (presentation) |
| `independence` | `billing` and `shipping` declared independent, but `billing` imports `shipping` |
| `must_not_reach` | `web` transitively reaches `secrets` through a dependency chain |
| `no_orphans` | `legacy` module has no incoming or outgoing dependencies (dead code) |
| `max_public_surface` | 80% of `core`'s symbols used externally — too exposed |
| `min_cohesion` | `utils` imports 90% from other modules — grab-bag module |

### Rules reference

```toml
[[modules]]
name = "api"
path = "api/"
cannot_depend_on = ["billing", "tests_*"]   # blacklist (exact names or globs)
# can_only_depend_on = ["core", "db"]        # allowlist (alternative to blacklist)

[rules]
no_cycles = true
no_file_cycles = false                        # file-level import loops (on by default in --auto)
enforce_cannot_depend_on = true
no_orphans = false                            # warn on dead modules
independence = [["billing", "shipping"]]      # mutually-independent module groups
transitive = false                            # also check indirect dependency chains
exclude_from_cycles = []
exclude_from_orphans = ["__main__"]

[[rules.must_not_reach]]                       # reachability contracts
source = ["web"]
target = ["secrets", "internal_*"]

[rules.severity]                               # override per-rule severity
no_orphans = "warning"                         # error | warning | info | off
min_cohesion = "off"
```

Only **error**-severity violations fail the run (non-zero exit); warnings and info
are advisory. Cycle detection and all rule output are **deterministic** — identical
across runs and machines, so CI and agents get reproducible results.

## What happens in CI

Every PR gets a governance comment:

```
❌ Governance — 1 violation, 1 new module

🔴 🔗 core: Forbidden dependency: 'core' imports 'db' (cannot_depend_on: ['db'])
  core/service.py:2

📦 New module exporters (exporters/) — not in governance.toml

Reply /governance fix to apply.

🤖 AI: The core→db import creates tight coupling. Extract shared
types into a common module, or remove "db" from core's cannot_depend_on.
```

**New module detected?** Reply `/governance fix` — bot adds it to config, commits to your branch.

**Violation?** Fix the import or update `governance.toml`. Use `/governance-check` in Claude Code for interactive help.

### Pre-commit

```yaml
repos:
  - repo: https://github.com/useparadigm/code-governance
    rev: main
    hooks:
      - id: governance-check
      - id: governance-diff
```

### Adopting on legacy codebases

```bash
governance-ast --save-baseline .governance-baseline.json
governance-ast --baseline .governance-baseline.json
```

Accept existing violations. Only fail on new ones.

### Transitive detection

```bash
governance-ast --transitive
```

Detects indirect violations through dependency chains. If `api` has `cannot_depend_on = ["db"]` and `api → service → db`, the transitive check catches it. Also works with layer enforcement. Enable permanently in config with `transitive = true` under `[rules]`.

## LLM advice

```bash
export OPENAI_API_KEY=sk-...   # or ANTHROPIC_API_KEY
governance-ast --advise
```

Analyzes violations and suggests: accept the dependency, restructure the code, or extract a shared module. Works with OpenAI and Anthropic.

## HTML report

```bash
governance-ast --format html > report.html
```

Self-contained dependency matrix with module metrics. Drop any governance JSON into it.

## Dependency drill-down

```bash
governance-ast --graph .                        # findings report on stdout
governance-ast --graph . --graph-out .reports   # findings.md + graph.html
governance-ast --graph . --format json          # the raw payload
```

Zero-config, no `governance.toml`. The rules answer *does this repo break its
contract*; this answers *what does this repo actually look like* — every file,
every resolved import, explorable at any directory depth.

`findings.md` reports:

- **file-level import cycles**, judged on runtime edges — `import type` and
  `if TYPE_CHECKING:` are erased before the program runs and are listed apart
- **folder cycles at every nesting depth** — a repo can be clean at the module
  granularity its config declares and still have `lib/a` and `lib/b` importing
  each other three levels down
- fan-in, fan-out, biggest files, longest dependency chain
- **dead code vs test-only helpers** — test files are scanned separately, so a
  helper only the suite imports is reported as such rather than as dead
- coupling per workspace package (Ce / Ca / instability)

`graph.html` is a self-contained viewer: drill into any folder, click a file to
read its source with import lines linked, trace a symbol to every call site
through the barrels that re-export it.

Boxes are draggable — no automatic layout untangles every graph, so move them and
the edges re-route live (**reset layout** puts them back). Hovering an arrow says
what it carries: import-site count, how many are type-only, and each imported name
with what it is (function, class, type, component, hook, re-export), read off the
declaration in the file that exports it. Clicking an arrow lists every site. Arrows
are one size and one width regardless of weight — the count belongs in the label,
not in the line thickness — and **edge labels** draws it on the line.

Click the **cycles** count in the header to focus the loops at that level: the
graph dims everything outside them and numbers the ring in order, and the side
panel names the modules in sequence plus the concrete `file:line` imports that
close it — click one to land on the import statement to delete. Where the loop
is between folders and no single file ring exists, it shows the one import per
hop that closes each side instead. Esc leaves the mode.

Entry points — framework routes, `package.json` `exports` targets, configs —
are detected from the markers actually present (Next.js, Expo, Vite, Python
packaging) so they are not reported as dead code. Extend with `--entry`:

```bash
governance-ast --graph . --entry 'generated/**' --entry '**/fixtures/*'
```

Pass `--no-sources` to drop the embedded source text — much smaller output, no
code view.

## Comparison

| | code-governance | tach | import-linter |
|---|---|---|---|
| **Zero-config scan** | `--auto` — instant | No | No |
| **Config generation** | `--generate` from source | `tach init` (interactive TUI) | Manual |
| **LLM advice** | `--advise` — explains + suggests fix | No | No |
| **CI comments** | PR comments with explanations | No | No |
| **Auto-fix from PR** | `/governance fix` | No | No |
| **AI agent plugin** | Claude Code plugin | No | No |
| **Diff mode** | `--diff HEAD` | No | No |
| **Baseline** | Accept existing, fail on new | No | No |
| **Module metrics** | Cohesion, surface, symbols | No | No |
| **HTML report** | Dependency matrix viewer | `tach show` | Browser UI |
| **JSON output** | Yes | Yes | No |
| **Scans source directly** | Yes | Yes | No — must be importable |
| **Interface enforcement** | No | Yes | No |
| **Transitive imports** | Full chain (`--transitive`) | Direct only | Full chain |
| **Forbidden imports** | `cannot_depend_on` | `cannot_depend_on` | Forbidden contract |
| **Visibility control** | No | Yes | No |
| **Speed (Django)** | ~1.2s | Sub-second (Rust) | ~0.1s (grimp) |
| **Config syntax** | Simple TOML lists | TOML with regex | INI or TOML |

**Choose tach** if you need interface enforcement or visibility control.
**Choose code-governance** if you want transitive detection, AI-guided setup, CI integration, or zero-config scanning.

### Known limitations

- **Bare imports that shadow the standard library resolve to the stdlib.** A bare
  `import json` / `from logging import x` is treated as the standard library even
  if a local package shares that name, since that is how Python 3 resolves it in
  the common case and avoids flooding the report with false edges (e.g. every
  `import logging` linking to a local `logging/` package). Imports of the local
  package via a relative (`from .json import x`) or package-prefixed
  (`from myapp.json import x`) form still resolve normally — only the bare form is
  treated as external.
- **Dynamic imports are not tracked.** `importlib.import_module("a.b")` and
  `__import__` use runtime strings the static analyzer cannot follow.

---

<details>
<summary><h3>Governance fix workflow</h3></summary>

Add to `.github/workflows/governance-fix.yml`:

```yaml
name: Governance Fix

on:
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write

jobs:
  apply-fix:
    name: Apply config fix
    runs-on: ubuntu-latest
    if: |
      github.event.issue.pull_request &&
      contains(github.event.comment.body, '/governance fix')
    steps:
      - name: Acknowledge
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh api repos/${{ github.repository }}/issues/comments/${{ github.event.comment.id }}/reactions \
            -X POST -f content=eyes

      - uses: actions/checkout@v4

      - name: Checkout PR branch
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh pr checkout ${{ github.event.issue.number }}

      - name: Find and apply fix
        id: fix
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          PAYLOAD=$(gh api repos/${{ github.repository }}/issues/${{ github.event.issue.number }}/comments \
            --jq '.[] | select(.body | contains("<!-- governance-fix:")) | .body' \
            | grep -oP '<!-- governance-fix:\K[A-Za-z0-9+/=]+' | tail -1)

          if [ -z "$PAYLOAD" ]; then
            gh api repos/${{ github.repository }}/issues/comments/${{ github.event.comment.id }}/reactions \
              -X POST -f content=confused
            exit 1
          fi

          DECODED=$(echo "$PAYLOAD" | base64 -d)
          CONFIG_PATH=$(echo "$DECODED" | python3 -c "import sys,json; print(json.load(sys.stdin)['config_path'])")
          echo "$DECODED" | python3 -c "import sys,json; print(json.load(sys.stdin)['updated_config'])" > "$CONFIG_PATH"

          pip install code-governance
          echo "Config updated"

          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add "$CONFIG_PATH"
          git diff --cached --quiet && exit 0

          git commit -m "governance: add new modules to config"
          git push
          echo "sha=$(git rev-parse --short HEAD)" >> $GITHUB_OUTPUT

      - name: Confirm
        if: steps.fix.outputs.sha
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh api repos/${{ github.repository }}/issues/comments/${{ github.event.comment.id }}/reactions \
            -X POST -f content='+1'
          gh pr comment ${{ github.event.issue.number }} \
            --body "✅ Config fix applied in \`${{ steps.fix.outputs.sha }}\`. Re-run checks to verify."
```

</details>

<details>
<summary><h3>GitLab MR comments</h3></summary>

Posts the governance report as a note on the merge request. Requires a Project Access Token with `api` scope, exposed as masked CI variable `GOVERNANCE_GITLAB_TOKEN`.

```yaml
governance:
  image: python:3.12-slim
  variables:
    GIT_DEPTH: 0
  before_script:
    - pip install code-governance
    - apt-get update && apt-get install -y --no-install-recommends curl jq && rm -rf /var/lib/apt/lists/*
  script:
    - set +e
    - governance-ci-report --config governance.toml --json > report.json
    - set -e
    - PASSED=$(jq -r '.passed' report.json)
    - jq -r '.markdown' report.json | tee report.md
    - |
      if [ "$CI_PIPELINE_SOURCE" = "merge_request_event" ] && [ -n "$GOVERNANCE_GITLAB_TOKEN" ]; then
        BODY=$(jq -Rs . < report.md)
        curl -sf --request POST \
          --header "PRIVATE-TOKEN: $GOVERNANCE_GITLAB_TOKEN" \
          --header "Content-Type: application/json" \
          --data "{\"body\": $BODY}" \
          "$CI_API_V4_URL/projects/$CI_PROJECT_ID/merge_requests/$CI_MERGE_REQUEST_IID/notes"
      fi
    - test "$PASSED" = "true"
  artifacts:
    when: always
    paths: [report.json, report.md]
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
```

`/governance fix` is GitHub-only for now — GitLab equivalent would need a Note webhook listener.

</details>

## Updating

There are two pieces to update independently:

**CLI (PyPI package):**

```bash
pip install --upgrade code-governance
```

Check the installed version against the flags — if you don't see `--language {auto,python,typescript}` in `governance-ast --help`, you're on a pre-0.4 CLI and the TypeScript feature won't work.

**Claude Code plugin (skills):**

```
/plugin marketplace update useparadigm-code-governance-plugin
```

```
/reload-plugins
```

Or enable auto-update once: `/plugin` → **Marketplaces** → toggle *Enable auto-update*. Then every session picks up the latest skills automatically.

## License

MIT
