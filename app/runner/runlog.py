"""Нэг run-ын нийтлэг лог (run.log + events.jsonl) — pipeline-ын бүх алхам нэг файлд бичнэ."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.runner.detectors import make_sanitizer

Emit = Callable[[str, dict], None]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RunLog:
    def __init__(self, run_dir: Path, emit: Emit | None = None):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.emit = emit or (lambda t, d: None)
        # Нууц үг системд ордоггүй тул зөвхөн JWT/Bearer/token= шүүлтүүр үйлчилнэ
        self.sanitize = make_sanitizer([])
        self._log_fh = open(self.run_dir / "run.log", "a", encoding="utf-8")
        self._raw_fh = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")

    def log(self, msg: str, level: str = "info", to_ui: bool = True) -> None:
        msg = self.sanitize(msg)
        if not self._log_fh.closed:
            self._log_fh.write(f"{now_iso()} [{level.upper()}] {msg}\n")
            self._log_fh.flush()
        if to_ui:
            self.emit("log", {"level": level, "msg": msg, "at": now_iso()})

    def raw(self, ev: dict) -> None:
        if not self._raw_fh.closed:
            self._raw_fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self._log_fh.close()
        self._raw_fh.close()
