"""Generate terse CI reports for PR comments."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from code_governance.config import load_config
from code_governance.engine import (
    config_to_toml,
    generate_config,
    run_governance,
)
from code_governance.schemas import GovernanceConfig, RuleKind, Violation


RULE_ICON = {
    RuleKind.ENFORCE_CANNOT_DEPEND_ON: "🔗",
    RuleKind.NO_CYCLES: "🔄",
    RuleKind.NO_FILE_CYCLES: "🔁",
    RuleKind.ENFORCE_LAYERS: "📐",
    RuleKind.MAX_PUBLIC_SURFACE: "📡",
    RuleKind.MIN_COHESION: "🧩",
}


def _format_violation(v: Violation) -> str:
    """One-line violation with evidence."""
    icon = RULE_ICON.get(v.rule, "❌")
    severity = "🔴" if v.severity.value == "error" else "🟡"

    # Extract target module from detail if present
    parts = []
    for e in v.evidence[:3]:
        sf = e.get("source_file", "")
        ln = e.get("line", "")
        if sf and ln:
            parts.append(f"`{sf}:{ln}`")

    locations = " ".join(parts)
    return f"{severity} {icon} **{v.module}**: {v.detail}  \n{locations}"


def _find_new_modules(
    config: GovernanceConfig,
    source_root: Path,
) -> list[dict[str, str]]:
    discovered = generate_config(str(source_root), "python")
    existing_paths = {m.path.rstrip("/") for m in config.modules}
    existing_names = {m.name for m in config.modules}

    new_modules = []
    for mod in discovered.modules:
        mod_path = mod.path.rstrip("/")
        if mod_path not in existing_paths and mod.name not in existing_names and mod_path != ".":
            new_modules.append({"name": mod.name, "path": mod.path})
    return new_modules


def _suggest_config_update(
    config: GovernanceConfig,
    new_modules: list[dict[str, str]],
) -> str:
    from code_governance.schemas import ModuleConfig

    updated = config.model_copy(deep=True)
    for mod in new_modules:
        updated.modules.append(ModuleConfig(
            name=mod["name"],
            path=mod["path"],
            cannot_depend_on=[],
        ))
    return config_to_toml(updated)


def generate_ci_report(
    config_path: str | Path,
    advise: bool = False,
) -> dict:
    config_path = Path(config_path)
    config = load_config(config_path)
    source_root = config_path.parent / config.root

    report = run_governance(config_path)
    new_modules = _find_new_modules(config, source_root)

    lines = []

    # Header — one line
    v_count = len(report.violations)
    if v_count == 0 and not new_modules:
        lines.append("## ✅ Governance — passed")
    else:
        problems = []
        if v_count:
            problems.append(f"{v_count} violation{'s' if v_count != 1 else ''}")
        if new_modules:
            problems.append(f"{len(new_modules)} new module{'s' if len(new_modules) != 1 else ''}")
        lines.append(f"## ❌ Governance — {', '.join(problems)}")
    lines.append("")

    # Violations — one line each
    if report.violations:
        for v in report.violations:
            lines.append(_format_violation(v))
        lines.append("")

    # New modules — compact with checkbox fix
    updated_config = None
    if new_modules:
        for mod in new_modules:
            lines.append(f"📦 **New module `{mod['name']}`** (`{mod['path']}`) — not in `governance.toml`")

        updated_config = _suggest_config_update(config, new_modules)
        lines.append("")
        lines.append("Reply `/governance fix` to apply.")
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Config changes</summary>")
        lines.append("")
        lines.append("```toml")
        for mod in new_modules:
            lines.append(f'[[modules]]')
            lines.append(f'name = "{mod["name"]}"')
            lines.append(f'path = "{mod["path"]}"')
            lines.append(f'cannot_depend_on = []')
        lines.append("```")
        lines.append("Add forbidden dependencies to `cannot_depend_on` as needed.")
        lines.append("</details>")
        lines.append("")

        # Embed payload for the fix workflow
        payload = json.dumps({
            "config_path": str(config_path),
            "updated_config": updated_config,
        })
        encoded = base64.b64encode(payload.encode()).decode()
        lines.append(f"<!-- governance-fix:{encoded} -->")

    # LLM advice — compact
    advice_data = None
    if advise and (report.violations or new_modules):
        try:
            from code_governance.advisor import generate_advice

            advice = generate_advice(config_path, governance_report=report, new_modules=new_modules)
            if advice:
                lines.append(advice.to_markdown())
                advice_data = advice.model_dump()
        except Exception as e:
            lines.append(f"⚠️ AI advice unavailable: {e}")

    # Metrics — collapsed
    if report.metrics:
        lines.append("<details>")
        lines.append("<summary>Metrics</summary>")
        lines.append("")
        lines.append("| Module | Sym | Cohesion | Surface |")
        lines.append("|--------|-----|----------|---------|")
        for m in report.metrics:
            c = f"{m.cohesion_ratio:.2f}" if m.cohesion_ratio is not None else "—"
            s = f"{m.public_surface_ratio:.2f}" if m.public_surface_ratio is not None else "—"
            lines.append(f"| {m.name} | {m.total_symbols} | {c} | {s} |")
        lines.append("</details>")

    markdown = "\n".join(lines)

    return {
        "markdown": markdown,
        "violations": [v.model_dump() for v in report.violations],
        "new_modules": new_modules,
        "updated_config": updated_config,
        "advice": advice_data,
        "passed": report.passed and len(new_modules) == 0,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="governance-ci-report")
    parser.add_argument("--config", "-c", default="governance.toml")
    parser.add_argument("--output-markdown", help="Write markdown report to file")
    parser.add_argument("--output-config", help="Write updated config if new modules found")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--advise", action="store_true", help="LLM architectural advice")
    args = parser.parse_args()

    result = generate_ci_report(args.config, advise=args.advise)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["markdown"])

    if args.output_markdown:
        Path(args.output_markdown).write_text(result["markdown"])

    if args.output_config and result["updated_config"]:
        Path(args.output_config).write_text(result["updated_config"])

    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
