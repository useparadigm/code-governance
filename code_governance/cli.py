from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from code_governance.config import load_config
from code_governance.engine import config_to_toml, discover_dependencies, generate_config, generate_full_config, run_auto_scan, run_governance, run_governance_diff


def main():
    parser = argparse.ArgumentParser(
        prog="governance-ast",
        description="AST-based governance engine — analyze module boundaries, dependencies, and architecture rules",
    )
    parser.add_argument(
        "--config", "-c",
        default="governance.toml",
        help="Path to governance.toml config file (default: governance.toml)",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["text", "json", "html"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--fix-config",
        action="store_true",
        help="Generate initial governance.toml from folder structure",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover actual dependencies between modules (instead of checking rules)",
    )
    parser.add_argument(
        "--save-baseline",
        metavar="PATH",
        help="Save current report as a baseline file (JSON)",
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        help="Compare against a baseline — only report new violations",
    )
    parser.add_argument(
        "--diff",
        metavar="REF",
        nargs="?",
        const="HEAD",
        help="Only check files changed since REF (default: HEAD). Implies --baseline if baseline exists.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate governance.toml with cannot_depend_on seeded by locking down current non-dependencies",
    )
    parser.add_argument(
        "--fix-deps",
        action="store_true",
        help="Re-seed cannot_depend_on in an existing governance.toml based on current imports",
    )
    parser.add_argument(
        "--source-root",
        default=".",
        help="Source root for --fix-config / --generate (default: .)",
    )
    parser.add_argument(
        "--language",
        default="auto",
        choices=["auto", "python", "typescript"],
        help="Language for --fix-config / --generate (default: auto — detect from source)",
    )
    parser.add_argument(
        "--advise",
        action="store_true",
        help="Use LLM to generate architectural advice for violations and new modules",
    )
    parser.add_argument(
        "--auto",
        metavar="PATH",
        nargs="?",
        const=".",
        help="Zero-config scan: discover modules from source and check for cycles. No governance.toml needed.",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        metavar="N",
        help="Module granularity for --auto: top-level packages (1, default), deeper nesting (2+), or unlimited per-directory (0)",
    )
    parser.add_argument(
        "--transitive",
        action="store_true",
        help="Check transitive dependencies (detects indirect violations through dependency chains)",
    )
    parser.add_argument(
        "--affected-tests",
        metavar="PATH",
        nargs="?",
        const=".",
        help="Find test files affected by git changes across project boundaries (zero-config, TypeScript)",
    )
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Git ref to diff against for --affected-tests (default: HEAD)",
    )
    parser.add_argument(
        "--nx",
        action="store_true",
        help="Output nx run commands instead of plain jest commands (for NX monorepos)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Execute the generated test commands after analysis",
    )

    args = parser.parse_args()

    if args.affected_tests is not None:
        _handle_affected_tests(args)
        return

    if args.auto is not None:
        _handle_auto(args)
        return

    if args.format == "html":
        _handle_html_output(args)
        return

    if args.fix_config:
        _handle_fix_config(args)
        return

    if args.generate:
        _handle_generate(args)
        return

    if args.fix_deps:
        _handle_fix_deps(args)
        return

    if args.discover:
        _handle_discover(args)
        return

    if args.save_baseline:
        _handle_save_baseline(args)
        return

    _handle_check(args)


def _handle_html_output(args):
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    discover_report = discover_dependencies(config_path)
    governance_report = run_governance(config_path)

    combined = {
        "config_path": str(config_path),
        "language": discover_report.language.value,
        "module_count": discover_report.module_count,
        "total_files_scanned": discover_report.total_files_scanned,
        "dependencies": {
            src: [t.model_dump() for t in targets]
            for src, targets in discover_report.dependencies.items()
        },
        "violations": [v.model_dump() for v in governance_report.violations],
        "metrics": [m.model_dump() for m in discover_report.metrics],
    }

    template_path = Path(__file__).resolve().parent / "viewer" / "index.html"
    template = template_path.read_text()

    payload = json.dumps(combined).replace("</", "<\\/")
    html = template.replace('"__REPORT_DATA__"', payload)
    print(html)


def _resolve_language(arg_language: str, source_root: str) -> str:
    if arg_language != "auto":
        return arg_language
    from code_governance.engine import detect_language
    return detect_language(source_root).value


def _handle_fix_config(args):
    out_path = Path(args.config)
    if out_path.exists():
        print(f"Config file already exists: {out_path}", file=sys.stderr)
        sys.exit(1)

    language = _resolve_language(args.language, args.source_root)
    config = generate_full_config(args.source_root, language, args.config, seed=False)
    toml_str = config_to_toml(config)
    out_path.write_text(toml_str)
    print(f"Generated {out_path} with {len(config.modules)} modules ({language})")


