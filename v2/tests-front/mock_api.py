#!/usr/bin/env python3
"""Mock of the FilmBam V2 API for testing the front end WITHOUT Worker/D1/R2.

Mirrors the real Worker (v2/src): same routes, same codes and the SAME response shapes:
  POST /api/session {code}      → fb_sid cookie (401 `bad_code` if the code is wrong)
  GET  /api/me                  → {id, role, limits:{perDay, usedToday}} (401 without a session)
  GET  /api/catalog             → {menu, cinema, fmts, looks, linkDays, caps} (public)
  GET  /api/orders[?all=1]      → {orders:[publicOrder…]} (owner + all=1: everything, with costs/runner)
  POST /api/orders {mode,len,fmt,q,brief} → 201 {order} · 429 `daily_limit` · 429 `monthly_cap`
                                   · 400 validation (src/orders.ts)
  GET  /api/admin/ledger        → {month, spent:{real,reserved,total}, remaining, caps, orders, ledger}
                                   (403 `owner_only` for non-owners)
  GET  /media/<id>/<file>       → bytes only for the order's owner (or the owner role); 404 after expiry
publicOrder (src/orders.ts): `expires_at` = ts_done + linkDays, `expired` and `link` = "" after
expiry; `user_id`/`cost`/`cost_real`/`runner`/`file` ONLY for the owner. No response carries `instant`
(the contract has no such field); the mock's `instant` config is an optional hook to test the
"instant" copy in case the API ever sends `instant:true` on GET /api/me.

Test control routes (exist ONLY in the mock):
  POST /__mock/orders/<id> {fields}  → patches an order (e.g. status done + link + ts_done)
  POST /__mock/config {fields}       → instant, daily_limit, monthly_cap, month_spent, link_days,
                                       orders_delay_ms (delays GET /api/orders: stale-response test)
  POST /__mock/reset                 → resets everything
  GET  /__mock/state                 → state dump

Usage: python3 v2/tests-front/mock_api.py [port]   (default 8787)
Codes: ACCESS_CODE=open-sesame · OWNER_CODE=owner-sesame (environment variables override).
"""
import calendar
import json
import os
import secrets
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PUBLIC = Path(__file__).resolve().parent.parent / "public"
ACCESS_CODE = os.environ.get("ACCESS_CODE", "open-sesame")
OWNER_CODE = os.environ.get("OWNER_CODE", "owner-sesame")

# Catalogue identical to the V1 console / V2 front end / src/catalog.ts
MENU = {
    "film": [{"v": 5, "l": "5 s", "price": 5.9, "cost": 2.7}, {"v": 10, "l": "10 s", "price": 9.9, "cost": 4.5},
             {"v": 20, "l": "20 s", "price": 17.9, "cost": 8.3}, {"v": 30, "l": "30 s", "price": 24.9, "cost": 12}],
    "story": [{"v": 6, "l": "6 frames", "price": 2.9, "cost": 1.1}, {"v": 12, "l": "12 frames", "price": 4.9, "cost": 1.6}],
}
FMTS = ("9:16", "16:9", "1:1")
LOOKS = ("standard", "cinema")
CINEMA = 1.5
CAP_FILM, CAP_STORY = 30.0, 5.0
DAY_MS = 864e5


def p90(n):
    return max(0.9, round(n - 0.9) + 0.9)


def now_ms():
    return int(time.time() * 1000)


def month_start(ts_ms):   # monthStart() from src/util.ts: first day of the month, UTC
    t = time.gmtime(ts_ms / 1000)
    return calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0)) * 1000


def month_label(ts_ms):
    return time.strftime("%Y-%m", time.gmtime(ts_ms / 1000))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.users = {}       # id -> {id, role}
        self.sessions = {}    # sid -> user id
        self.orders = []      # newest first ("database" rows: every schema field)
        self.ledger = []      # {id, order_id, ts, usd, status}
        self.config = {"instant": False, "daily_limit": 3, "monthly_cap": 60.0, "month_spent": 0.0,
                       "link_days": 3, "orders_delay_ms": 0}
        self.seq = 0

    def new_id(self, prefix):
        self.seq += 1
        return prefix + format(now_ms() + self.seq, "x")[-8:] + format(self.seq, "x")

    # MONTH_SPEND_SQL from src/limits.ts: cost_real when present; otherwise `cost` for
    # pending/producing/done; failed without cost_real = 0. `month_spent` = test-ONLY baseline.
    def month_spend(self):
        start = month_start(now_ms())
        real = reserved = 0.0
        for o in self.orders:
            if o["ts"] < start:
                continue
            if o.get("cost_real") is not None:
                real += float(o["cost_real"])
            elif o["status"] in ("pending", "producing", "done"):
                reserved += float(o["cost"])
        base = float(self.config["month_spent"])
        return {"real": round(real, 2), "reserved": round(reserved + base, 2), "total": round(real + reserved + base, 2)}

    def public_order(self, o, owner):
        """publicOrder() from src/orders.ts: the view the front end receives."""
        days = self.config["link_days"]
        expires_at = (o["ts_done"] + days * DAY_MS) if o.get("ts_done") else None
        expired = expires_at is not None and now_ms() > expires_at
        out = {"id": o["id"], "ts": o["ts"], "mode": o["mode"], "len": o["len"], "fmt": o["fmt"], "q": o["q"],
               "price": o["price"], "brief": o["brief"], "status": o["status"], "note": o.get("note") or "",
               "link": "" if expired else (o.get("link") or ""), "ts_prod": o.get("ts_prod"),
               "ts_done": o.get("ts_done"), "expires_at": expires_at, "expired": expired}
        if owner:
            out.update({"user_id": o["user_id"], "cost": o["cost"], "cost_real": o.get("cost_real"),
                        "runner": o.get("runner"), "file": o.get("file")})
        return out


