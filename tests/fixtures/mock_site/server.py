"""DevExpress Blazor-тэй ижил DOM бүтэцтэй жижиг mock SPA-ийн сервер (тестэд)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent


SPECULATION_RULES = (b'{"tag":"cf-speed-brain","prefetch":[{"eagerness":"conservative","source":"document",'
                     b'"where":{"and":[{"href_matches":"/*","relative_to":"document"}]}}]}')


class MockState:
    def __init__(self, session_ttl_s: int = 20, speed_brain: bool = False):
        self.session_ttl_s = session_ttl_s
        self.mutations = 0          # READ-ONLY зөрчил (хориотой товч дарагдсан) тоологч
        self.logins = 0
        self.deploy_hits = 0        # эхний дуудлага 503 + msdeploy, дараагийнх нь 200
        # Cloudflare Speed Brain: Chrome-д href бүрийг prefetch хийлгэх дүрэм өгч, `Sec-Purpose: prefetch`
        # хүсэлтэд 503 буцаадаг (бодит testitsm/testaccounting.smartapp.mn дээр ажиглагдсан).
        self.speed_brain = speed_brain
        self.prefetch_hits = 0
        self.initial_tab_closed = 0   # анхны (цэсний бус) таб хаагдсан тоо — 0 байх ёстой


def make_handler(state: MockState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # чимээгүй
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if state.speed_brain and ctype.startswith("text/html"):
                self.send_header("Speculation-Rules", '"/cdn-cgi/speculation"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p.startswith("/_content/"):          # deploy-д хуулагдаагүй static файл
                return self._send(404, b"", "text/css")
            if state.speed_brain:
                if p == "/cdn-cgi/speculation":
                    return self._send(200, SPECULATION_RULES, "application/speculationrules+json")
                if "prefetch" in (self.headers.get("Sec-Purpose", "") + self.headers.get("Purpose", "")).lower():
                    state.prefetch_hits += 1
                    return self._send(503, b"Service Unavailable", "text/plain")
            if p == "/config.js":
                js = f"window.MOCK_TTL_MS = {state.session_ttl_s * 1000};"
                return self._send(200, js.encode(), "application/javascript")
            if p == "/api/initial-closed":
                state.initial_tab_closed += 1
                return self._send(200, b"{}", "application/json")
            if p == "/api/fail":
                return self._send(500, b'{"error":"Invalid column name \'CostCenterId\'."}', "application/json")
            if p == "/api/deploy":
                state.deploy_hits += 1
                if state.deploy_hits == 1:
                    return self._send(503, b"<html>Service Unavailable - msdeploy app_offline.htm</html>",
                                      "text/html")
                return self._send(200, b'{"ok":true}', "application/json")
            if p.startswith("/api/"):
                return self._send(200, b'{"ok":true}', "application/json")
            if p == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")

        def do_POST(self):
            p = self.path.split("?")[0]
            if p == "/api/login":
                state.logins += 1
                return self._send(200, b'{"ok":true}', "application/json")
            if p == "/api/mutate":
                state.mutations += 1
                return self._send(200, b'{"ok":true}', "application/json")
            return self._send(404, b"{}", "application/json")

    return H


class MockServer:
    def __init__(self, session_ttl_s: int = 20, port: int = 0, speed_brain: bool = False):
        self.state = MockState(session_ttl_s, speed_brain)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with MockServer(session_ttl_s=int(sys.argv[2]) if len(sys.argv) > 2 else 300, port=port) as s:
        print(f"Mock site: {s.url}")
        s.thread.join()
