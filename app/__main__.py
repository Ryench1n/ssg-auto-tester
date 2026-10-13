"""python -m app [--host 127.0.0.1] [--port 8000]

Run бүр хөтчийг харагдах (headed) горимд нээж, хэрэглэгч гараар нэвтэрнэ."""
import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="SSG Auto Tester")
    ap.add_argument("--host", default=os.getenv("SSG_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("SSG_PORT", "8000")))
    # Хуучин команд эвдрэхгүйн тулд хүлээн авна — гар нэвтрэлтийн улмаас хөтөч үргэлж харагдана.
    ap.add_argument("--headed", action="store_true", help="(хуучирсан — хөтөч үргэлж харагдана)")
    a = ap.parse_args()
    print(f"SSG Auto Tester → http://{a.host}:{a.port}")
    uvicorn.run("app.api:app", host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
