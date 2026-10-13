"""Pipeline-ын нэг session: гар нэвтрэлт, logout илрүүлэх, түр зогсоох/үргэлжлүүлэх.

Бүх алхам (smoke, crud, …) нэг page/session хуваалцана. Session тасарвал (logout) run түр
зогсож, хэрэглэгч хөтөч дээр дахин гараар нэвтэрмэгц тасарсан алхмаасаа үргэлжилнэ.
Хугацаа хэтэрвэл `lost=True` болж, pipeline үлдсэн алхмуудыг Blocked болгоно.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from app.models import RunData, TimelineEvent
from app.runner.detectors import snapshot
from app.runner.runlog import RunLog, now_iso
from app.runner.session import LoginCancelled, LoginError, is_login_url, login

if TYPE_CHECKING:
    from app.runner.runlog import Emit

# Тестийн hook: гар нэвтрэлт хүлээж эхлэхэд page-ийг дамжуулна (production-д None)
LoginObserver = Callable[[object], Awaitable[None]]


class SessionLost(Exception):
    """Session тасарч, гар нэвтрэлтийн хугацаанд дахин нэвтрээгүй."""


class SessionGuard:
    def __init__(self, url: str, cfg: dict, data: RunData, runlog: RunLog, emit: "Emit",
                 halt: asyncio.Event, on_waiting_login: LoginObserver | None = None):
        self.url = url
        self.cfg = cfg
        self.data = data
        self.runlog = runlog
        self.emit = emit
        self.halt = halt                    # хэрэглэгч зогсоосон ЭСВЭЛ session алдагдсан
        self.on_waiting_login = on_waiting_login
        self.page = None
        self.stage = "auth"                 # waiting_login event-д аль алхам дээр зогссоныг заана
        self.lost = False
        self.lost_reason = ""
        self.last_login_mono = 0.0
        self.segments: list[float] = []
        self._hook_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ helpers
    def _timeline(self, activity: str, result: str = "", note: str = "") -> None:
        self.data.timeline.append(TimelineEvent(now_iso(), activity, result, note))

    def _mark_login(self) -> None:
        self.last_login_mono = time.monotonic()
        self.data.login_times.append(now_iso())
        self.emit("login_ok", {"stage": self.stage, "at": now_iso()})

    def close_segment(self) -> None:
        if self.last_login_mono:
            self.segments.append(time.monotonic() - self.last_login_mono)
            self.last_login_mono = 0.0

    def _waiting(self, reason: str):
        def cb(timeout_s: float) -> None:
            self.runlog.log(f"Нээгдсэн хөтөч дээр нэвтэрнэ үү ({reason}) — дээд тал нь {int(timeout_s)} сек хүлээнэ",
                            "warn")
            self.emit("waiting_login", {"reason": reason, "stage": self.stage, "timeout_s": int(timeout_s),
                                        "at": now_iso()})
            if self.on_waiting_login is not None:
                t = asyncio.get_running_loop().create_task(self.on_waiting_login(self.page))
                self._hook_tasks.add(t)
                t.add_done_callback(self._hook_tasks.discard)
        return cb

    async def is_logged_out(self) -> bool:
        st = await snapshot(self.page, self.cfg)
        return bool(st and is_login_url(st.url, self.cfg))

    # --------------------------------------------------------------- main
    async def first_login(self) -> str:
        """Эхний гар нэвтрэлт. LoginError / LoginCancelled шиднэ."""
        version = await login(self.page, self.url, self.cfg, navigate=True,
                              on_manual_wait=self._waiting("эхний нэвтрэлт"), cancel_event=self.halt)
        self._mark_login()
        return version

    async def relogin(self, during: str, count: bool = True) -> bool:
        """Logout илэрсэн → run-ыг түр зогсоож гар нэвтрэлт хүлээнэ. Амжилттай бол True."""
        if self.lost or self.halt.is_set():
            return False
        if count:
            after = time.monotonic() - self.last_login_mono if self.last_login_mono else 0.0
            self.close_segment()
            self.data.logouts.append({"at": now_iso(), "after_s": round(after, 1), "during": during})
            self.runlog.log(f"Logout илэрлээ ({int(after // 60)} мин {int(after % 60)} сек-ийн дараа) — "
                            f"run түр зогсож, дахин гар нэвтрэлт хүлээж байна", "warn")
            self._timeline("Logout → гар дахин нэвтрэлт", "Түр зогссон",
                           f"Нэвтэрснээс {int(after // 60)} мин {int(after % 60)} сек-ийн дараа; алхам: {during}")
        else:
            self.close_segment()
        try:
            t = self.cfg.get("timing") or {}
            await login(self.page, self.url, self.cfg, navigate=False,
                        on_manual_wait=self._waiting(f"session тасарсан — {during}"), cancel_event=self.halt,
                        timeout_ms=t.get("relogin_timeout_ms") or t.get("manual_login_timeout_ms"))
        except LoginCancelled:
            self.runlog.log("Хэрэглэгч дахин нэвтрэлтийг хүлээж байх зуур зогсоолоо", "warn")
            return False
        except LoginError as e:
            self.lost, self.lost_reason = True, f"Session тасарч, дахин нэвтрээгүй: {e}"
            self.runlog.log(f"{self.lost_reason} — үлдсэн алхмууд Blocked болно", "error")
            self._timeline("Дахин нэвтрэлт", "Fail", str(e)[:200])
            self.emit("session_lost", {"reason": self.lost_reason, "stage": self.stage})
            self.halt.set()
            return False
        self._mark_login()
        self.runlog.log("Дахин нэвтэрлээ — тасарсан алхмаасаа үргэлжилнэ")
        return True

    async def ensure(self, during: str) -> None:
        """Алхмын өмнө: logout болсон бол дахин нэвтрэлт хүлээнэ. Чадахгүй бол SessionLost."""
        if self.lost:
            raise SessionLost(self.lost_reason)
        if await self.is_logged_out():
            if not await self.relogin(during):
                raise SessionLost(self.lost_reason or "Хэрэглэгч зогсоосон")