STATE = State()


class Handler(SimpleHTTPRequestHandler):
    server_version = "filmbam-mock/2"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(PUBLIC), **kw)

    def log_message(self, fmt, *args):  # quiet unless asked
        if os.environ.get("MOCK_VERBOSE"):
            super().log_message(fmt, *args)

    # ── helpers ──
    def _json(self, status, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status, code, msg):
        self._json(status, {"error": msg, "code": code})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            return None

    def _user(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fb_sid":
                uid = STATE.sessions.get(v)
                return STATE.users.get(uid)
        return None

    def _used_today(self, uid):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return sum(1 for o in STATE.orders
                   if o["user_id"] == uid and time.strftime("%Y-%m-%d", time.gmtime(o["ts"] / 1000)) == day)

    def _caps(self):
        c = STATE.config
        return {"perOrderFilm": CAP_FILM, "perOrderStory": CAP_STORY, "month": float(c["monthly_cap"]),
                "perDay": c["daily_limit"], "linkDays": c["link_days"], "orphanHours": 2}

    # ── routing ──
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/me":
            return self.api_me()
        if path == "/api/catalog":
            return self.api_catalog()
        if path == "/api/orders":
            return self.api_orders_get()
        if path == "/api/admin/ledger":
            return self.api_ledger()
        if path.startswith("/media/"):
            return self.media()
        if path == "/__mock/state":
            with STATE.lock:
                return self._json(200, {"users": STATE.users, "orders": STATE.orders, "ledger": STATE.ledger,
                                        "config": STATE.config})
        if path.startswith("/api/") or path.startswith("/__mock/"):
            return self._err(404, "not_found", "No such route")
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/session":
            return self.api_session()
        if path == "/api/orders":
            return self.api_orders_post()
        if path == "/__mock/reset":
            with STATE.lock:
                STATE.reset()
            return self._json(200, {"ok": True})
        if path == "/__mock/config":
            body = self._body() or {}
            with STATE.lock:
                STATE.config.update(body)
                return self._json(200, STATE.config)
        if path.startswith("/__mock/orders/"):
            oid = path.rsplit("/", 1)[1]
            body = self._body() or {}
            with STATE.lock:
                for o in STATE.orders:
                    if o["id"] == oid:
                        o.update(body)
                        return self._json(200, o)
            return self._err(404, "not_found", "No such order")
        return self._err(404, "not_found", "No such route")

    # ── API ──
    def api_session(self):
        body = self._body()
        code = (body or {}).get("code")
        if not isinstance(code, str):
            return self._err(400, "bad_json", 'Body must be JSON: {"code": "..."}')
        if secrets.compare_digest(code, OWNER_CODE):
            role = "owner"
        elif secrets.compare_digest(code, ACCESS_CODE):
            role = "user"
        else:
            return self._err(401, "bad_code", "That code didn’t open the door. Check it and try again.")
        with STATE.lock:
            uid = "u_" + role + "_" + secrets.token_hex(3)
            STATE.users[uid] = {"id": uid, "role": role}
            sid = secrets.token_urlsafe(24)
            STATE.sessions[sid] = uid
            me = self._me(uid)
        # no `Secure` because the mock runs on http://localhost
        self._json(200, me, {"Set-Cookie": "fb_sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=7776000" % sid})

    def _me(self, uid):
        u = STATE.users[uid]
        per_day = None if u["role"] == "owner" else STATE.config["daily_limit"]
        me = {"id": uid, "role": u["role"], "limits": {"perDay": per_day, "usedToday": self._used_today(uid)}}
        if STATE.config["instant"]:   # optional hook (outside the contract): the real API does NOT send this
            me["instant"] = True
        return me

    def api_me(self):
        u = self._user()
        if not u:
            return self._err(401, "no_session", "Enter the access code first")
        with STATE.lock:
            self._json(200, self._me(u["id"]))

    def api_catalog(self):
        with STATE.lock:
            self._json(200, {"menu": MENU, "cinema": CINEMA, "fmts": list(FMTS), "looks": list(LOOKS),
                             "linkDays": STATE.config["link_days"], "caps": self._caps()})

    def api_orders_get(self):
        u = self._user()
        if not u:
            return self._err(401, "no_session", "Enter the access code first")
        q = parse_qs(urlsplit(self.path).query)
        owner = u["role"] == "owner"
        want_all = owner and q.get("all", ["0"])[0] == "1"
        with STATE.lock:
            rows = [STATE.public_order(o, owner) for o in STATE.orders if want_all or o["user_id"] == u["id"]]
        # stale-response test: the list snapshot is taken NOW and only arrives after the delay
        delay = STATE.config.get("orders_delay_ms") or 0
        if delay:
            time.sleep(delay / 1000.0)
        self._json(200, {"orders": rows})

    def api_orders_post(self):
        u = self._user()
        if not u:
            return self._err(401, "no_session", "Enter the access code first")
        b = self._body()
        if not isinstance(b, dict):
            return self._err(400, "bad_json", "Body must be JSON")
        mode, fmt, q = b.get("mode"), b.get("fmt"), b.get("q", "standard")
        if mode not in MENU:
            return self._err(400, "bad_mode", 'mode must be "film" or "story"')
        try:
            ln = int(b.get("len"))
        except (TypeError, ValueError):
            ln = None
        item = next((i for i in MENU[mode] if i["v"] == ln), None)
        if not item:
            return self._err(400, "bad_len", "len is not in the %s catalog" % mode)
        if fmt not in FMTS:
            return self._err(400, "bad_fmt", "fmt must be 9:16, 16:9 or 1:1")
        if q not in LOOKS:
            return self._err(400, "bad_q", 'q must be "standard" or "cinema"')
        brief = b.get("brief")
        brief = brief.strip() if isinstance(brief, str) else ""
        if not brief:
            return self._err(400, "brief_empty", "Type the brief first — one sentence is enough")
        if len(brief) > 600:
            return self._err(400, "brief_too_long", "Brief must be ≤ 600 characters")
        if "<" in brief or ">" in brief:
            return self._err(400, "brief_html", "Brief cannot contain < or >")
        mult = CINEMA if q == "cinema" else 1
        price, cost = p90(item["price"] * mult), round(item["cost"] * mult, 2)
        if cost > (CAP_FILM if mode == "film" else CAP_STORY):
            return self._err(422, "over_cap", "This item exceeds the per-order budget")
        with STATE.lock:
            if u["role"] != "owner" and self._used_today(u["id"]) >= STATE.config["daily_limit"]:
                return self._err(429, "daily_limit",
                                 "Daily limit reached (%d per day) — come back tomorrow" % STATE.config["daily_limit"])
            if STATE.month_spend()["total"] + cost > float(STATE.config["monthly_cap"]):
                return self._err(429, "monthly_cap", "Monthly budget reached — new films open again next month")
            ts = now_ms()
            o = {"id": STATE.new_id("fb"), "user_id": u["id"], "ts": ts, "mode": mode, "len": ln, "fmt": fmt,
                 "q": q, "price": price, "cost": cost, "brief": brief, "status": "pending", "note": "",
                 "link": "", "file": "", "ts_prod": None, "ts_done": None, "cost_real": None, "runner": ""}
            STATE.orders.insert(0, o)
            STATE.ledger.insert(0, {"id": len(STATE.ledger) + 1, "order_id": o["id"], "ts": ts, "usd": cost,
                                    "status": "reserved"})
            self._json(201, {"order": STATE.public_order(o, u["role"] == "owner")})

    def api_ledger(self):
        u = self._user()
        if not u:
            return self._err(401, "no_session", "Enter the access code first")
        if u["role"] != "owner":
            return self._err(403, "owner_only", "Owner only")
        with STATE.lock:
            ts = now_ms()
            start = month_start(ts)
            spent = STATE.month_spend()
            counts = {"pending": 0, "producing": 0, "done": 0, "failed": 0}
            for o in STATE.orders:
                if o["ts"] >= start:
                    counts[o["status"]] = counts.get(o["status"], 0) + 1
            self._json(200, {"month": month_label(ts), "spent": spent,
                             "remaining": round(max(0.0, float(STATE.config["monthly_cap"]) - spent["total"]), 2),
                             "caps": self._caps(), "orders": counts,
                             "ledger": [l for l in STATE.ledger if l["ts"] >= start][:200]})

    def media(self):
        u = self._user()
        if not u:
            return self._err(401, "no_session", "Enter the access code first")
        parts = urlsplit(self.path).path.split("/")
        oid = parts[2] if len(parts) > 2 else ""
        with STATE.lock:
            o = next((x for x in STATE.orders if x["id"] == oid), None)
            if not o or (o["user_id"] != u["id"] and u["role"] != "owner"):
                return self._err(404, "not_found", "Not found")
            days = STATE.config["link_days"]
            if o.get("ts_done") and now_ms() > o["ts_done"] + days * DAY_MS:
                return self._err(404, "expired", "This link expired (%d days after delivery)" % days)
        body = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64   # fake mp4 header
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port=0):
    """Starts the server in a thread; returns (server, base_url)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    srv, url = serve(port)
    print("FilmBam mock API on %s (public: %s)  codes: %s / owner %s" % (url, PUBLIC, ACCESS_CODE, OWNER_CODE))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
