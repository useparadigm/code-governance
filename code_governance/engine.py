from __future__ import annotations

from pathlib import Path

from code_governance.config import load_config
from code_governance.dep_graph import build_dependency_graph
from code_governance.extractor import extract_directory
from code_governance.languages import get_patterns
from code_governance.rules import compute_module_metrics, run_all_rules
from code_governance.schemas import (
    DependencyTarget,
    DiscoverReport,
    GovernanceConfig,
    GovernanceReport,
    Violation,
)


def run_governance(config_path: str | Path, *, config: GovernanceConfig | None = None) -> GovernanceReport:
    config_path = Path(config_path)
    if config is None:
        config = load_config(config_path)
    repo_root = config_path.parent
    source_root = repo_root / config.root

    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    patterns = get_patterns(config.language, repo_root=repo_root, config=config)
    extractions = extract_directory(source_root, config.language, config.rules.exclude_test_files, patterns=patterns)

    graph = build_dependency_graph(extractions, config, patterns=patterns)

    violations = run_all_rules(graph, config)

    metrics = compute_module_metrics(graph, config)

    return GovernanceReport(
        config_path=str(config_path),
        language=config.language,
        module_count=len(config.modules),
        total_files_scanned=len(extractions),
        violations=violations,
        metrics=metrics,
    )


def run_governance_diff(config_path: str | Path, git_ref: str = "HEAD", *, config: GovernanceConfig | None = None) -> GovernanceReport:
    import subprocess

    config_path = Path(config_path)
    if config is None:
        config = load_config(config_path)
    repo_root = config_path.parent
    source_root = repo_root / config.root

    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", git_ref],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")

    changed_files = set()
    root_prefix = config.root.rstrip("/") + "/"
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith(root_prefix):
            rel = line[len(root_prefix):]
            changed_files.add(rel)

    patterns = get_patterns(config.language, repo_root=repo_root, config=config)
    all_extractions = extract_directory(source_root, config.language, config.rules.exclude_test_files, patterns=patterns)

    changed_extractions = [e for e in all_extractions if e.file_path in changed_files]

    graph = build_dependency_graph(all_extractions, config, patterns=patterns)

    violations = run_all_rules(graph, config)

    changed_modules = set()
    for ext in changed_extractions:
        for mod in config.modules:
            mod_path = mod.path.rstrip("/")
            if ext.file_path == mod_path or ext.file_path.startswith(mod_path + "/"):
                changed_modules.add(mod.name)
                break

    filtered = []
    for v in violations:
        if v.module in changed_modules:
            filtered.append(v)
            continue
        if v.evidence:
            relevant = [e for e in v.evidence if e.get("source_file") in changed_files]
            if relevant:
                v = v.model_copy(update={"evidence": relevant})
                filtered.append(v)

    metrics = compute_module_metrics(graph, config)

    return GovernanceReport(
        config_path=str(config_path),
        language=config.language,
        module_count=len(config.modules),
        total_files_scanned=len(changed_extractions),
        violations=filtered,
        metrics=metrics,
    )


def discover_dependencies(config_path: str | Path) -> DiscoverReport:
    from collections import defaultdict

    config_path = Path(config_path)
    config = load_config(config_path)
    repo_root = config_path.parent
    source_root = repo_root / config.root

    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    patterns = get_patterns(config.language, repo_root=repo_root, config=config)
    extractions = extract_directory(source_root, config.language, config.rules.exclude_test_files, patterns=patterns)
    graph = build_dependency_graph(extractions, config, patterns=patterns)
    metrics = compute_module_metrics(graph, config)

    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    seen: set[tuple[str, str, str, int]] = set()
    for edge in graph.edge_details:
        key = (edge.source_module, edge.target_module, edge.source_file, edge.line)
        if key in seen:
            continue
        seen.add(key)
        grouped[edge.source_module][edge.target_module].append({
            "file": edge.source_file,
            "line": edge.line,
            "raw_statement": edge.raw_statement,
        })

    dependencies: dict[str, list[DependencyTarget]] = {}
    for src_mod in sorted(grouped):
        targets = []
        for tgt_mod in sorted(grouped[src_mod], key=lambda t: -len(grouped[src_mod][t])):
            files = grouped[src_mod][tgt_mod]
            targets.append(DependencyTarget(
                target=tgt_mod,
                count=len(files),
                files=files,
            ))
        dependencies[src_mod] = targets

    return DiscoverReport(
        config_path=str(config_path),
        language=config.language,
        module_count=len(config.modules),
        total_files_scanned=len(extractions),
        dependencies=dependencies,
        metrics=metrics,
    )


