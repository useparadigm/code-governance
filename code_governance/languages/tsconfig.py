from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TsConfig:
    base_url: Optional[str] = None
    paths: dict[str, list[str]] = field(default_factory=dict)
    config_dir: Path = field(default_factory=Path)


def load_tsconfig(repo_root: Path, filename: str | list[str] = "tsconfig.json") -> Optional[TsConfig]:
    filenames = [filename] if isinstance(filename, str) else filename
    for fname in filenames:
        path = repo_root / fname
        if not path.exists():
            continue
        try:
            loaded = _load_with_extends(path, visited=set())
        except Exception as e:
            print(f"Warning: failed to parse {path}: {e}", file=sys.stderr)
            continue
        if loaded is None:
            continue
        merged, origin = loaded

        compiler_options = merged.get("compilerOptions", {})
        base_url = compiler_options.get("baseUrl")
        raw_paths = compiler_options.get("paths", {})
        paths: dict[str, list[str]] = {}
        if isinstance(raw_paths, dict):
            for key, value in raw_paths.items():
                if isinstance(value, list):
                    paths[key] = [str(v) for v in value]

        # `baseUrl` and `paths` are relative to the config that *declares* them, not
        # to the one that inherited them through `extends` — a `{"extends": "../"}`
        # stub would otherwise re-anchor the parent's targets under its own folder.
        anchor = origin.get("baseUrl") or origin.get("paths") or path.parent.resolve()
        return TsConfig(base_url=base_url, paths=paths, config_dir=anchor)
    return None


def _load_with_extends(path: Path, visited: set[Path]) -> Optional[tuple[dict, dict[str, Path]]]:
    """(merged config, declaring directory per compilerOptions key)."""
    resolved = path.resolve()
    if resolved in visited:
        return None
    visited.add(resolved)

    raw = path.read_text(encoding="utf-8", errors="replace")
    data = json.loads(_strip_jsonc(raw))
    own_dir = path.parent.resolve()
    own_origin = {k: own_dir for k in data.get("compilerOptions", {})}

    extends = data.get("extends")
    if not extends:
        return data, own_origin

    extends_list = extends if isinstance(extends, list) else [extends]
    merged: dict = {}
    origin: dict[str, Path] = {}
    for ext in extends_list:
        if not isinstance(ext, str):
            continue
        if ext.startswith("@") or (not ext.startswith(".") and "/" in ext and not ext.endswith(".json")):
            print(f"Warning: skipping npm-resolved extends '{ext}' in {path}", file=sys.stderr)
            continue
        candidate = (path.parent / ext).resolve()
        if not candidate.suffix:
            candidate = candidate.with_suffix(".json")
        if not candidate.exists():
            print(f"Warning: extends target not found: {candidate}", file=sys.stderr)
            continue
        loaded = _load_with_extends(candidate, visited)
        if loaded:
            base, base_origin = loaded
            merged = _shallow_merge(merged, base)
            origin.update(base_origin)

    merged = _shallow_merge(merged, data)
    merged.pop("extends", None)
    origin.update(own_origin)
    return merged, origin


def _shallow_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k == "compilerOptions" and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j == -1:
                i = n
            else:
                i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                i = n
            else:
                i = j + 2
            continue
        out.append(ch)
        i += 1
    return _TRAILING_COMMA.sub(r"\1", "".join(out))
