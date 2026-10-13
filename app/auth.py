"""SSG Auto Tester-ийн өөрийн хэрэглэгчийн нэвтрэлт (SmartApp-ийн нэвтрэлттэй холбоогүй).

Нууц үг PBKDF2-SHA256 hash-аар, session token зөвхөн SHA-256 hash-аар хадгалагдана. Cookie: HttpOnly,
SameSite=Strict (өөр сайтаас API дуудах CSRF-ийг хаана).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta

COOKIE = "ssg_session"
SESSION_HOURS = 12
MIN_PASSWORD = 8
_ITERATIONS = 200_000

# Нэг хэрэглэгчийн нэрээр 5 минутад 5 удаа буруу оролдвол түр түгжинэ (brute-force)
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


# Хэрэглэгч олдоогүй үед ч ижил хугацаа зарцуулна (нэр байгаа эсэхийг хариуны хугацаагаар таахгүй)
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_login(user: dict | None, password: str) -> bool:
    if not user or user.get("disabled"):
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, user["pw_hash"])


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expires_at(hours: int = SESSION_HOURS) -> str:
    return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")


def generate_password(length: int = 12) -> str:
    """Андуурагдах тэмдэгтгүй (0/O, 1/l/I) санамсаргүй нууц үг."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def password_problem(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        return f"Нууц үг дор хаяж {MIN_PASSWORD} тэмдэгт байна"
    return ""


class Throttle:
    def __init__(self, max_failures: int = MAX_FAILURES, window_s: float = FAILURE_WINDOW_S):
        self.max, self.window = max_failures, window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._fails.get(key, []) if now - t < self.window]

    def blocked(self, key: str) -> int:
        """Түгжээтэй бол үлдсэн секунд, үгүй бол 0."""
        now = time.monotonic()
        with self._lock:
            recent = self._fails[key] = self._recent(key, now)
            return int(self.window - (now - recent[0])) + 1 if len(recent) >= self.max else 0

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._fails[key] = self._recent(key, now) + [now]

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
