"""Автомат CRUD-ийн илрүүлсэн формоос `flows:` YAML ноорог үүсгэнэ.

QA хүн ноорогийг шалгаж (талбарын нэр, утга, TODO) config/sites/<host>.yaml-д хуулснаар тухайн цэсэд
баталгаатай, давтагдах CRUD flow болно.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.runner.classify import transliterate

HEADER = """# SSG Auto Tester — автомат CRUD-оос үүсгэсэн flow НООРОГ (run {run_id}).
# ⚠ Шалгаж засаад config/sites/<host>.yaml-ын `flows:` доор хуулна:
#   - "TODO" утгатай талбар = автоматаар бөглөж чадаагүй заавал талбар (сонголт, огноо …)
#   - key талбарын утга заавал {{run_tag}} агуулна (тестийн өгөгдлийг таних, цэвэрлэх)
#   - form_fields нь зөвхөн тайлбар (engine үл тоомсорлоно)
"""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", transliterate(name).lower()).strip("_")
    return s[:40] or "menu"


def draft_flows(flows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for fl in flows:
        meta = fl.get("meta") or {}
        if not meta.get("auto") or not (meta.get("fields") or meta.get("probe")):
            continue
        menu = meta.get("menu", "")
        fields = dict(meta.get("fields") or {"TODO талбарын шошго": "{run_tag} ..."})
        key = meta.get("key") or next(iter(fields))
        name = base = "auto_" + _slug(menu.split(" / ")[-1])
        n = 2
        while name in out:
            name, n = f"{base}_{n}", n + 1
        steps = [
            {"uses": "auth.login", "name": "Нэвтрэх"},
            {"uses": "crud.create", "name": "Бүртгэх", "with": {
                "menu": menu, "fields": fields, "key": key, "save_as": "record"}},
            {"uses": "crud.search", "name": "Бүртгэснээ хайх", "with": {"text": "{record}"}},
        ]
        if meta.get("edit_value"):
            steps += [
                {"uses": "crud.update", "name": "Засах", "with": {
                    "find": "{record}", "key": key, "save_as": "record", "fields": {key: meta["edit_value"]}}},
                {"uses": "crud.search", "name": "Засварыг шалгах", "with": {"text": "{record}"}},
            ]
        steps += [
            {"uses": "crud.delete", "name": "Цэвэрлэх (устгах)", "always": True, "with": {"find": "{record}"}},
            {"uses": "crud.search", "name": "Устгагдсаныг шалгах", "with": {"text": "{record}", "expect": "absent"}},
        ]
        status = f"автомат run: {fl.get('status')}" + (f" — {meta['skipped']}" if meta.get("skipped") else "")
        entry = {"description": f"«{menu}» CRUD ({status})",
                 "form_fields": [f"{f['label']}{' *' if f.get('required') else ''} "
                                 f"({'огноо' if f.get('date') else 'lookup' if f.get('lookup') else f.get('type') or f.get('tag')}"
                                 f"{', сонголт' if f.get('combo') else ''}{', readonly' if f.get('readonly') else ''})"
                                 for f in meta.get("form") or []],
                 "steps": steps}
        if meta.get("probe"):
            # Оношлогоо (engine үл тоомсорлоно): «Шинэ» дарсны дараах табууд, цонх, input-уудын дээж
            entry["diagnostics"] = {"opened": meta.get("opened"), **meta["probe"]}
        out[name] = entry
    return out


def write_draft(run_dir: Path, run_id: str, flows: list[dict]) -> str:
    """Ноорог үүссэн бол файлын нэрийг, эс бөгөөс "" буцаана."""
    d = draft_flows(flows)
    if not d:
        return ""
    path = Path(run_dir) / "flows-draft.yaml"
    body = yaml.safe_dump({"flows": d}, allow_unicode=True, sort_keys=False, width=120)
    path.write_text(HEADER.format(run_id=run_id) + body, encoding="utf-8")
    return path.name
