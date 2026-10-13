"""Flow config-ийн туслахууд: selector, товчны нэр, цэсний зам → MenuItem."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.flows.context import StepFailed
from app.models import MenuItem

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


def fcfg(env: "FlowEngine") -> dict:
    return env.cfg.get("flow") or {}


def sel_payload(env: "FlowEngine") -> dict:
    return {**fcfg(env).get("selectors", {}), "dialog": env.cfg["selectors"]["dialog"]}


def buttons(env: "FlowEngine", kind: str, override=None) -> list[str]:
    if override:
        return [override] if isinstance(override, str) else list(override)
    return list((fcfg(env).get("buttons") or {}).get(kind) or [])


def menu_item(path: str) -> MenuItem:
    """'Модуль / Хавтас / Дэд хавтас / Цэс' эсвэл smoke-ийн цэсний түлхүүр 'module|folder|name|href' → MenuItem."""
    if path.count("|") == 3:
        m, folder, name, href = path.split("|")
        return MenuItem(module=m, folder=folder, name=name, href=href)
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if len(parts) < 2:
        raise StepFailed(f"Цэсний зам 'Модуль / ... / Цэс' хэлбэртэй байх ёстой: {path!r}")
    return MenuItem(module=parts[0], folder=" / ".join(parts[1:-1]), name=parts[-1], href="")