def _handle_generate(args):
    out_path = Path(args.config)
    if out_path.exists():
        print(f"Config file already exists: {out_path}", file=sys.stderr)
        sys.exit(1)

    language = _resolve_language(args.language, args.source_root)
    config = generate_full_config(args.source_root, language, args.config, seed=True)
    toml_str = config_to_toml(config)
    out_path.write_text(toml_str)

    forbidden_total = sum(len(m.cannot_depend_on) for m in config.modules)
    print(f"Generated {out_path} with {len(config.modules)} modules ({language}), {forbidden_total} forbidden edges seeded")


def _handle_fix_deps(args):
    from code_governance.engine import populate_cannot_depend_on

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = populate_cannot_depend_on(config_path)
    config_path.write_text(config_to_toml(config))

    forbidden_total = sum(len(m.cannot_depend_on) for m in config.modules)
    print(f"Updated {config_path} — {len(config.modules)} modules, {forbidden_total} forbidden edges")


def _handle_discover(args):
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    report = discover_dependencies(config_path)

    if args.format == "json":
        print(json.dumps(report.model_dump(), indent=2))
    else:
        _print_discover_report(report)


def _handle_save_baseline(args):
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    report = run_governance(config_path)
    baseline_path = Path(args.save_baseline)
    baseline_path.write_text(json.dumps(report.model_dump(), indent=2))
    print(f"Saved baseline with {len(report.violations)} violations to {baseline_path}")


def _handle_check(args):
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        print("Run with --fix-config to generate one", file=sys.stderr)
        sys.exit(1)

    config = load_config(config_path)
    if args.transitive:
        config.rules.transitive = True

    if args.diff:
        report = run_governance_diff(config_path, args.diff, config=config)
    else:
        report = run_governance(config_path, config=config)

    baseline_keys: set[tuple[str, str, str]] | None = None
    baseline_path = args.baseline
    if not baseline_path and args.diff:
        default_baseline = config_path.parent / ".governance-baseline.json"
        if default_baseline.exists():
            baseline_path = str(default_baseline)

    if baseline_path:
        bp = Path(baseline_path)
        if bp.exists():
            baseline_data = json.loads(bp.read_text())
            baseline_keys = {
                (v["rule"], v["module"], v["detail"])
                for v in baseline_data.get("violations", [])
            }

    if baseline_keys is not None:
        new_violations = [
            v for v in report.violations
            if (v.rule.value, v.module, v.detail) not in baseline_keys
        ]
        accepted_count = len(report.violations) - len(new_violations)
        report = report.model_copy(update={"violations": new_violations})
    else:
        accepted_count = 0

    if args.format == "json":
        print(json.dumps(report.model_dump(), indent=2))
    else:
        _print_text_report(report, accepted_count=accepted_count)

    if args.advise and (report.violations or not report.passed):
        _handle_advise(args, report)

    sys.exit(0 if report.passed else 1)


def _handle_advise(args, report=None):
    from code_governance.advisor import generate_advice
    from code_governance.advisor.providers import ConfigError

    config_path = Path(args.config)
    try:
        advice = generate_advice(config_path, governance_report=report)
    except ConfigError as e:
        print(f"\n{e}", file=sys.stderr)
        return

    if advice is None:
        return

    print()
    if args.format == "json":
        print(json.dumps(advice.model_dump(), indent=2))
    else:
        print(advice.to_markdown())


def _handle_affected_tests(args):
    from code_governance.test_targeting import execute_commands, run_affected_tests

    source_root = Path(args.affected_tests).resolve()
    if not source_root.exists():
        print(f"Source root not found: {source_root}", file=sys.stderr)
        sys.exit(1)

    result = run_affected_tests(source_root, args.base_ref, nx=args.nx)

    if args.format == "json":
        data = {
            "changed_files": result.changed_files,
            "affected_files": result.affected_files,
            "test_files": result.test_files,
            "scopes": [
                {"config": s.config_path, "tests": s.test_files}
                for s in result.scopes
            ],
            "commands": result.commands,
        }
        print(json.dumps(data, indent=2))
    else:
        _print_affected_tests(result)

    if args.run and result.commands:
        exit_code = execute_commands(result.commands, source_root)
        sys.exit(exit_code)