def config_to_toml(config: GovernanceConfig) -> str:
    lines = [
        "[governance]",
        f'root = "{config.root}"',
        f'language = "{config.language.value}"',
        "",
    ]
    for mod in config.modules:
        lines.append("[[modules]]")
        lines.append(f'name = "{mod.name}"')
        lines.append(f'path = "{mod.path}"')
        deps = ", ".join(f'"{d}"' for d in mod.cannot_depend_on)
        lines.append(f"cannot_depend_on = [{deps}]")
        if mod.can_only_depend_on is not None:
            allow = ", ".join(f'"{d}"' for d in mod.can_only_depend_on)
            lines.append(f"can_only_depend_on = [{allow}]")
        if mod.layer:
            lines.append(f'layer = "{mod.layer}"')
        lines.append("")
    lines.append("[layers]")
    if config.layers.order:
        order = ", ".join(f'"{o}"' for o in config.layers.order)
        lines.append(f"order = [{order}]")
    else:
        lines.append("order = []")
    lines.append("")
    lines.append("[rules]")
    lines.append(f"no_cycles = {'true' if config.rules.no_cycles else 'false'}")
    lines.append(f"enforce_layers = {'true' if config.rules.enforce_layers else 'false'}")
    lines.append(f"enforce_cannot_depend_on = {'true' if config.rules.enforce_cannot_depend_on else 'false'}")
    lines.append(f"transitive = {'true' if config.rules.transitive else 'false'}")
    lines.append(f"exclude_test_files = {'true' if config.rules.exclude_test_files else 'false'}")
    if config.rules.exclude_from_cycles:
        excluded = ", ".join(f'"{e}"' for e in config.rules.exclude_from_cycles)
        lines.append(f"exclude_from_cycles = [{excluded}]")
    lines.append("")
    return "\n".join(lines) + "\n"


