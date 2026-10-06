#!/usr/bin/env python3
"""End-to-end test of the V2 front end (v2/public) with Playwright + chromium against the mock.

Usage: python3 v2/tests-front/test_front.py          (exits with code 1 if anything fails)
       SHOTS_DIR=/tmp/shots python3 v2/tests-front/test_front.py   → also saves screenshots
Requires: pip `playwright` + chromium in $PLAYWRIGHT_BROWSERS_PATH (or /opt/pw-browsers/chromium).

Covers: gate → console with the right code (401 = shake + message) · conservative footer by
default (the API sends no `instant`) and "instant" copy only with `instant:true` on /api/me · BAM
creates an IN QUEUE card · an in-flight poll does NOT erase the new card (stale response ignored) ·
polling (20 s) flips to READY when the mock changes the status · absolute link on the SAME origin
(http in dev) shows Download/"Copy my link" + hint · failed = NEEDS ATTENTION + note · announces
only what changed (aria-live outside the list) · polling stops · expired (publicOrder: expired:true,
link "") = EXPIRED chip + "Link expired" with the catalogue's days · API errors (429 daily_limit /
429 monthly_cap / 400) in the notice line with the server message · owner: budget strip (ledger),
"All films", the person's id/cost/runner · 390 px without horizontal overflow · zero console errors.
"""
import os
import sys
import time
import json
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_api  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

RESULTS = []
CONSOLE = []      # (page-label, text) of console errors that count
NET = []          # responses >= 400 outside /api (broken asset)
DAY_MS = 864e5


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("  — " + str(detail)) if detail and not cond else ""))


def mock(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def mock_state(base):
    with urllib.request.urlopen(base + "/__mock/state") as r:
        return json.loads(r.read().decode())


def shot(page, name):
    """Optional screenshots for visual review: SHOTS_DIR=/some/dir."""
    d = os.environ.get("SHOTS_DIR")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / (name + ".png")), full_page=True)


def card(oid, sub=""):
    return '.order[data-id="%s"] %s' % (oid, sub)


def new_page(ctx, label):
    page = ctx.new_page()
    # Google Fonts is unreachable in the sandbox: answer with empty CSS (the page uses its fallback)
    page.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(status=200, content_type="text/css", body=""))
    page.route("https://fonts.gstatic.com/**", lambda route: route.fulfill(status=200, content_type="font/woff2", body=b""))

    def on_console(msg):
        if msg.type != "error":
            return
        url = (msg.location or {}).get("url", "") if isinstance(msg.location, dict) else ""
        # 4xx from /api is expected contract behaviour (401 → gate, 429/400 → notice)
        if "/api/" in url and "status of 4" in msg.text:
            return
        CONSOLE.append((label, msg.text + " @ " + url))

    page.on("console", on_console)
    page.on("pageerror", lambda e: CONSOLE.append((label, "pageerror: " + str(e))))
    page.on("response", lambda r: NET.append((label, r.status, r.url))
            if r.status >= 400 and "/api/" not in r.url and "/__mock/" not in r.url else None)
    return page


def login(page, base, code):
    page.goto(base + "/")
    page.wait_for_selector("#code")
    page.fill("#code", code)
    page.click("#enter")
    page.wait_for_selector("#go")


def overflow(page):
    return page.evaluate("""() => {
      const w = window.innerWidth, doc = document.documentElement;
      const wide = [...document.querySelectorAll('.stage *')].filter(el => {
        const r = el.getBoundingClientRect(); return r.width && r.right > w + 0.5; });
      return {scrollWidth: doc.scrollWidth, bodyScroll: document.body.scrollWidth, inner: w,
              wide: wide.slice(0, 5).map(e => e.tagName + '.' + e.className)};
    }""")


