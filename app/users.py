"""SSG Auto Tester-ийн хэрэглэгчийн удирдлага (сервер дээрээс).

python -m app.users init [--count 15] [--out initial-passwords.txt]   admin + qa01..qa14 үүсгэнэ
python -m app.users list
python -m app.users add <нэр> [--admin] [--name "Овог Нэр"]          санамсаргүй нууц үг хэвлэнэ
python -m app.users reset <нэр>                                       шинэ санамсаргүй нууц үг хэвлэнэ
python -m app.users disable|enable <нэр>

Нууц үг зөвхөн нэг удаа (дэлгэц / --out файл) харагдана — DB-д зөвхөн hash хадгалагдана.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from app import auth
from app.config import ROOT
from app.store import LEGACY_OWNER, Store

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


def default_usernames(count: int) -> list[str]:
    """admin (бүх түүхийг харна, хуучин run-ууд түүнийх) + qa01, qa02 … — нийт count."""
    return [LEGACY_OWNER] + [f"qa{i:02d}" for i in range(1, count)]


def add_user(store: Store, username: str, is_admin: bool = False, name: str = "",
             password: str | None = None) -> str:
    username = username.strip().lower()
    if not _NAME.match(username):
        raise ValueError(f"Хэрэглэгчийн нэр буруу: {username!r} (a-z, 0-9, . _ -, 2-32 тэмдэгт)")
    if store.get_user(username):
        raise ValueError(f"{username} хэрэглэгч аль хэдийн байна")
    password = password or auth.generate_password()
    store.create_user(username, auth.hash_password(password), name, is_admin)
    return password


def reset_password(store: Store, username: str) -> str:
    password = auth.generate_password()
    if not store.update_user(username, pw_hash=auth.hash_password(password)):
        raise ValueError(f"{username} хэрэглэгч олдсонгүй")
    store.delete_sessions(username=username)
    return password


def init_users(store: Store, count: int) -> list[tuple[str, str, bool]]:
    """Байхгүй хэрэглэгчдийг л үүсгэнэ (дахин ажиллуулахад байгаа нууц үгийг өөрчлөхгүй)."""
    created = []
    for u in default_usernames(count):
        if store.get_user(u):
            continue
        admin = u == LEGACY_OWNER
        created.append((u, add_user(store, u, is_admin=admin, name="Администратор" if admin else ""), admin))
    return created


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="SSG Auto Tester хэрэглэгчид")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="admin + qa01.. хэрэглэгчдийг үүсгэнэ")
    p.add_argument("--count", type=int, default=15)
    p.add_argument("--out", default="initial-passwords.txt", help="нууц үгийг бичих файл (git-д орохгүй)")
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true")
    p.add_argument("--name", default="")
    for c in ("reset", "disable", "enable"):
        sub.add_parser(c).add_argument("username")
    a = ap.parse_args(argv)
    store = Store()
    try:
        if a.cmd == "init":
            created = init_users(store, a.count)
            if not created:
                print("Бүх хэрэглэгч аль хэдийн үүссэн байна — нууц үг өөрчлөгдөөгүй.")
                return
            lines = [f"{u}\t{pw}\t{'admin' if adm else 'хэрэглэгч'}" for u, pw, adm in created]
            out = Path(a.out) if Path(a.out).is_absolute() else ROOT / a.out
            out.write_text("# SSG Auto Tester — анхны нууц үг. Хэрэглэгчдэд хүргээд ЭНЭ ФАЙЛЫГ УСТГАНА УУ.\n"
                           "# Нэвтэрсний дараа «Нууц үг солих»-оор өөрчилнө.\n" + "\n".join(lines) + "\n",
                           encoding="utf-8")
            print(f"{len(created)} хэрэглэгч үүсгэв. Нууц үг: {out}")
        elif a.cmd == "list":
            for u in store.list_users():
                flags = ("admin " if u["is_admin"] else "") + ("ИДЭВХГҮЙ" if u["disabled"] else "")
                print(f"{u['username']:<12} {flags:<15} сүүлд нэвтэрсэн: {u['last_login'] or '-'}")
        elif a.cmd == "add":
            print(f"{a.username.lower()}: {add_user(store, a.username, a.admin, a.name)}")
        elif a.cmd == "reset":
            print(f"{a.username}: {reset_password(store, a.username)}")
        elif a.cmd in ("disable", "enable"):
            if not store.update_user(a.username, disabled=int(a.cmd == "disable")):
                raise ValueError(f"{a.username} хэрэглэгч олдсонгүй")
            if a.cmd == "disable":
                store.delete_sessions(username=a.username)
            print(f"{a.username}: {'идэвхгүй' if a.cmd == 'disable' else 'идэвхтэй'}")
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
