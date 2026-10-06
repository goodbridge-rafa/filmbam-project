"""Browser check of the FREE PROTOTYPE against the real Worker, using the same
`wrangler.demo.toml` that gets deployed.

Everything here is disposable local emulation (workerd + local D1). No Cloudflare account is touched,
no R2 is bound and no paid provider is called. What is proven: the interface is the real one, the
gate stays real, and what the prototype does NOT do is stated on screen and refused by the server.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright


def launch_chromium(p):
    """In CI the workflow installs chromium. In an environment that already ships one (a build
    other than the one pinned by the package), use the existing one instead of downloading another."""
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if not exe:
        found = sorted(Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")).glob("chromium-*/chrome-linux/chrome"))
        exe = str(found[-1]) if found else None
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()

ROOT = Path(__file__).resolve().parents[1]
def free_port():
    """Free port per run: an orphaned dev server from a previous run must not make this one
    talk to the wrong Worker (and the wrong state)."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PREFIX = "/apps/filmbam"                     # must match PUBLIC_BASE_PATH in wrangler.demo.toml
PORT = free_port()
ORIGIN = f"http://127.0.0.1:{PORT}"
BASE = ORIGIN + PREFIX
CONFIG = "wrangler.demo.toml"                  # the real prototype config, not a copy
SECRET = "w" * 48
# Recognisable fake keys: if any of them shows up in a response, the test fails.
FAKE_FAL = "prototype-must-never-return-this-fal"
FAKE_ELEVEN = "prototype-must-never-return-this-eleven"

devvars = ROOT / ".dev.vars"
assert not devvars.exists(), "Refuse to overwrite any existing environment credentials"
devvars.write_text(
    'ACCESS_CODE="ci-only-code"\nOWNER_CODE="ci-only-owner"\n'
    f'WORKER_SECRET="{SECRET}"\nGITHUB_TOKEN=""\nGITHUB_REPO=""\n'
    f'FAL_KEY="{FAKE_FAL}"\nELEVENLABS_API_KEY="{FAKE_ELEVEN}"\nPUBLIC_URL=""\n'
)
# Own local state per run: the REAL daily limits (3/day per IP) apply in the prototype,
# so a run must not inherit the previous run's orders.
STATE = tempfile.mkdtemp(prefix="filmbam-prototype-state-")
log = open("/tmp/filmbam-prototype-test.log", "w+")
process = None
try:
    subprocess.run(
        ["npx", "wrangler", "d1", "migrations", "apply", "filmbam-prototype", "--local", "--config", CONFIG, "--persist-to", STATE],
        cwd=ROOT, check=True,
    )
    process = subprocess.Popen(
        ["npx", "wrangler", "dev", "--local", "--config", CONFIG, "--port", str(PORT), "--persist-to", STATE],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as response:
                health = json.load(response)
            break
        except Exception:
            if process.poll() is not None or time.monotonic() >= deadline:
                log.seek(0)
                raise RuntimeError("Prototype Worker did not start: " + log.read()[-5000:])
            time.sleep(0.5)

    # The deployment itself declares it is a prototype; this is what a curl check verifies.
    assert health["ok"] is True and health["demo"] is True, health

    with sync_playwright() as p:
        browser = launch_chromium(p)
        for i, width in enumerate([1440, 390]):
            # Own IP per viewport: the Worker's REAL limits (20 req/min and 3 orders/day per IP)
            # stay on in the prototype, so the two passes must not share the same quota.
            context = browser.new_context(
                viewport={"width": width, "height": 920},
                extra_http_headers={"CF-Connecting-IP": f"10.0.0.{10 + i}"},
            )
            page = context.new_page()
            errors = []
            app_requests = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: app_requests.append(r.url) if r.url.startswith(ORIGIN) else None)
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            page.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=200, body=""))

            # 1. The prototype notice appears BEFORE any promise, already on the gate.
            page.goto(BASE)
            page.wait_for_selector("#code")
            gate = page.locator(".demo").inner_text()
            assert "Prototype" in gate and "no video is generated" in gate, gate

            # 2. The gate is real: a wrong code does not get in.
            page.fill("#code", "wrong-code")
            page.click("#enter")
            page.wait_for_selector("#notice.on")
            assert page.locator("#code").count() == 1, "a wrong code must not open the studio"

            # 3. The right code gets in, and the cookie is scoped to the assigned path.
            page.fill("#code", "ci-only-code")
            page.click("#enter")
            page.wait_for_selector("#go")
            session = next(c for c in context.cookies() if c["name"] == "fb_sid")
            assert session["path"] == PREFIX and session["httpOnly"] and session["secure"], session
            assert "Prototype" in page.locator(".demo").inner_text()

            # 4. The real product flow: sentence → price → BAM. The order is recorded and priced.
            assert "$" in page.locator(".price").inner_text()
            page.fill("#prompt", "A quiet coffee shop opens at dawn and the barista greets the first visitor.")
            page.click("#go")
            page.wait_for_selector(".order")
            card = page.locator(".order").first.inner_text()
            assert "NOT PRODUCED" in card.upper(), card          # never "in queue"/"ready"
            assert "no film is produced" in card, card           # the note comes from the server
            assert page.locator(".order .dl").count() == 0, "a prototype must offer no download"

            # 5. Survives a reload and fits the screen.
            page.reload()
            page.wait_for_selector(".order")
            assert page.url == BASE + "/"
            assert "NOT PRODUCED" in page.locator(".order").first.inner_text().upper()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

            # 6. The server refuses what the prototype does not do, and returns no key at all.
            keys = context.request.get(BASE + "/api/worker/keys", headers={"X-Worker-Secret": SECRET})
            assert keys.status == 503, keys.status
            assert json.loads(keys.text())["code"] == "demo_no_runner", keys.text()
            body = keys.text()
            assert FAKE_FAL not in body and FAKE_ELEVEN not in body, "provider credentials must never be served"
            claim = context.request.post(BASE + "/api/worker/claim", headers={"X-Worker-Secret": SECRET}, data={})
            assert claim.status == 503, claim.status

            # Media, both layers: without a session the gate blocks first; WITH a session the server
            # says delivery does not exist in this deployment (the fetch runs inside the page,
            # which carries the session cookie).
            anon = context.request.get(BASE + "/media/fbtest0001/final.mp4")
            assert anon.status == 401 and json.loads(anon.text())["code"] == "no_session", anon.text()
            authed = page.evaluate(
                "async (base) => { const r = await fetch(base + '/media/fbtest0001/final.mp4',"
                " {credentials:'same-origin'}); return {status: r.status, body: await r.text()}; }",
                BASE,
            )
            assert authed["status"] == 503, authed
            assert json.loads(authed["body"])["code"] == "media_unavailable", authed

            # 7. The mount boundaries still hold.
            assert not errors, errors
            assert all(u.startswith(BASE + "/") or u == BASE for u in app_requests), app_requests
            assert context.request.get(BASE + "/missing/nested/page").status == 404
            assert context.request.get(ORIGIN + "/api/me").status == 404

            print("PASS prototype browser: banner, real gate, scoped cookie, order recorded as not "
                  f"produced, no download, runner and media refused, boundaries, width {width}")
            context.close()
        browser.close()
finally:
    if process:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    log.close()
    devvars.unlink(missing_ok=True)
    shutil.rmtree(STATE, ignore_errors=True)
