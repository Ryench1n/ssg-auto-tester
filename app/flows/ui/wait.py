"""Ачаалал дуустал хүлээх, popup/toast дахь алдааг илрүүлэх."""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from app import models as M
from app.flows.context import StepFailed
from app.models import Finding
from app.runner.detectors import dialog_text, snapshot
from app.flows.ui.base import fcfg

if TYPE_CHECKING:
    from app.flows.engine import FlowEngine


# ------------------------------------------------------------------ хүлээлт / алдаа
async def settle(env: "FlowEngine", max_s: float | None = None) -> None:
    """Ачаалал (loading индикатор) дуустал (дээд тал нь max_s) хүлээгээд, дараа нь сүлжээ тайван болохыг
    `flow.settle_cap_ms` хүртэл л хүлээнэ. SmartApp-д байнга нээлттэй (polling/long-poll) хүсэлт байдаг тул
    сүлжээ хэзээ ч «тайван» болохгүй байж болно — урьд нь энэ нь алхам бүрт 10–30 сек хиймэл хүлээлт үүсгэдэг
    байсан (HR: шинэ форм ~43 сек). Smoke ч мөн адил богино хязгаартай (timing.settle_ms)."""
    quiet = fcfg(env).get("settle_ms", 500) / 1000
    cap = fcfg(env).get("settle_cap_ms", 1500) / 1000
    deadline = time.monotonic() + (max_s if max_s is not None else env.cfg["timing"]["loading_blocked_ms"] / 1000)
    calm_since = None
    while time.monotonic() < deadline:
        st = await snapshot(env.page, env.cfg)
        if st and not st.loading:
            if env.col.quiet_for(quiet):
                return
            calm_since = calm_since or time.monotonic()
            if time.monotonic() - calm_since >= cap:
                return
        else:
            calm_since = None
        await asyncio.sleep(0.1)


def _is_error_text(env: "FlowEngine", text: str) -> bool:
    det = env.cfg.get("detect") or {}
    low = text.lower()
    pats = (det.get("system_error_title") or []) + (det.get("sql_error") or []) + (det.get("auth_error") or [])
    return any(p.lower() in low for p in pats) or any(w in low for w in ("exception", "error"))


async def popup_problems(env: "FlowEngine") -> tuple[list[Finding], list[str]]:
    """(алдаа, validation-ий анхааруулга) — цонх/toast-оос."""
    st = await snapshot(env.page, env.cfg)
    if not st:
        return [], []
    warn_pats = [p.lower() for p in ((env.cfg.get("detect") or {}).get("warning_toast") or [])
                 + (fcfg(env).get("validation_texts") or [])]
    titles = [t.lower() for t in fcfg(env).get("validation_titles") or []]

    def is_warning(text: str, dialog: bool = False) -> bool:
        low = text.lower()
        first = low.strip().split("\n")[0].strip()
        if any(first.startswith(t) for t in titles):
            return True
        # Форм өөрөө цонх — урт текстэд (шошгонууд) эдгээр үг таарч болох тул зөвхөн богино мессеж цонх
        return (not dialog or len(low) <= 300) and any(p in low for p in warn_pats)

    def is_question(text: str) -> bool:
        """Баталгаажуулах асуулт («… итгэлтэй байна уу?», «Хадгалах уу?») — гарчиг нь «Анхааруулга» байсан ч
        validation анхааруулга биш: дараагийн алхам (Тийм/Үгүй) хариулна. Бичсэн урсгалд «Устгах» алхам
        Fail болдог байсан (HR «Тогтмол ажил», 2026-10-05)."""
        body = text.strip()
        return len(body) <= 300 and bool(re.search(r"(уу|үү|юу|юү)\s*\?|итгэлтэй|\?\s*$", body, re.I))

    def is_error_dialog(text: str) -> bool:
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        if len(text) <= 200 and len(lines) <= 4:
            return _is_error_text(env, text)          # богино мессеж цонх
        # Форм цонх (олон мөр/урт): шошго/багананд «Томьёо алдаатай», «error» мэт үг байж болно (ж: HR «Цалин бодох
        # загвар» — шошгоноос болж худал Fail болдог байсан) — зөвхөн гарчиг нь системийн алдаа, эсвэл SQL / exception
        if any(w in text for w in ("Exception:", "Object reference not set", "Unhandled exception")):
            return True
        # Урт текст = форм цонх: шошго/багананд «Томьёо алдаатай», «error» мэт үг байж болно — зөвхөн
        # SQL алдааны текст эсвэл системийн алдааны гарчгаар эхэлсэн үед алдаа гэж үзнэ
        det = env.cfg.get("detect") or {}
        low, first = text.lower(), text.strip().split("\n")[0].strip().lower()
        return (any(p.lower() in low for p in det.get("sql_error") or [])
                or any(first.startswith(t.lower()) and len(first) <= len(t) + 3
                       for t in det.get("system_error_title") or []))

    errors, warnings = [], []
    for d in st.dialogs:
        if is_error_dialog(d):
            errors.append(Finding(M.SYSTEM_ERROR, dialog_text(d)[:500]))
        elif is_question(d):
            continue                         # «Анхааруулга — …Устгахдаа итгэлтэй байна уу?» — асуулт, асуудал биш
        elif is_warning(d, dialog=True):     # «Анхаар — Данс оруулна уу!» мэт validation цонх
            warnings.append(dialog_text(d))
    for t in st.toasts:
        if _is_error_text(env, t) or "алдаа" in t.lower():
            errors.append(Finding(M.SYSTEM_ERROR, dialog_text(t)[:500]))
        elif is_warning(t):
            warnings.append(dialog_text(t))
    return errors, warnings


async def raise_on_problems(env: "FlowEngine", what: str, warnings_too: bool = True) -> None:
    errors, warnings = await popup_problems(env)
    if errors:
        raise StepFailed(f"{what}: {errors[0].text}", errors)
    if warnings and warnings_too:
        raise StepFailed(f"{what}: баталгаажуулалтын анхааруулга — {warnings[0]}",
                         [Finding(M.WARNING_TOAST, w) for w in warnings])
