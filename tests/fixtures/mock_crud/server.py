"""CRUD-тэй mock SPA (flow тестэд): Мастер дата / Бүртгэл / Харилцагч, admin + viewer role."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
PASSWORD = "Secr3t-Pass!"
USERS = {"admin": "admin", "viewer": "viewer"}      # user -> role


class CrudState:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 1
        # Тестийн бус "бодит" бичлэг — flow үүнийг хэзээ ч засах/устгах ёсгүй
        self.customers = [{"id": 1, "code": "C-0001", "name": "Бодит харилцагч ХХК", "reg": "1234567",
                           "phone": "70001111"}]
        self.logins: dict[str, int] = {}
        self.record_opened = 0      # lookup-ийн мөрийн сумыг (бичлэг нээх) дарсан тоо — 0 байх ёстой
        self.untagged_touched = 0   # tag-гүй (бодит) бичлэг засагдсан/устгагдсан тоо — 0 байх ёстой
        self.created = self.updated = self.deleted = 0
        # Бодит SmartApp шиг байнга нээлттэй (long-poll) хүсэлт — сүлжээ хэзээ ч «тайван» болохгүй
        self.longpoll = False
        self.crash_menu = False     # «Робот» цэс (Blazor уналт → апп эвдрэх) нэмэх
        self.rich = False           # «Ажил» цэс (бичих → тоглуулахын хүнд тохиолдлууд)
        # Автомат CRUD-ийн сайжруулалт: мастер өгөгдөл (Ажилтан → Чөлөө), DateEdit-ийг шошгоор таних,
        # засварт талбар алга болох bug, validation-гүй хоосон хадгалалт, алдаа гарсан ч хадгалагдах
        self.deps = False
        # E2E (Үе 1): «Борлуулалт» баримт (ноорог → батлах → цуцлах) + «Гүйлгээ баланс» тайлан (зөвхөн батлагдсан)
        self.e2e = False
        self.jobs_created = self.jobs_deleted = 0
        self.stopping = threading.Event()

    def next_code(self) -> str:
        return f"C-{self.seq + 1:04d}"


def make_handler(state: CrudState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj, ctype: str = "application/json") -> None:
            body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/customers":
                q = (parse_qs(u.query).get("q") or [""])[0].lower()
                with state.lock:
                    rows = [c for c in state.customers
                            if not q or any(q in str(v).lower() for v in c.values())]
                return self._send(200, rows)
            if u.path in ("/api/jobs-created", "/api/jobs-deleted"):
                with state.lock:
                    if u.path.endswith("created"):
                        state.jobs_created += 1
                    else:
                        state.jobs_deleted += 1
                return self._send(200, {"ok": 1})
            if u.path == "/api/slow":
                import time
                time.sleep(int((parse_qs(u.query).get("ms") or ["500"])[0]) / 1000)
                return self._send(200, {"ok": 1})
            if u.path == "/api/poll":
                if not state.longpoll:
                    return self._send(204, b"")
                state.stopping.wait(20)
                return self._send(200, {"tick": 1})
            if u.path == "/api/next-code":
                return self._send(200, {"code": state.next_code()})
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            html = (HERE / "index.html").read_bytes()
            extra = ""
            if state.crash_menu:
                extra += "{name: 'Робот', href: '/md/robot', kind: 'robot'},"
                extra += "{name: 'Таймер', href: '/md/timerlist', kind: 'timer'},"
                extra += "{name: 'Тасалдал', href: '/md/outagelist', kind: 'outage'},"
                extra += "{name: 'Насжилтын интервалын жагсаалт', href: '/md/aliaslist', kind: 'alias'},"
                extra += "{name: 'Хөрөнгийн эхний үлдэгдэл', href: '/md/openingbalance', kind: 'dform'},"
                extra += "{name: 'Ажилтны жагсаалт', href: '/md/employees', kind: 'md'},"
                extra += "{name: 'Тодорхойлолтын загвар', href: '/md/deftemplate', kind: 'dbset'},"
            if state.rich:
                extra += "{name: 'Ажил', href: '/md/jobs', kind: 'jobs'},"
                extra += "{name: 'Нэхэмжлэх', href: '/md/invoice', kind: 'invoice'},"
                extra += "{name: 'Үйл ажиллагааны загвар', href: '/md/act', kind: 'act'},"
            if state.deps:
                extra += "{name: 'Ажилтан', href: '/md/emp', kind: 'emp'},"
                extra += "{name: 'Чөлөө', href: '/md/leave', kind: 'leave'},"
                extra += "{name: 'Тэмдэглэл', href: '/md/note', kind: 'note'},"
                extra += "{name: 'Төсөл', href: '/md/proj', kind: 'proj'},"
                extra += "{name: 'Түгжээ', href: '/md/lock', kind: 'lock'},"
                extra += "{name: 'Давтамж', href: '/md/sched', kind: 'sched'},"
                extra += "{name: 'И-баримт', href: '/md/ebar', kind: 'ebar'},"
                extra += "{name: 'Хот', href: '/md/city', kind: 'city'},"
                extra += "{name: 'Удаан жагсаалт', href: '/md/slow', kind: 'slow'},"
                extra += "{name: 'Шүүлттэй жагсаалт', href: '/md/manual', kind: 'manual'},"
                extra += "{name: 'Үл харагдах жагсаалт', href: '/md/ghost', kind: 'ghost'},"
            if state.e2e:
                extra += "{name: 'Борлуулалт', href: '/md/sale', kind: 'sale'},"
                extra += "{name: 'Гүйлгээ баланс', href: '/rv/trialbalance', kind: 'tb'},"
                extra += "{name: 'Дансны жагсаалт', href: '/md/acclist', kind: 'acclist'},"
                extra += "{name: 'Дансны бүртгэл', href: '/md/accm', kind: 'accm'},"
            html = html.replace(b"/*EXTRA_ITEMS*/", extra.encode())
            return self._send(200, html, "text/html; charset=utf-8")

        def do_POST(self):
            p = urlparse(self.path).path
            d = self._json()
            if p == "/api/record-opened":
                state.record_opened += 1
                return self._send(200, {})
            if p == "/api/untagged-touched":
                state.untagged_touched += 1
                return self._send(200, {})
            if p == "/api/login":
                user, pwd = d.get("user", ""), d.get("pwd", "")
                if user in USERS and pwd == PASSWORD:
                    state.logins[user] = state.logins.get(user, 0) + 1
                    return self._send(200, {"role": USERS[user]})
                return self._send(401, {"error": "Нэвтрэх нэр эсвэл нууц үг буруу"})
            if p == "/api/customers":
                if d.get("reg") == "ERR500":
                    return self._send(500, {"error": "Invalid column name 'RegNo'."})
                with state.lock:
                    state.seq += 1
                    c = {"id": state.seq, "code": f"C-{state.seq:04d}", "name": d.get("name", ""),
                         "reg": d.get("reg", ""), "phone": d.get("phone", "")}
                    state.customers.append(c)
                    state.created += 1
                return self._send(200, c)
            return self._send(404, {})

        def do_PUT(self):
            p = urlparse(self.path).path
            if p.startswith("/api/customers/"):
                cid = int(p.rsplit("/", 1)[1])
                d = self._json()
                with state.lock:
                    for c in state.customers:
                        if c["id"] == cid:
                            c.update({k: d[k] for k in ("name", "reg", "phone") if k in d})
                            state.updated += 1
                            return self._send(200, c)
            return self._send(404, {})

        def do_DELETE(self):
            p = urlparse(self.path).path
            if p.startswith("/api/customers/"):
                cid = int(p.rsplit("/", 1)[1])
                with state.lock:
                    n = len(state.customers)
                    state.customers = [c for c in state.customers if c["id"] != cid]
                    if len(state.customers) < n:
                        state.deleted += 1
                        return self._send(200, {"ok": True})
            return self._send(404, {})

    return H


class CrudServer:
    def __init__(self, port: int = 0, longpoll: bool = False, crash_menu: bool = False, rich: bool = False):
        self.state = CrudState()
        self.state.longpoll = longpoll
        self.state.crash_menu = crash_menu
        self.state.rich = rich
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.httpd.daemon_threads = True
        self.httpd.block_on_close = False
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.state.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    with CrudServer(8766) as s:
        print(f"Mock CRUD site: {s.url}")
        s.thread.join()
