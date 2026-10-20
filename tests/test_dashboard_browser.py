"""Real Chromium regressions against an isolated, loopback-only paper server.

Optional locally: uv run --with playwright==1.62.0 python -m unittest discover
-s tests -p test_dashboard_browser.py -v. Install Playwright's Chromium first,
or set DASHBOARD_CHROMIUM to a local Chromium executable.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import ProxyHandler, build_opener

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
STAMP = "최근 동기화 2026-01-02 01:02:03 KST"


@unittest.skipIf(sync_playwright is None, "Optional browser tests require playwright==1.62.0")
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                executable_path=os.getenv("DASHBOARD_CHROMIUM") or None,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="dashboard-browser-")
        self.addCleanup(directory.cleanup)
        self.log = tempfile.TemporaryFile(mode="w+")
        self.addCleanup(self.log.close)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        environment = {**os.environ, "TRADER_DATA_DIR": directory.name,
                       "KIWOOM_APP_KEY": "", "KIWOOM_SECRET_KEY": "",
                       "ALLOW_LIVE_TRADING": "false", "KIWOOM_ENV": "mock"}
        self.server = subprocess.Popen(
            [str(python) if python.exists() else sys.executable, "-m", "uvicorn",
             "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=environment, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.stop_server)
        self.http = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.get_json("/api/health")["status"] == "ok":
                    break
            except OSError:
                if self.server.poll() is not None:
                    break
                time.sleep(0.05)
        else:
            self.fail("Isolated dashboard server did not become healthy")
        if self.server.poll() is not None:
            self.log.seek(0)
            self.fail(self.log.read())
        # A different browser timezone catches accidental use of local time.
        self.context = self.browser.new_context(timezone_id="America/Los_Angeles")
        self.addCleanup(self.context.close)
        self.context.route("**/*", self.local_only)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.clock.install(time=datetime(2026, 1, 1, 16, 2, 0, tzinfo=timezone.utc))
        self.page.clock.pause_at(datetime(2026, 1, 1, 16, 2, 3, tzinfo=timezone.utc))
        self.errors = []
        self.mutations = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", lambda request: self.mutations.append(request.url)
                     if request.method not in {"GET", "HEAD"} else None)
        self.page.goto(self.url)
        expect(self.page.locator("#last-updated")).to_have_text(STAMP)

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)

    def local_only(self, route):
        if route.request.url.startswith(self.url + "/"):
            route.continue_()
        else:
            self.errors.append("Unexpected nonlocal request: " + route.request.url)
            route.abort()

    def get_json(self, path):
        with self.http.open(self.url + path, timeout=1) as response:
            return json.load(response)

    def reset_samples(self):
        self.page.locator("#demo-reset").click()
        self.page.locator('#dialog-fields input[name="confirm"]').check()
        self.page.locator("#dialog-submit").click()
        expect(self.page.locator("#input-dialog")).not_to_be_visible()
        expect(self.page.locator("#watch-count")).to_have_text("2")
        self.mutations.clear()

    def next_poll(self):
        with self.page.expect_request("**/api/state"):
            self.page.clock.fast_forward(3100)

    def assert_single_start(self):
        self.assertEqual(self.mutations, [self.url + "/api/engine/start"])
        self.assertEqual(self.errors, [])

    def assert_korean_transport_warning(self):
        expect(self.page.locator("#toast")).to_contain_text("요청 처리 여부를 확인할 수 없습니다")
        self.assertNotIn("Failed to fetch", self.page.locator("body").inner_text())

    def test_paper_start_scenario_stop_and_full_kst_sync_time(self):
        self.reset_samples()
        self.page.locator("#start-engine").click()
        expect(self.page.locator("#engine-status")).to_have_text("모의 거래 · 감시 중")
        self.page.locator("#demo-scenario").click()
        expect(self.page.locator("#positions-content tr")).to_have_count(2)
        expect(self.page.locator("#position-count")).to_have_text("2")
        expect(self.page.locator("#orders-content tr")).to_have_count(2)
        state = self.get_json("/api/state")
        self.assertEqual({item["key"] for item in state["positions"]}, {"KR:000000", "US:DEMO"})
        self.page.locator("#stop-engine").click()
        expect(self.page.locator("#engine-status")).to_have_text("모의 거래 · 대기")
        self.assertFalse(self.get_json("/api/state")["running"])
        self.next_poll()
        expect(self.page.locator("#last-updated")).to_have_text("최근 동기화 2026-01-02 01:02:06 KST")
        self.assertEqual(self.errors, [])

    def test_start_transport_failure_does_not_replay_or_claim_running(self):
        self.reset_samples()
        self.page.route("**/api/engine/start", lambda route: route.abort("failed"))
        self.page.locator("#start-engine").click()
        self.assert_korean_transport_warning()
        expect(self.page.locator("#engine-status")).to_have_text("모의 거래 · 대기")
        self.assertFalse(self.get_json("/api/state")["running"])
        self.next_poll()
        expect(self.page.locator("#start-engine")).to_be_enabled()
        self.assert_single_start()

    def test_lost_start_response_reconciles_running_without_replaying_post(self):
        self.reset_samples()

        def lose_response(route):
            response = route.fetch()
            self.assertEqual(response.status, 200)
            route.abort("failed")

        self.page.route("**/api/engine/start", lose_response)
        self.page.locator("#start-engine").click()
        self.assert_korean_transport_warning()
        expect(self.page.locator("#engine-status")).to_have_text("모의 거래 · 감시 중")
        expect(self.page.locator("#stop-engine")).to_be_visible()
        self.assertTrue(self.get_json("/api/state")["running"])
        self.next_poll()
        self.assert_single_start()

    def test_failed_state_after_start_shows_unknown_until_read_only_recovery(self):
        self.reset_samples()
        offline = False

        def state_request(route):
            route.abort("failed") if offline else route.continue_()

        def start_then_disconnect(route):
            nonlocal offline
            response = route.fetch()
            self.assertEqual(response.status, 200)
            offline = True
            route.fulfill(response=response)

        self.page.route("**/api/state", state_request)
        self.page.route("**/api/engine/start", start_then_disconnect)
        self.page.locator("#start-engine").click()
        banner = self.page.locator("#global-message")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("서버에 연결할 수 없습니다")
        expect(self.page.locator("#start-engine")).to_be_disabled()
        expect(self.page.locator("#stop-engine")).to_be_visible()
        self.assertNotIn("모의 거래 · 대기", self.page.locator("#engine-status").inner_text())
        self.assertNotIn("모의 거래 감시를 시작했습니다", self.page.locator("#toast").inner_text())
        expect(self.page.locator("#last-updated")).to_contain_text(STAMP)
        expect(self.page.locator("#last-updated")).to_contain_text("서버 연결 끊김")
        self.assertTrue(self.get_json("/api/state")["running"])
        self.next_poll()
        expect(banner).to_be_visible()
        self.assert_single_start()
        offline = False
        self.next_poll()
        expect(self.page.locator("#engine-status")).to_have_text("모의 거래 · 감시 중")
        expect(banner).not_to_be_visible()
        self.assert_single_start()