_SKIP_DIRS = {"node_modules", "__pycache__", ".git", "venv", ".venv"}
_LANG_EXTENSIONS = {
    "python": {".py"},
    "typescript": {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"},
}


_MIXED_WARN_RATIO = 0.2


def detect_language(source_root: str | Path, *, warn_mixed: bool = True) -> "Language":
    import sys as _sys
    from code_governance.schemas import Language

    root = Path(source_root)
    counts: dict[str, int] = {lang: 0 for lang in _LANG_EXTENSIONS}
    ext_to_lang: dict[str, str] = {}
    for lang, exts in _LANG_EXTENSIONS.items():
        for ext in exts:
            ext_to_lang[ext] = lang

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        lang = ext_to_lang.get(path.suffix)
        if lang:
            counts[lang] += 1

    best = max(counts, key=lambda k: (counts[k], -list(_LANG_EXTENSIONS).index(k)))
    if counts[best] == 0:
        return Language.PYTHON

    if warn_mixed:
        others = [(lang, n) for lang, n in counts.items() if lang != best and n > 0]
        for lang, n in others:
            if n >= _MIXED_WARN_RATIO * counts[best]:
                print(
                    f"Warning: mixed project detected — using '{best}' "
                    f"({counts[best]} files) but also found {n} {lang} files. "
                    f"Run a separate governance.toml for {lang} to cover them.",
                    file=_sys.stderr,
                )
    return Language(best)


def generate_config(
    source_root: str | Path,
    language: str = "python",
) -> GovernanceConfig:
    from code_governance.schemas import Language, ModuleConfig

    root = Path(source_root)
    if not root.exists():
        raise FileNotFoundError(f"Source root not found: {root}")

    lang = Language(language)
    extensions = _LANG_EXTENSIONS.get(language, set())
    modules: list[ModuleConfig] = []

    has_root_files = any(
        child.is_file() and child.suffix in extensions
        and not child.name.startswith("_")
        for child in root.iterdir()
    )
    if has_root_files:
        modules.append(ModuleConfig(
            name="core",
            path=".",
            cannot_depend_on=[],
        ))

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if child.name in _SKIP_DIRS:
            continue

        modules.append(ModuleConfig(
            name=child.name,
            path=child.name + "/",
            cannot_depend_on=[],
        ))

    return GovernanceConfig(
        root=".",
        language=lang,
        modules=modules,
    )


def generate_full_config(
    source_root: str | Path,
    language: str = "python",
    config_path: str | Path = "governance.toml",
    *,
    seed: bool = True,
) -> GovernanceConfig:
    source_root = Path(source_root).resolve()
    config = generate_config(source_root, language)

    # Set root relative to where governance.toml will be written
    config_dir = Path(config_path).resolve().parent
    try:
        rel_root = source_root.relative_to(config_dir)
    except ValueError:
        rel_root = source_root
    config.root = str(rel_root)

    if seed:
        _seed_cannot_depend_on(config, source_root)
    return config


def _seed_cannot_depend_on(config: GovernanceConfig, source_root: Path) -> GovernanceConfig:
    """Lock down the current state: for each module, forbid every other module it
    does not currently import. New imports that weren't observed at config
    generation time will surface as violations."""
    patterns = get_patterns(config.language, repo_root=source_root, config=config)
    extractions = extract_directory(source_root, config.language, config.rules.exclude_test_files, patterns=patterns)
    graph = build_dependency_graph(extractions, config, patterns=patterns)

    module_names = {mod.name for mod in config.modules}
    for mod in config.modules:
        observed = set(graph.module_edges.get(mod.name, {}).keys()) & module_names
        mod.cannot_depend_on = sorted(module_names - observed - {mod.name})
    return config


def populate_cannot_depend_on(config_path: str | Path) -> GovernanceConfig:
    """Re-seed cannot_depend_on on an existing config — used by --fix-deps after
    new modules or imports appear."""
    config_path = Path(config_path)
    config = load_config(config_path)
    source_root = config_path.parent / config.root

    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    return _seed_cannot_depend_on(config, source_root)


def _is_skippable_part(part: str) -> bool:
    """Directory components that should never contribute modules: build/cache dirs,
    dunder dirs (__pycache__), and hidden dirs. Single-underscore dirs like
    `_transports` or `_internal` ARE real modules and are kept."""
    return part in _SKIP_DIRS or part.startswith("__") or part.startswith(".")


def _iter_source_files(root: Path, extensions: set[str]):
    """Yield (relative_dir_parts, file) for every source file under root, skipping
    build/cache/hidden directories. Single-underscore files and dirs are kept."""
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix not in extensions:
            continue
        rel = f.relative_to(root)
        dir_parts = rel.parts[:-1]
        if any(_is_skippable_part(p) for p in dir_parts):
            continue
        yield dir_parts, f


def _discover_modules(root: Path, extensions: set[str], max_depth: int = 1) -> list:
    """Discover modules up to ``max_depth`` directory levels under ``root``.

    Each directory whose depth is <= max_depth (and which contains source files,
    directly or nested) becomes one module; files in deeper sub-directories are
    attributed to their nearest ancestor module. This produces a clean partition
    (no parent/child module overlap), so nested packages do not generate spurious
    "cycles". Loose source files at the root are grouped into a single module
    named after the root directory.

    ``max_depth <= 0`` means unlimited depth — every source directory becomes its
    own module (the original fine-grained behavior, available via --depth 0).
    """
    from code_governance.schemas import ModuleConfig

    modules: dict[str, str] = {}
    has_root_loose = False

    for dir_parts, _f in _iter_source_files(root, extensions):
        if not dir_parts:
            has_root_loose = True
            continue
        depth = len(dir_parts) if max_depth <= 0 else min(len(dir_parts), max_depth)
        mod_parts = dir_parts[:depth]
        name = ".".join(mod_parts)
        modules[name] = "/".join(mod_parts) + "/"

    result = [
        ModuleConfig(name=name, path=path, cannot_depend_on=[])
        for name, path in sorted(modules.items())
    ]

    if has_root_loose:
        taken = set(modules)
        root_name = root.name or "core"
        # Guarantee a unique name even if a child package already uses it (e.g. a
        # source root literally named `core` that also contains a `core/` subdir),
        # so two distinct directories never collapse into one module.
        if root_name in taken:
            base = "core" if root_name != "core" else "root"
            candidate = base
            i = 2
            while candidate in taken:
                candidate = f"{base}{i}"
                i += 1
            root_name = candidate
        result.insert(0, ModuleConfig(name=root_name, path=".", cannot_depend_on=[]))

    return result


def run_auto_scan(source_root: str | Path, max_depth: int = 0) -> GovernanceReport:
    """Zero-config scan: discover modules and check for cycles.

    By default every source directory is its own module (``max_depth=0``), giving
    fine-grained per-package metrics. Pass ``max_depth=1`` for top-level packages
    only, or ``N`` to split up to N levels deep. Cycle output stays clean at any
    granularity because cycles are reported per strongly connected component."""
    from code_governance.schemas import RulesConfig

    source_root = Path(source_root).resolve()
    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    language = detect_language(source_root)
    extensions = _LANG_EXTENSIONS[language.value]
    modules = _discover_modules(source_root, extensions, max_depth=max_depth)

    config = GovernanceConfig(
        root=".",
        language=language,
        # The source-root directory name is the importable package name, so absolute
        # self-imports (`from posthog.models import X`) resolve to local modules
        # instead of being dropped as third-party imports.
        package_prefix=source_root.name or None,
        modules=modules,
        rules=RulesConfig(
            no_cycles=True,
            enforce_layers=False,
            enforce_cannot_depend_on=True,
            exclude_test_files=True,
        ),
    )

    patterns = get_patterns(config.language, repo_root=source_root, config=config)
    extractions = extract_directory(source_root, config.language, config.rules.exclude_test_files, patterns=patterns)
    graph = build_dependency_graph(extractions, config, patterns=patterns)

    violations = run_all_rules(graph, config)

    metrics = compute_module_metrics(graph, config)

    return GovernanceReport(
        config_path="(auto-scan)",
        language=config.language,
        module_count=len(config.modules),
        total_files_scanned=len(extractions),
        violations=violations,
        metrics=metrics,
    )


