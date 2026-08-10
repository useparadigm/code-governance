from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_governance.languages.tsconfig import _strip_jsonc, load_tsconfig


def test_strips_line_comments():
    assert json.loads(_strip_jsonc('{"a": 1 // comment\n}')) == {"a": 1}


def test_strips_block_comments():
    assert json.loads(_strip_jsonc('{"a": /* x */ 1}')) == {"a": 1}


def test_strips_trailing_commas():
    assert json.loads(_strip_jsonc('{"a": [1, 2,],}')) == {"a": [1, 2]}


def test_preserves_string_with_slashes():
    assert json.loads(_strip_jsonc('{"url": "http://x.com"}')) == {"url": "http://x.com"}


def test_preserves_string_with_comment_like_content():
    assert json.loads(_strip_jsonc('{"s": "a // b"}')) == {"s": "a // b"}


def test_load_returns_none_when_missing(tmp_path):
    assert load_tsconfig(tmp_path) is None


def test_load_basic(tmp_path):
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}'
    )
    cfg = load_tsconfig(tmp_path)
    assert cfg is not None
    assert cfg.base_url == "."
    assert cfg.paths == {"@/*": ["src/*"]}


def test_extends_merges_paths(tmp_path):
    (tmp_path / "tsconfig.base.json").write_text(
        '{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}'
    )
    (tmp_path / "tsconfig.json").write_text(
        '{"extends": "./tsconfig.base.json", "compilerOptions": {"baseUrl": "."}}'
    )
    cfg = load_tsconfig(tmp_path)
    assert cfg.base_url == "."
    assert cfg.paths == {"@/*": ["src/*"]}


def test_extends_child_overrides_base(tmp_path):
    (tmp_path / "tsconfig.base.json").write_text(
        '{"compilerOptions": {"paths": {"@/*": ["old/*"]}}}'
    )
    (tmp_path / "tsconfig.json").write_text(
        '{"extends": "./tsconfig.base.json", "compilerOptions": {"paths": {"@/*": ["src/*"]}}}'
    )
    cfg = load_tsconfig(tmp_path)
    assert cfg.paths == {"@/*": ["src/*"]}


def test_extends_anchors_base_url_to_declaring_config(tmp_path):
    """A `{"extends": "../tsconfig.json"}` stub must not re-anchor the parent's
    `baseUrl` under its own folder — PostHog's frontend/tsconfig.json is exactly
    this, and re-anchoring turned `frontend/` into `frontend/frontend/`."""
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"baseUrl": "frontend/", "paths": {"lib/*": ["src/lib/*"]}}}'
    )
    child = tmp_path / "frontend"
    child.mkdir()
    (child / "tsconfig.json").write_text('{"extends": "../tsconfig.json"}')

    cfg = load_tsconfig(child)
    assert cfg is not None
    assert cfg.base_url == "frontend/"
    assert cfg.config_dir == tmp_path.resolve()
    assert (cfg.config_dir / cfg.base_url / "src/lib").resolve() == (
        tmp_path / "frontend" / "src" / "lib"
    ).resolve()


def test_extends_skips_npm_resolved(tmp_path):
    (tmp_path / "tsconfig.json").write_text(
        '{"extends": "@tsconfig/node18/tsconfig.json", "compilerOptions": {"baseUrl": "."}}'
    )
    cfg = load_tsconfig(tmp_path)
    assert cfg is not None
    assert cfg.base_url == "."
