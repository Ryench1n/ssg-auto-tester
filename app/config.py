"""YAML тохиргоо ачаалах: default.yaml + sites/<host>.yaml (deep merge) + override."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TEMPLATES_DIR = ROOT / "templates"
RUNS_DIR = ROOT / "runs"
RECORDED_DIR = CONFIG_DIR / "recorded"      # тестэд monkeypatch хийж болно


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(url: str | None = None, overrides: dict | None = None,
                config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    cfg = yaml.safe_load((config_dir / "default.yaml").read_text(encoding="utf-8")) or {}
    if url:
        host = urlparse(url).hostname or ""
        site = config_dir / "sites" / f"{host}.yaml"
        if site.exists():
            cfg = deep_merge(cfg, yaml.safe_load(site.read_text(encoding="utf-8")) or {})
        # UI-аас бичсэн урсгалууд (record → replay): config/recorded/<host>.yaml → flows
        rec = recorded_path(host)
        if rec.exists():
            flows = (yaml.safe_load(rec.read_text(encoding="utf-8")) or {}).get("flows") or {}
            cfg["flows"] = {**(cfg.get("flows") or {}), **flows}
    return deep_merge(cfg, overrides)


RECORDED_PREFIX = "rec: "


def recorded_path(host: str) -> Path:
    return RECORDED_DIR / f"{host}.yaml"


def load_recorded(host: str) -> dict[str, Any]:
    p = recorded_path(host)
    return ((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("flows") or {}) if p.exists() else {}


def save_recorded(host: str, flows: dict[str, Any]) -> None:
    p = recorded_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# UI-аас бичсэн урсгалууд (SSG Auto Tester → «Урсгал бичих»). Гараар засаж болно.\n"
                 + yaml.safe_dump({"flows": flows}, allow_unicode=True, sort_keys=False, width=120),
                 encoding="utf-8")
