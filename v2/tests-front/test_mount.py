"""Native subpath browser check against the actual Worker, D1 and R2 in the CI runner.
All stores are disposable emulations. No Cloudflare account or paid provider is contacted.
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


PREFIX = "/apps/filmbam"
PORT = free_port()
ORIGIN = f"http://127.0.0.1:{PORT}"
BASE = ORIGIN + PREFIX
config = ROOT / "wrangler.hosting-test.toml"
devvars = ROOT / ".dev.vars"
assert not devvars.exists(), "Refuse to overwrite any existing environment credentials"
cfg = (ROOT / "wrangler.toml").read_text()
cfg = cfg.replace('[vars]', '[vars]\nPUBLIC_BASE_PATH = "' + PREFIX + '"')
config.write_text(cfg)
devvars.write_text('ACCESS_CODE="ci-only-code"\nOWNER_CODE="ci-only-owner"\nWORKER_SECRET="ci-only-runner-secret"\nGITHUB_TOKEN=""\nGITHUB_REPO=""\nFAL_KEY=""\nELEVENLABS_API_KEY=""\nPUBLIC_URL=""\n')
# Own local state per run (the Worker's daily limits are real).
STATE = tempfile.mkdtemp(prefix="filmbam-mount-state-")
log = open("/tmp/filmbam-hosting-test.log", "w+")
process = None
try:
    subprocess.run(["npx", "wrangler", "d1", "migrations", "apply", "filmbam", "--local", "--config", config.name, "--persist-to", STATE], cwd=ROOT, check=True)
    process = subprocess.Popen(["npx", "wrangler", "dev", "--local", "--config", config.name, "--port", str(PORT), "--persist-to", STATE], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as response:
                assert json.load(response)["ok"]
            break
        except Exception:
            if process.poll() is not None or time.monotonic() >= deadline:
                log.seek(0)
                raise RuntimeError("Test Worker did not start: " + log.read()[-5000:])
            time.sleep(0.5)
    with sync_playwright() as p:
        browser = launch_chromium(p)
        for i, width in enumerate([1440, 390]):
            # Own IP per viewport: the real per-IP limits must not be shared between passes.
            context = browser.new_context(
                viewport={"width": width, "height": 920},
                extra_http_headers={"CF-Connecting-IP": f"10.0.0.{20 + i}"},
            )
            page = context.new_page()
            errors = []
            app_requests = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: app_requests.append(r.url) if r.url.startswith(ORIGIN) else None)
            page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
            page.route("https://fonts.gstatic.com/**", lambda r: r.fulfill(status=200, body=""))
            page.goto(BASE)
            page.wait_for_selector("#code")
            assert page.url == BASE + "/"
            assert page.locator("script[src]").get_attribute("src") == "app.js"
            page.fill("#code", "ci-only-code")
            page.click("#enter")
            page.wait_for_selector("#go")
            cookies = context.cookies()
            session = next(c for c in cookies if c["name"] == "fb_sid")
            assert session["path"] == PREFIX and session["httpOnly"] and session["secure"]
            assert not context.cookies(ORIGIN + "/apps/other-app/")
            page.fill("#prompt", "A quiet coffee shop opens at dawn and the barista greets the first visitor.")
            page.click("#go")
            page.wait_for_selector(".order")
            assert "IN QUEUE" in page.locator(".order").first.inner_text().upper()
            page.reload()
            page.wait_for_selector(".order")
            assert page.url == BASE + "/"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert not errors, errors
            assert all(url.startswith(BASE + "/") or url == BASE for url in app_requests), app_requests
            assert context.request.get(BASE + "/missing/nested/page").status == 404
            assert context.request.get(ORIGIN + "/api/me").status == 404
            print("PASS native mounted browser: gate, cookie, assets, queue, reload, boundaries, width", width)
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
    config.unlink(missing_ok=True)
    devvars.unlink(missing_ok=True)
    shutil.rmtree(STATE, ignore_errors=True)