def run():
    srv, base = mock_api.serve(0)
    print("mock API:", base)
    with sync_playwright() as p:
        # pip playwright may expect another chromium build; the environment's binary wins
        exe = Path("/opt/pw-browsers/chromium")
        browser = p.chromium.launch(executable_path=str(exe)) if exe.exists() else p.chromium.launch()

        # ── 1. desktop: gate, wrong code, right code ──
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  permissions=["clipboard-read", "clipboard-write"])
        page = new_page(ctx, "desktop")
        page.goto(base + "/")
        page.wait_for_selector("#code")
        check("gate first: access-code input shown, no console", page.query_selector("#go") is None)
        check("gate copy: headline", "One sentence in." in page.inner_text("h1") and "A film out." in page.inner_text("h1"))
        check("gate copy: FilmBam wordmark", page.inner_text(".brand").replace("\n", "") == "FilmBam")
        check("gate copy: link days from GET /api/catalog (3)", "links live 3 days" in page.inner_text("#gate .foot"))
        page.fill("#code", "wrong-code")
        page.click("#enter")
        page.wait_for_selector("#gate.shake")
        page.wait_for_selector(".notice.on")
        check("wrong code: 401 → shake + server message", "didn’t open the door" in page.inner_text("#notice"))
        check("wrong code: still on the gate", page.query_selector("#go") is None and page.query_selector("#code") is not None)
        shot(page, "gate-wrong-code")
        page.fill("#code", "open-sesame")
        page.click("#enter")
        page.wait_for_selector("#go")
        check("right code: console shown", page.query_selector("#code") is None)
        check("console copy: headline", page.inner_text("h1").replace("\n", " ") == "One sentence in. Bam!! A film out.")
        check("console copy: catalogue default price $9.90 (10 s)", page.inner_text(".price") == "$9.90")
        check("console copy: menu lengths", [b.inner_text() for b in page.query_selector_all("[data-len]")] == ["5 s", "10 s", "20 s", "30 s"])
        check("console copy: foot is conservative when the API sends no `instant` (contract)",
              page.inner_text("#footmsg") == "Studio wakes within the hour · your film lands below within the hour", page.inner_text("#footmsg"))
        check("console copy: BAM! button", page.inner_text("#go") == "BAM!")
        check("console: empty state", "Nothing in orbit yet" in page.inner_text("#orders"))
        check("owner toggle + ledger hidden for a normal user", page.query_selector("#all") is None and page.query_selector("#ledger") is None)
        check("a11y: no aria-live on the whole list; a status line exists",
              page.get_attribute("#orders", "aria-live") is None and page.get_attribute("#live", "aria-live") == "polite")
        # price follows the catalogue: 30 s Cinema = 24.9*1.5 → .90
        page.click("[data-len='3']"); page.click("[data-q='cinema']")
        check("price: 30 s Cinema = $36.90", page.inner_text(".price") == "$36.90")
        page.click("[data-mode='story']")
        check("storyboard mode keeps Cinema: 6 frames = $3.90", page.inner_text(".price") == "$3.90" and "A board out." in page.inner_text("h1"))
        page.click("[data-q='standard']")
        check("storyboard mode: 6 frames Standard = $2.90", page.inner_text(".price") == "$2.90")
        page.click("[data-mode='film']"); page.click("[data-len='1']")
        check("back to film: 10 s Standard = $9.90", page.inner_text(".price") == "$9.90")
        shot(page, "console-empty")

        # ── 2. BAM → card IN QUEUE ──
        page.fill("#prompt", "A barista pours latte art at sunrise — cut to the first sip.")
        page.click("#go")
        page.wait_for_selector(".order .chip.pending")
        c1 = page.query_selector(".order")
        check("BAM: card prepended with IN QUEUE chip", c1.query_selector(".chip").inner_text() == "IN QUEUE")
        check("BAM: card header", c1.query_selector(".what").inner_text() == "10-second film · 9:16")
        check("BAM: rail with 5 stages + labels", len(c1.query_selector_all(".seg")) == 5 and "CASTING" in c1.inner_text() and "DELIVERED" in c1.inner_text())
        check("BAM: textarea cleared, button back", page.input_value("#prompt") == "" and page.inner_text("#go") == "BAM!")
        check("BAM: success notice (conservative)", page.inner_text("#notice") == "Order received — the studio wakes within the hour.", page.inner_text("#notice"))
        st = mock_state(base)
        check("BAM: mock received the order via POST /api/orders (201 {order})", len(st["orders"]) == 1 and st["orders"][0]["brief"].startswith("A barista"))
        oid = st["orders"][0]["id"]
        check("BAM: card shows the API id", oid in c1.query_selector(".id").inner_text())
        check("BAM: no owner-only meta (cost/runner) for a normal user", "cost" not in c1.query_selector(".id").inner_text())

        # ── 3. stale response: a slow in-flight GET /api/orders does not erase the card of a later BAM ──
        mock(base, "/__mock/config", {"orders_delay_ms": 3000})
        t0 = time.time()
        with page.expect_request(lambda r: "/api/orders" in r.url and r.method == "GET", timeout=25000):
            pass                                    # the next poll (≤ 20 s) is held for 3 s by the mock
        page.fill("#prompt", "Second film, sent while a poll is in flight")
        page.click("#go")
        page.wait_for_function("document.querySelectorAll('.order').length === 2")
        page.wait_for_timeout(3600)                  # the stale response arrives now and must be ignored
        briefs = [b.inner_text() for b in page.query_selector_all(".order .brief")]
        check("stale poll: the new card survives the late GET /api/orders (%.0fs)" % (time.time() - t0),
              len(briefs) == 2 and briefs[0].startswith("Second film"), briefs)
        mock(base, "/__mock/config", {"orders_delay_ms": 0})
        oid2 = mock_state(base)["orders"][0]["id"]

        # ── 4. polling: the mock delivers #1 (absolute same-origin http link) and fails #2 ──
        link = base + "/media/%s/final.mp4" % oid       # publicBase(): the request origin
        mock(base, "/__mock/orders/" + oid, {"status": "done", "link": link, "file": "final.mp4", "ts_done": int(time.time() * 1000)})
        mock(base, "/__mock/orders/" + oid2, {"status": "failed", "note": "Budget cap hit — no charge."})
        t1 = time.time()
        page.wait_for_selector(card(oid, ".chip.done"), timeout=30000)
        dt = time.time() - t1
        check("polling: IN QUEUE → READY after the mock flips status (%.1fs, ≤ 20 s cycle)" % dt, page.inner_text(card(oid, ".chip")) == "READY" and dt <= 25)
        check("ready: 'Download the film ↓' with the API's same-origin http link", page.inner_text(card(oid, ".dl")) == "Download the film ↓" and page.get_attribute(card(oid, ".dl"), "href") == link)
        check("ready: 'Copy my link' + not-shareable hint", page.inner_text(card(oid, ".cp")) == "Copy my link" and "own browser" in page.inner_text(card(oid, ".hint")))
        check("ready: all 5 segments lit", len(page.query_selector_all(card(oid, ".seg.lit"))) == 5)
        check("failed: NEEDS ATTENTION chip + note from the API", page.inner_text(card(oid2, ".chip")) == "NEEDS ATTENTION" and page.inner_text(card(oid2, ".note")) == "Budget cap hit — no charge.")
        live = page.text_content("#live")
        check("a11y: status line names only what changed", "is now Ready" in live and "is now Needs attention" in live and live.count("Order ") == 2, live)
        shot(page, "console-ready")
        page.click(card(oid, ".cp"))
        page.wait_for_timeout(300)
        clip = page.evaluate("navigator.clipboard.readText()")
        check("copy link: clipboard has the absolute link", clip == link and page.inner_text(card(oid, ".cp")) == "Copied ✓", clip)
        # nothing active in the queue any more: polling must stop (no GET /api/orders in 22 s)
        hits = []
        page.on("request", lambda r: hits.append(r.url) if "/api/orders" in r.url else None)
        page.wait_for_timeout(22000)
        check("polling stops when nothing is pending/producing", hits == [], hits)

        # ── 5. expired: publicOrder sends expired:true + link "" (catalogue lifetime, 2 days here) ──
        mock(base, "/__mock/config", {"link_days": 2})
        mock(base, "/__mock/orders/" + oid, {"ts_done": int(time.time() * 1000 - 3 * DAY_MS)})
        page.reload()
        page.wait_for_selector(card(oid, ".chip.expired"))
        check("expired: EXPIRED chip, no Download/Copy", page.inner_text(card(oid, ".chip")) == "EXPIRED" and page.query_selector(card(oid, ".dl")) is None and page.query_selector(card(oid, ".cp")) is None)
        check("expired: 'Link expired' note with the catalog's days (2)", page.inner_text(card(oid, ".note")) == "Link expired — links stay live for 2 days.", page.inner_text(card(oid, ".note")))
        check("expired: signature copy follows the catalog too", "Links stay live for 2 days." in page.inner_text(".sig"))
        mock(base, "/__mock/config", {"link_days": 3})
        mock(base, "/__mock/orders/" + oid, {"ts_done": int(time.time() * 1000)})

        # ── 6. reload: the cookie session keeps the console and the list ──
        page.reload()
        page.wait_for_selector("#go")
        page.wait_for_selector(card(oid, ".chip.done"))
        check("reload: cookie session keeps the console + orders", page.query_selector("#code") is None and page.query_selector(card(oid, ".dl")) is not None)

        # ── 7. API errors in the notice line ──
        mock(base, "/__mock/config", {"daily_limit": 1})
        page.fill("#prompt", "Third film of the day")
        page.click("#go")
        page.wait_for_function("document.querySelector('#notice').textContent.includes('Daily limit')")
        check("429 daily_limit: server message in the notice", "Daily limit reached" in page.inner_text("#notice") and page.inner_text("#go") == "BAM!")
        mock(base, "/__mock/config", {"daily_limit": 3, "month_spent": 59.0})
        page.click("#go")
        page.wait_for_function("document.querySelector('#notice').textContent.includes('Monthly budget')")
        check("429 monthly_cap: server message in the notice", "Monthly budget reached" in page.inner_text("#notice"))
        mock(base, "/__mock/config", {"month_spent": 0.0})
        page.fill("#prompt", "<b>html</b> in the brief")
        page.click("#go")
        page.wait_for_function("document.querySelector('#notice').textContent.includes('<')")
        check("400 validation: server message in the notice", "cannot contain" in page.inner_text("#notice"))
        check("errors: no extra card created", len(page.query_selector_all(".order")) == 2)

        # ── 8. instant:true on /api/me → "instant" footer ──
        mock(base, "/__mock/config", {"instant": True})
        page.reload()
        page.wait_for_selector("#go")
        check("instant:true from /api/me: foot copy", page.inner_text("#footmsg") == "BAM starts the studio · your film lands below in ~10 min")
        mock(base, "/__mock/config", {"instant": False})
        ctx.close()

        # ── 9. owner: budget strip + All films + the person's id/cost/runner ──
        mock(base, "/__mock/orders/" + oid, {"cost_real": 4.2, "runner": "gh-actions#a1b2"})
        octx = browser.new_context(viewport={"width": 1280, "height": 900})
        opage = new_page(octx, "owner")
        login(opage, base, "owner-sesame")
        opage.wait_for_selector("#all")
        check("owner: 'All films' toggle shown", opage.inner_text("#all") == "All films")
        check("owner: own list is empty", "Nothing in orbit yet" in opage.inner_text("#orders"))
        led = opage.inner_text("#ledger")
        month = time.strftime("%Y-%m", time.gmtime())
        check("owner: ledger strip = month · spent · remaining · cap (real $4.20 → $55.80 of $60)",
              month in led and "$4.20 spent" in led and "$55.80 left of $60.00/month" in led, led)
        opage.click("#all")
        opage.wait_for_selector(".order .who")
        check("owner: ?all=1 shows other users' films with their id", opage.inner_text(card(oid, ".who")) == st["orders"][0]["user_id"])
        idline = opage.inner_text(card(oid, ".id"))
        check("owner: real cost + runner on the id line", "cost $4.20 real" in idline and "gh-actions#a1b2" in idline, idline)
        check("owner: estimate shown when the runner reported no cost", "cost $4.50 est." in opage.inner_text(card(oid2, ".id")), opage.inner_text(card(oid2, ".id")))
        check("owner: heading flips to 'All films'", opage.text_content("#orders h2") == "All films")
        check("owner: toggle re-enabled after the load", opage.get_attribute("#all", "aria-pressed") == "true" and not opage.is_disabled("#all"))
        shot(opage, "owner-all")
        octx.close()

        # ── 10. mobile 390 px: zero horizontal overflow ──
        mctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        mpage = new_page(mctx, "mobile")
        mpage.goto(base + "/")
        mpage.wait_for_selector("#code")
        g = overflow(mpage)
        check("mobile 390: gate has no horizontal overflow", g["scrollWidth"] <= g["inner"] and g["bodyScroll"] <= g["inner"] and not g["wide"], g)
        mpage.fill("#code", "open-sesame"); mpage.click("#enter"); mpage.wait_for_selector("#go")
        mpage.fill("#prompt", "Averyveryverylongwordwithoutanyspacesthatcouldpushthecardwiderthantheviewportifunhandled and then some more words")
        mpage.click("#go")
        mpage.wait_for_selector(".order .chip.pending")
        c = overflow(mpage)
        check("mobile 390: console + order card have no horizontal overflow", c["scrollWidth"] <= c["inner"] and c["bodyScroll"] <= c["inner"] and not c["wide"], c)
        shot(mpage, "mobile-390")
        mctx.close()

        # owner on mobile: the budget strip and the id/cost/runner line do not overflow either
        octx2 = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        opage2 = new_page(octx2, "owner-mobile")
        login(opage2, base, "owner-sesame")
        opage2.wait_for_selector("#ledger")
        opage2.click("#all")
        opage2.wait_for_selector(".order .who")
        c = overflow(opage2)
        check("mobile 390 owner: ledger strip + All films have no horizontal overflow", c["scrollWidth"] <= c["inner"] and c["bodyScroll"] <= c["inner"] and not c["wide"], c)
        shot(opage2, "owner-mobile-390")
        octx2.close()
        browser.close()
    srv.shutdown()

    check("no console errors / uncaught exceptions on any page", not CONSOLE, CONSOLE)
    check("no broken static assets (>=400 outside /api)", not NET, NET)
    failed = [r for r in RESULTS if not r[1]]
    print("\n%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
