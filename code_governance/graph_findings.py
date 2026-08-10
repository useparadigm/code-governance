"""Turn a graph payload into a findings report.

Cycles are computed on *value* edges only: `import type` / `if TYPE_CHECKING:`
is erased before the program runs and cannot close an import loop, which is the
same rule :mod:`code_governance.dep_graph` applies to `no_file_cycles`. Coupling,
fan-in, fan-out and the orphan scan count every import, because a type-only
import is still a real dependency and still keeps its target alive.

The folder-cycle section is the one thing module-level rules cannot see: it looks
for cycles between sibling directories at *every* nesting depth, not just the
module granularity a config declares.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, Optional

_TOP_N = 15
_MAX_ORPHANS = 20


class Graph:
    """Adjacency views over a payload, built once and shared by every section."""

    def __init__(self, payload: dict) -> None:
        self.files: list[str] = payload["files"]
        self.parts = [f.split("/") for f in self.files]
        self.n = len(self.files)
        self.symbols: list[int] = payload["symbols"]
        self.lines: list[int] = payload["lines"]
        self.projects: list[str] = payload.get("projects", ["." for _ in self.files])
        self.entries = set(payload.get("entries", []))
        self.test_refs = set(payload.get("test_refs", []))
        self.stats = payload.get("stats", {})

        self.out: dict[int, list[int]] = defaultdict(list)
        self.inn: dict[int, list[int]] = defaultdict(list)
        self.weight: dict[tuple[int, int], int] = {}
        # value_* excludes type-only sites — see module docstring
        self.value_out: dict[int, list[int]] = defaultdict(list)
        self.value_weight: dict[tuple[int, int], int] = {}
        for s, t, count, _lines, type_only, *_ in payload["edges"]:
            self.out[s].append(t)
            self.inn[t].append(s)
            self.weight[(s, t)] = count
            if count - type_only > 0:
                self.value_out[s].append(t)
                self.value_weight[(s, t)] = count - type_only

    def fan_in(self, i: int) -> int:
        return sum(self.weight[(s, i)] for s in self.inn.get(i, ()))

    def fan_out(self, i: int) -> int:
        return sum(self.weight[(i, t)] for t in self.out.get(i, ()))


def strongly_connected(nodes: Iterable[int], adj: dict) -> list[list[int]]:
    """Iterative Tarjan — components with more than one member, i.e. cycles.

    Iterative because a deep import chain would blow the recursion limit on a
    large repo.
    """
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    onstack: set[int] = set()
    stack: list[int] = []
    out: list[list[int]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, pi = work[-1]
            if pi == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                onstack.add(v)
            recursed = False
            neighbours = adj.get(v, ())
            while pi < len(neighbours):
                w = neighbours[pi]
                pi += 1
                work[-1] = (v, pi)
                if w not in index:
                    work.append((w, 0))
                    recursed = True
                    break
                if w in onstack:
                    low[v] = min(low[v], index[w])
            else:
                work[-1] = (v, pi)
            if recursed:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def file_cycles(g: Graph, *, value_only: bool = True) -> list[list[int]]:
    source = g.value_out if value_only else g.out
    adj = {s: sorted(set(ts)) for s, ts in source.items()}
    return sorted(strongly_connected(range(g.n), adj), key=len, reverse=True)


def folder_cycles(g: Graph, weights: dict) -> list[tuple[str, list[str], int]]:
    """Cycles between sibling folders, at every directory depth.

    A repo can be cycle-free at module granularity and still have `a/x` and `a/y`
    importing each other. Each directory is grouped by its immediate children and
    checked on its own, so a cycle three levels down is not hidden by the shape of
    the level above it.
    """
    dirs = {()}
    for p in g.parts:
        for k in range(len(p) - 1):
            dirs.add(tuple(p[: k + 1]))

    found: list[tuple[str, list[str], int]] = []
    for d in sorted(dirs, key=len):
        own: dict[int, str] = {}
        for i, p in enumerate(g.parts):
            if len(p) > len(d) and tuple(p[: len(d)]) == d:
                own[i] = p[len(d)]
        names = sorted(set(own.values()))
        if len(names) < 2:
            continue
        adj: dict[str, set[str]] = defaultdict(set)
        weight: Counter = Counter()
        for (s, t), count in weights.items():
            a, b = own.get(s), own.get(t)
            if a and b and a != b:
                adj[a].add(b)
                weight[(a, b)] += count
        for comp in strongly_connected(names, {k: sorted(v) for k, v in adj.items()}):
            members = set(comp)
            inner = sum(v for (a, b), v in weight.items() if a in members and b in members)
            found.append(("/".join(d), sorted(comp), inner))
    return sorted(found, key=lambda x: -len(x[1]))


def orphans(g: Graph) -> tuple[list[int], list[int]]:
    """(dead, test_only) — files nothing in production imports.

    Framework entry points are excluded: they are executed by a router or a build
    tool, never imported, so counting them would bury the real findings. Test
    files are scanned separately, so a helper only tests use is reported as such
    rather than as dead code.
    """
    dead, test_only = [], []
    for i in range(g.n):
        if g.inn.get(i) or i in g.entries:
            continue
        (test_only if i in g.test_refs else dead).append(i)
    key = lambda i: -g.symbols[i]
    return sorted(dead, key=key), sorted(test_only, key=key)


def project_coupling(g: Graph) -> list[tuple[str, int, int, float, int]]:
    """(project, Ce, Ca, instability, internal edges) — Martin's metric."""
    ce: Counter = Counter()
    ca: Counter = Counter()
    internal: Counter = Counter()
    for (s, t), count in g.weight.items():
        a, b = g.projects[s], g.projects[t]
        if a == b:
            internal[a] += count
        else:
            ce[a] += count
            ca[b] += count
    rows = []
    for m in sorted(set(g.projects)):
        total = ce[m] + ca[m]
        rows.append((m, ce[m], ca[m], (ce[m] / total) if total else 0.0, internal[m]))
    return sorted(rows, key=lambda r: -(r[1] + r[2]))