def _print_affected_tests(result):
    print(f"Changed files ({len(result.changed_files)}):")
    for f in result.changed_files:
        print(f"  {f}")
    print()

    print(f"Affected files ({len(result.affected_files)}):")
    for f in result.affected_files:
        print(f"  {f}")
    print()

    print(f"Tests to run ({len(result.test_files)}):")
    for scope in result.scopes:
        label = scope.root_dir or "root"
        for t in scope.test_files:
            print(f"  [{label}] {t}")
    print()

    if result.commands:
        print("Commands:")
        for cmd in result.commands:
            print(f"  {cmd}")
    else:
        print("No test files affected.")


def _handle_auto(args):
    source_root = Path(args.auto).resolve()
    if not source_root.exists():
        print(f"Source root not found: {source_root}", file=sys.stderr)
        sys.exit(1)

    report = run_auto_scan(source_root, max_depth=args.depth)

    if args.format == "json":
        print(json.dumps(report.model_dump(), indent=2))
    else:
        _print_text_report(report)

    sys.exit(0 if report.passed else 1)


def _print_discover_report(report):
    print(f"Module Dependencies — discovered ({report.language.value})")
    print(f"Config: {report.config_path}")
    print(f"Modules: {report.module_count} | Files scanned: {report.total_files_scanned}")
    print()

    for src_mod, targets in report.dependencies.items():
        symbol_count = 0
        for m in report.metrics:
            if m.name == src_mod:
                symbol_count = m.total_symbols
                break
        print(f"  {src_mod} ({symbol_count} symbols)")
        for t in targets:
            label = "import" if t.count == 1 else "imports"
            file_samples = []
            for f in t.files[:5]:
                fname = f["file"].rsplit("/", 1)[-1] if "/" in f["file"] else f["file"]
                file_samples.append(f"{fname}:{f['line']}")
            sample_str = ", ".join(file_samples)
            if len(t.files) > 5:
                sample_str += ", ..."
            print(f"    → {t.target:<20s} {t.count:>3} {label:<8s} ({sample_str})")
        print()

    if report.metrics:
        print("Module Metrics:")
        for m in report.metrics:
            parts = [f"  {m.name}: {m.total_symbols} symbols"]
            if m.cohesion_ratio is not None:
                parts.append(f"cohesion={m.cohesion_ratio:.2f}")
            if m.public_surface_ratio is not None:
                parts.append(f"surface={m.public_surface_ratio:.2f}")
            print(", ".join(parts))
        print()


def _print_text_report(report, accepted_count: int = 0):
    print(f"Governance Report ({report.language.value})")
    print(f"Config: {report.config_path}")
    print(f"Modules: {report.module_count} | Files scanned: {report.total_files_scanned}")
    print()

    if report.metrics:
        print("Module Metrics:")
        for m in report.metrics:
            parts = [f"  {m.name}: {m.total_symbols} symbols"]
            if m.public_surface_ratio is not None:
                parts.append(f"surface={m.public_surface_ratio:.2f}")
            if m.cohesion_ratio is not None:
                parts.append(f"cohesion={m.cohesion_ratio:.2f}")
            parts.append(f"edges(in={m.internal_edges}, out={m.external_edges})")
            dep_summary = _dep_summary_for_module(report, m.name)
            if dep_summary:
                parts.append(dep_summary)
            print(", ".join(parts))
        print()

    if report.violations:
        print(f"Violations ({len(report.violations)} new):" if accepted_count else f"Violations ({len(report.violations)}):")
        for v in report.violations:
            icon = "E" if v.severity.value == "error" else "W"
            print(f"  [{icon}] [{v.rule.value}] {v.detail}")
            for e in v.evidence[:5]:
                fname = e.get("source_file", "")
                line = e.get("line", 0)
                raw = e.get("raw_statement", "").strip()
                if len(raw) > 80:
                    raw = raw[:77] + "..."
                print(f"      {fname}:{line:<6} {raw}")
            if len(v.evidence) > 5:
                print(f"      ... and {len(v.evidence) - 5} more")
        if accepted_count:
            print(f"\n  ({accepted_count} existing violations accepted from baseline)")
        print()
        print("FAILED")
    else:
        if accepted_count:
            print(f"No new violations. ({accepted_count} existing accepted from baseline)")
        else:
            print("No violations found.")
        print("PASSED")


def _dep_summary_for_module(report, module_name: str) -> str:
    counts: dict[str, int] = {}
    for v in report.violations:
        if v.module == module_name:
            for e in v.evidence:
                tgt = e.get("target_module", "")
                if tgt:
                    counts[tgt] = counts.get(tgt, 0) + 1
    if not counts:
        return ""
    parts = [f"{t}({c})" for t, c in sorted(counts.items(), key=lambda x: -x[1])]
    return "→ " + ", ".join(parts)


if __name__ == "__main__":
    main()
