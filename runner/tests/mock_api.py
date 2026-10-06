#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_api — in-memory stand-in for the FilmBam V2 worker API (v2/README.md), for tests.

Implements the worker routes with the SAME reply shapes as the real Worker (v2/src/worker.ts):
    POST /api/worker/claim            X-Worker-Secret · oldest pending → producing · 204 when empty
                                      reply: {order} — plus monthly_spent/monthly_cap only when
                                      state.monthly_in_claim is set (the real route does not send
                                      them yet); state.thin_claim strips the brief from the reply
    GET  /api/worker/keys             {FAL_KEY, ELEVENLABS_API_KEY} (state.keys_fail: "500" | "garbage")
    GET  /api/worker/orders/:id       {order}
    POST /api/worker/orders/:id       {status, note?, cost_real?} → done/failed/pending → {order}
    PUT  /api/worker/orders/:id/media binary + Content-Type → {link} (state.media_fail: N first
                                      PUTs answer 503, -1 = always)
plus a stub of the Anthropic Messages API (POST /v1/messages) so the LLM path of
brief_to_config can be exercised offline (state.llm_reply: None → HTTP 500, dict → JSON text
answer, str → verbatim text answer).

State lives in a State object shared with the test (server runs in a daemon thread).
Standalone: python3 runner/tests/mock_api.py --port 8787 [--seed orders.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = "test-worker-secret-0123456789abcdef0123456789abcdef"   # ≥ 32 bytes, like the contract
BRIEF_DEFAULT = ("A woman in her late 20s with dark hair in a tight ponytail and a charcoal running "
                 "jacket ties her neon-coral running shoes on a wet city street at dawn, then sprints "
                 "through a puddle. Cold blue light, cinematic.")


class State:
    def __init__(self, secret: str = SECRET):
        self.secret = secret
        self.orders: dict[str, dict] = {}
        self.keys = {"FAL_KEY": "fal-test-key-not-real-0000", "ELEVENLABS_API_KEY": "eleven-test-key-not-real-0000"}
        self.monthly_cap = 60.0
        self.monthly_spent = 0.0
        self.monthly_in_claim = False                   # True → claim reply carries the two fields
        self.thin_claim = False                         # True → claim reply without the brief
        self.media_fail = 0                             # N → first N PUTs 503 · -1 → always 503
        self.media_failed = 0
        self.keys_fail = None                           # None | "500" | "garbage"
        self.media: dict[str, tuple[str, bytes]] = {}
        self.history: list[tuple[str, str]] = []        # (order id, status) transitions
        self.calls: list[tuple[str, str]] = []          # (method, path)
        self.llm_reply = None                           # None | dict | str
        self.llm_calls: list[dict] = []                 # {"headers": {...}, "body": {...}}
        self.lock = threading.Lock()
        self._n = 0

    def add_order(self, mode="film", length=5, fmt="9:16", q="standard", brief=BRIEF_DEFAULT,
                  id=None, cost=None, status="pending", ts=None) -> dict:
        with self.lock:
            self._n += 1
            oid = id or f"fbtest{self._n:03d}"
            order = {"id": oid, "user_id": "u_test", "ts": ts or int(time.time() * 1000) + self._n,
                     "mode": mode, "len": length, "fmt": fmt, "q": q, "price": 9.9,
                     "cost": cost if cost is not None else 4.5, "brief": brief, "status": status,
                     "note": None, "link": None, "file": None, "ts_prod": None, "ts_done": None,
                     "cost_real": None, "runner": None}
            self.orders[oid] = order
            self.history.append((oid, status))
            return order


class Handler(BaseHTTPRequestHandler):
    server_version = "filmbam-mock/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # quiet unless asked
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[mock] " + fmt % args + "\n")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, status: int, data=None) -> None:
        raw = b"" if data is None else json.dumps(data).encode("utf-8")
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _fail(self, status: int, code: str, msg: str) -> None:
        self._json(status, {"error": msg, "code": code})

    def _raw(self, status: int, raw: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self) -> bool:
        if self.headers.get("X-Worker-Secret") != self.state.secret:
            self._fail(401, "unauthorized", "bad worker secret")
            return False
        return True

    def _base(self) -> str:
        return f"http://{self.headers.get('Host') or 'localhost'}"

    # -- routes -------------------------------------------------------------
    def do_POST(self):
        self.state.calls.append(("POST", self.path))
        if self.path == "/api/worker/claim":
            return self._claim()
        m = re.fullmatch(r"/api/worker/orders/([A-Za-z0-9_-]+)", self.path)
        if m:
            return self._status(m.group(1))
        if self.path == "/v1/messages":
            return self._llm()
        self._fail(404, "not_found", "no such route")

    def do_GET(self):
        self.state.calls.append(("GET", self.path))
        if self.path == "/api/worker/keys":
            if not self._auth():
                return
            if self.state.keys_fail == "500":
                return self._fail(500, "keys_unavailable", "secrets store hiccup")
            if self.state.keys_fail == "garbage":
                return self._raw(200, b"<html>not json</html>", "text/html")
            return self._json(200, self.state.keys)
        m = re.fullmatch(r"/api/worker/orders/([A-Za-z0-9_-]+)", self.path)
        if m:
            if not self._auth():
                return
            order = self.state.orders.get(m.group(1))
            return self._json(200, {"order": order}) if order else self._fail(404, "not_found", "no such order")
        if self.path == "/__state":                    # test helper
            return self._json(200, {"orders": self.state.orders, "history": self.state.history,
                                    "media": {k: [v[0], len(v[1])] for k, v in self.state.media.items()}})
        self._fail(404, "not_found", "no such route")

    def do_PUT(self):
        self.state.calls.append(("PUT", self.path))
        m = re.fullmatch(r"/api/worker/orders/([A-Za-z0-9_-]+)/media", self.path)
        if not m:
            return self._fail(404, "not_found", "no such route")
        if not self._auth():
            return
        oid = m.group(1)
        order = self.state.orders.get(oid)
        if not order:
            return self._fail(404, "not_found", "no such order")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        data = self._body()
        if ctype not in ("video/mp4", "image/png"):
            return self._fail(415, "bad_type", "Content-Type must be video/mp4 or image/png")
        if not data:
            return self._fail(400, "empty", "empty body")
        with self.state.lock:                          # body already read: the connection stays clean
            if self.state.media_fail < 0 or self.state.media_failed < self.state.media_fail:
                self.state.media_failed += 1
                return self._fail(503, "storage_unavailable", "R2 hiccup — retry")
        fname = "final.mp4" if ctype == "video/mp4" else "storyboard.png"
        with self.state.lock:
            self.state.media[oid] = (ctype, data)
            order["file"] = fname
            order["link"] = f"{self._base()}/media/{oid}/{fname}"
        self._json(200, {"link": order["link"]})

    def _claim(self):
        if not self._auth():
            return
        body = self._body()
        runner = None
        if body:
            try:
                runner = (json.loads(body) or {}).get("runner")
            except ValueError:
                pass
        with self.state.lock:
            pending = sorted((o for o in self.state.orders.values() if o["status"] == "pending"),
                             key=lambda o: o["ts"])
            if not pending:
                return self._json(204)
            order = pending[0]
            order["status"] = "producing"
            order["ts_prod"] = int(time.time() * 1000)
            order["runner"] = runner
            self.state.history.append((order["id"], "producing"))
            shown = {k: v for k, v in order.items() if k != "brief"} if self.state.thin_claim else dict(order)
            reply = {"order": shown}
            if self.state.monthly_in_claim:
                reply.update(monthly_spent=self.state.monthly_spent, monthly_cap=self.state.monthly_cap)
        self._json(200, reply)

    def _status(self, oid: str):
        if not self._auth():
            return
        order = self.state.orders.get(oid)
        if not order:
            return self._fail(404, "not_found", "no such order")
        try:
            body = json.loads(self._body() or b"{}")
        except ValueError:
            return self._fail(400, "bad_json", "invalid JSON")
        status = body.get("status")
        if status not in ("done", "failed", "pending"):
            return self._fail(400, "bad_status", "status must be done/failed/pending")
        with self.state.lock:
            order["status"] = status
            if "note" in body:
                order["note"] = body["note"]
            if "cost_real" in body:
                order["cost_real"] = body["cost_real"]
            if status == "done":
                order["ts_done"] = int(time.time() * 1000)
            if status == "pending":
                order["ts_prod"] = None
                order["runner"] = None
            self.state.history.append((oid, status))
        self._json(200, {"order": order})

    def _llm(self):
        """Anthropic Messages API stub."""
        try:
            body = json.loads(self._body() or b"{}")
        except ValueError:
            body = {}
        self.state.llm_calls.append({"headers": {k.lower(): ("<set>" if k.lower() == "x-api-key" else v)
                                                 for k, v in self.headers.items()},
                                     "body": body})
        if not self.headers.get("x-api-key") or not self.headers.get("anthropic-version"):
            return self._json(401, {"type": "error", "error": {"type": "authentication_error",
                                                               "message": "missing headers"}})
        reply = self.state.llm_reply
        if reply is None:
            return self._json(500, {"type": "error", "error": {"type": "api_error", "message": "boom"}})
        text = reply if isinstance(reply, str) else json.dumps(reply)
        self._json(200, {"id": "msg_mock", "type": "message", "role": "assistant",
                         "model": body.get("model"), "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": text}],
                         "usage": {"input_tokens": 1, "output_tokens": 1}})


class Server:
    def __init__(self, port: int = 0, state: State | None = None, host: str = "127.0.0.1", verbose=False):
        self.state = state or State()
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.httpd.verbose = verbose   # type: ignore[attr-defined]
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def start(port: int = 0, state: State | None = None, **kw) -> Server:
    return Server(port=port, state=state, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FilmBam V2 mock worker API")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--seed", help="JSON file: list of orders or {\"orders\": [...]}")
    ap.add_argument("--secret", default=SECRET)
    args = ap.parse_args(argv)
    state = State(secret=args.secret)
    if args.seed:
        with open(args.seed, encoding="utf-8") as f:
            data = json.load(f)
        for o in (data.get("orders") if isinstance(data, dict) else data) or []:
            state.add_order(mode=o.get("mode", "film"), length=int(o.get("len", 5)), fmt=o.get("fmt", "9:16"),
                            q=o.get("q", "standard"), brief=o.get("brief", BRIEF_DEFAULT), id=o.get("id"))
    srv = start(port=args.port, state=state, verbose=True)
    print(f"mock FilmBam API on {srv.url} · secret {args.secret[:4]}… · {len(state.orders)} order(s)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