def longest_chain(g: Graph) -> tuple[int, list[int]]:
    """Longest path through the file graph, condensed over its cycles."""
    comp_of: dict[int, int] = {}
    for cid, comp in enumerate(file_cycles(g, value_only=False)):
        for m in comp:
            comp_of[m] = cid

    memo: dict[int, tuple[int, list[int]]] = {}

    def walk(v: int, seen: set[int]) -> tuple[int, list[int]]:
        if v in memo:
            return memo[v]
        if v in seen:
            return 0, [v]
        seen.add(v)
        best, best_path = 0, [v]
        for w in sorted(set(g.out.get(v, ()))):
            if comp_of.get(v) is not None and comp_of.get(v) == comp_of.get(w):
                continue
            d, path = walk(w, seen)
            if d + 1 > best:
                best, best_path = d + 1, [v] + path
        seen.discard(v)
        memo[v] = (best, best_path)
        return memo[v]

    best = (0, [])
    for v in range(g.n):
        d, path = walk(v, set())
        if d > best[0]:
            best = (d, path)
    return best


def render_markdown(payload: dict, *, viewer: Optional[str] = "graph.html", title: str = "") -> str:
    """The findings report. ``viewer`` is the filename the deep links point at;
    pass None for a report that stands alone."""
    g = Graph(payload)
    lines: list[str] = []
    add = lines.append

    def link(i: int) -> str:
        name = g.files[i]
        return f"[{name}]({viewer}#file:{name})" if viewer else f"`{name}`"

    def level_link(path: str, label: str) -> str:
        return f"[{label}]({viewer}#{path})" if viewer else label

    fcycles = file_cycles(g)
    value_members = {i for c in fcycles for i in c}
    fcycles_type_only = [c for c in file_cycles(g, value_only=False) if not (set(c) & value_members)]

    lcycles = folder_cycles(g, g.value_weight)
    value_at: dict[str, set[str]] = defaultdict(set)
    for path, members, _ in lcycles:
        value_at[path] |= set(members)
    lcycles_type_only = [
        (path, members, extra)
        for path, members, _ in folder_cycles(g, g.weight)
        if (extra := sorted(set(members) - value_at[path]))
    ]

    dead, test_only = orphans(g)
    depth, chain = longest_chain(g)

    add(f"# {title or payload.get('root', 'Dependency')} — dependency findings\n")
    add(f"{g.n} source files · {len(payload['edges'])} file→file edges · "
        f"{sum(g.weight.values())} import sites · {sum(g.lines):,} lines\n")
    sites = g.stats.get("import_sites", 0)
    resolved = g.stats.get("resolved_sites", 0)
    if sites:
        add(f"{100 * resolved / sites:.0f}% of {sites} import sites resolved to a file in "
            "this tree; the rest are third-party. A number far below that of a "
            "comparable repo means aliases are not being read, and every count below "
            "is an undercount.\n")
    if viewer:
        add(f"Links open `{viewer}` at that file or level.\n")

    add("\n## 1. Import cycles between files\n")
    add("Type-only imports are excluded — they are erased before the program runs and "
        "cannot close a loop.\n")
    if not fcycles:
        add("None. Every import chain that survives compilation terminates.\n")
    else:
        for c in fcycles:
            add(f"- {len(c)} files: " + ", ".join(f"`{g.files[i]}`" for i in c))
    if fcycles_type_only:
        add(f"\nAnother {len(fcycles_type_only)} loop(s) exist only if type-only imports are "
            "counted — harmless, listed for completeness:\n")
        for c in fcycles_type_only:
            add(f"- {len(c)} files: " + ", ".join(f"`{g.files[i]}`" for i in c))
    add("")

    add("\n## 2. Cycles between folders\n")
    add("Checked at every directory depth, so a cycle between sibling subfolders shows up "
        "even when the modules a config declares are clean.\n")
    if not lcycles:
        add("None.\n")
    for path, members, weight in lcycles:
        add(f"- **{path or 'repo'}** — {', '.join(members)}  ({weight} import sites) "
            + level_link(path, "open"))
    if lcycles_type_only:
        add("\n### 2a. Type-only — not cycles at runtime\n")
        add("Present only because type-only imports were counted. Nothing to fix.\n")
        for path, members, extra in lcycles_type_only:
            why = ("the whole loop is type-only" if extra == members
                   else f"joins {', '.join(members)} by type import alone")
            add(f"- **{path or 'repo'}** — {', '.join(extra)} ({why}) " + level_link(path, "open"))
    add("")

    add("\n## 3. Most depended-on files\n")
    add("| file | importers | import sites | symbols | lines |")
    add("|---|---|---|---|---|")
    for i in sorted(range(g.n), key=lambda i: -g.fan_in(i))[:_TOP_N]:
        add(f"| {link(i)} | {len(set(g.inn.get(i, [])))} | {g.fan_in(i)} | {g.symbols[i]} | {g.lines[i]} |")

    add("\n## 4. Files with the most dependencies\n")
    add("| file | imports | import sites | lines |")
    add("|---|---|---|---|")
    for i in sorted(range(g.n), key=lambda i: -g.fan_out(i))[:_TOP_N]:
        add(f"| {link(i)} | {len(set(g.out.get(i, [])))} | {g.fan_out(i)} | {g.lines[i]} |")

    add("\n## 5. Biggest files\n")
    add("| file | lines | symbols | importers |")
    add("|---|---|---|---|")
    for i in sorted(range(g.n), key=lambda i: -g.lines[i])[:_TOP_N]:
        add(f"| {link(i)} | {g.lines[i]} | {g.symbols[i]} | {len(set(g.inn.get(i, [])))} |")

    presets = ", ".join(g.stats.get("entry_presets", [])) or "none"
    add("\n## 6. Files nothing in production imports\n")
    add(f"Entry points excluded (detected conventions: {presets}) — they are run by a "
        "framework or build tool, not imported. Dynamic `import()` counts as a real "
        f"reference, and {g.stats.get('test_files', 0)} test files were scanned separately so "
        "test-only helpers are not mistaken for dead code.\n")
    add(f"\n### 6a. Dead — not referenced by production or tests ({len(dead)})\n")
    if not dead:
        add("None.\n")
    else:
        add("| file | symbols | lines |")
        add("|---|---|---|")
        for i in dead[:_MAX_ORPHANS]:
            add(f"| {link(i)} | {g.symbols[i]} | {g.lines[i]} |")
        if len(dead) > _MAX_ORPHANS:
            add(f"\n…and {len(dead) - _MAX_ORPHANS} more.\n")
    add(f"\n### 6b. Test-only — production never imports them ({len(test_only)})\n")
    if not test_only:
        add("None.\n")
    else:
        add("| file | symbols | lines |")
        add("|---|---|---|")
        for i in test_only[:_TOP_N]:
            add(f"| {link(i)} | {g.symbols[i]} | {g.lines[i]} |")
        if len(test_only) > _TOP_N:
            add(f"\n…and {len(test_only) - _TOP_N} more.\n")

    add("\n## 7. Coupling per project\n")
    add("Ce = imports going out, Ca = imports coming in, I = Ce/(Ce+Ca). I near 1 = depends on "
        "everything (volatile); I near 0 = depended on by everything (must stay stable).\n")
    add("| project | Ce out | Ca in | I | internal edges |")
    add("|---|---|---|---|---|")
    for m, ce, ca, inst, internal in project_coupling(g):
        if ce + ca + internal == 0:
            continue
        label = level_link(m, m) if viewer else m
        add(f"| {label} | {ce} | {ca} | {inst:.2f} | {internal} |")

    add(f"\n## 8. Longest dependency chain — {depth} hops\n")
    for step, i in enumerate(chain):
        add(f"{step + 1}. {link(i)}")

    return "\n".join(lines) + "\n"


def summary_line(payload: dict) -> str:
    """One-line counts, for the CLI to print after writing files."""
    g = Graph(payload)
    fcycles = file_cycles(g)
    lcycles = folder_cycles(g, g.value_weight)
    dead, test_only = orphans(g)
    # The resolve rate is the report's own smoke alarm: a misconfigured alias or
    # an unread tsconfig shows up here as a collapsed percentage long before
    # anyone notices that a central file claims five importers.
    sites = g.stats.get("import_sites", 0)
    resolved = g.stats.get("resolved_sites", 0)
    rate = f" resolved={100 * resolved / sites:.0f}%-of-{sites}-sites" if sites else ""
    return (f"files={g.n} edges={len(payload['edges'])} "
            f"file-cycles={len(fcycles)} folder-cycles={len(lcycles)} "
            f"dead={len(dead)} test-only={len(test_only)}{rate}")
