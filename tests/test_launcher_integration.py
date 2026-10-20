"""Real loopback processes: alternate startup, reuse and exclusive data ownership.

No desktop browser, package installation or brokerage network access is used.
The runtime-preparation unit tests cover installing dependencies separately.
"""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import launch
from app.instance import project_identity


ROOT = Path(__file__).resolve().parents[1]


def consecutive_ports():
    """Find three currently bindable ephemeral ports, never CI's global 8000."""
    for _ in range(100):
        with socket.socket() as seed:
            seed.bind((launch.HOST, 0))
            first = seed.getsockname()[1]
        if first > 65533 or first <= 8010:
            continue
        try:
            with contextlib.ExitStack() as stack:
                for port in range(first, first + 3):
                    listener = stack.enter_context(socket.socket())
                    listener.bind((launch.HOST, port))
            return tuple(range(first, first + 3))
        except OSError:
            continue
    raise RuntimeError("Could not reserve three consecutive ephemeral test ports")


class LauncherProcessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="kiwoom-launcher-process-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.data = self.directory / "data"
        self.ports = consecutive_ports()
        candidate = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python = str(candidate if candidate.is_file() else Path(sys.executable))
        self.environment = {
            **os.environ, "TRADER_DATA_DIR": str(self.data),
            "KIWOOM_APP_KEY": "", "KIWOOM_SECRET_KEY": "",
            "ALLOW_LIVE_TRADING": "false", "KIWOOM_ENV": "mock", "PYTHONUTF8": "1",
        }
        self.log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.addCleanup(self.log.close)
        self.spawned = []
        self.real_popen = subprocess.Popen
        self.real_lock = launch.InstanceLock

    def cleanup_process(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def spawn(self, command, **options):
        """Capture real launcher children without replacing their behavior."""
        if command[1:3] == ["-m", "uvicorn"]:
            options.update(stdout=self.log, stderr=subprocess.STDOUT)
            process = self.real_popen(command, **options)
            self.spawned.append(process)
            self.addCleanup(self.cleanup_process, process)
            return process
        return self.real_popen(command, **options)

    def request(self, url, path="/api/health", body=None):
        endpoint = urlsplit(url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        try:
            headers = {}
            if body is not None:
                headers = {"Content-Type": "application/json", "Origin": url}
            connection.request("POST" if body is not None else "GET", path,
                               body=json.dumps(body) if body is not None else None,
                               headers=headers)
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 200, raw.decode(errors="replace"))
            return json.loads(raw)
        finally:
            connection.close()

    def launcher_context(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ, self.environment))
        stack.enter_context(patch.object(launch, "CANDIDATE_PORTS", self.ports))
        stack.enter_context(patch.object(launch, "PORT", self.ports[0]))
        stack.enter_context(patch.object(launch, "ensure_runtime", return_value=Path(self.python)))
        stack.enter_context(patch.object(launch, "InstanceLock",
            side_effect=lambda: self.real_lock(self.directory / "launcher.lock")))
        stack.enter_context(patch.object(launch.subprocess, "Popen", side_effect=self.spawn))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack

    def assert_own_health(self, url, process):
        health = self.request(url)
        self.assertEqual(health["service"], "kiwoom-breakout")
        self.assertEqual(health["project_id"], project_identity(ROOT))
        self.assertEqual(health["pid"], process.pid)
        self.assertEqual(health["instance_lock_version"], 1)
        self.assertEqual(launch.probe_server(urlsplit(url).port, ROOT, expected_pid=process.pid), "ready")
        return health

    def start_unrelated_http(self):
        # The stdlib CLI runs ThreadingHTTPServer. Its distinct command also
        # lets the real owner inspection recognize this as an unrelated server.
        process = self.real_popen(
            [self.python, "-m", "http.server", str(self.ports[0]),
             "--bind", launch.HOST, "--directory", str(self.directory)],
            env=self.environment, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.cleanup_process, process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection(launch.HOST, self.ports[0], timeout=0.5)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    return process
            except OSError:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            finally:
                connection.close()
        self.fail("Unrelated stdlib HTTP server did not start")

    def snapshot(self):
        with sqlite3.connect(self.data / "trader.sqlite3") as connection:
            return connection.execute("SELECT value FROM snapshot WHERE id=1").fetchone()[0]

    def test_occupied_port_uses_alternate_and_second_launch_reuses_running_engine(self):
        unrelated = self.start_unrelated_http()
        opened = []

        def opener(url):
            opened.append(url)
            self.assertEqual(url, f"http://{launch.HOST}:{self.ports[1]}")
            self.assertEqual(len(self.spawned), 1)
            child = self.spawned[0]
            self.assert_own_health(url, child)
            initial = self.request(url, "/api/state")
            self.assertFalse(initial["running"])
            self.assertEqual(initial["mode"], "paper")

            self.request(url, "/api/demo/reset", {})
            started = self.request(url, "/api/engine/start", {"mode": "paper", "confirmation": ""})
            self.assertTrue(started["running"])

            def nested_opener(nested_url):
                opened.append(nested_url)
                self.assertEqual(nested_url, url)
                self.assert_own_health(nested_url, child)
                return True

            self.assertEqual(launch.run(ROOT, opener=nested_opener, startup_timeout=10), 0)
            self.assertEqual(len(self.spawned), 1, "Second launch started another engine")
            self.assertEqual(self.request(url, "/api/state"), started,
                             "Reusing the dashboard changed its trading state")
            stopped = self.request(url, "/api/engine/stop", {})
            self.assertFalse(stopped["running"])
            self.assertIsNone(unrelated.poll(), "Launcher stopped the unrelated listener")
            raise KeyboardInterrupt

        with self.launcher_context():
            self.assertEqual(launch.run(ROOT, opener=opener, startup_timeout=30), 0)
        self.assertEqual(opened, [f"http://{launch.HOST}:{self.ports[1]}"] * 2)
        self.assertIsNotNone(self.spawned[0].poll(), "Owned server was left running")
        self.assertIsNone(unrelated.poll(), "Unrelated listener was shut down")

    def test_manual_second_port_is_rejected_before_shared_snapshot_changes(self):
        opened = []

        def opener(url):
            opened.append(url)
            self.assertEqual(url, f"http://{launch.HOST}:{self.ports[0]}")
            owner = self.spawned[0]
            self.assert_own_health(url, owner)
            initial = self.request(url, "/api/state")
            before = self.snapshot()
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as rejected_log:
                other = self.real_popen(
                    [self.python, "-m", "uvicorn", "app.main:app", "--host", launch.HOST,
                     "--port", str(self.ports[1])],
                    cwd=ROOT, env=self.environment, stdout=rejected_log, stderr=subprocess.STDOUT,
                )
                self.addCleanup(self.cleanup_process, other)
                self.assertNotEqual(other.wait(timeout=20), 0)
                rejected_log.seek(0)
                self.assertIn("같은 데이터 폴더를 사용하는 프로그램이 이미 실행 중입니다", rejected_log.read())
            self.assertEqual(self.snapshot(), before, "Rejected process changed the saved snapshot")
            self.assertEqual(self.request(url, "/api/state"), initial)
            self.assert_own_health(url, owner)
            self.assertEqual(len(self.spawned), 1)
            raise KeyboardInterrupt

        with self.launcher_context():
            self.assertEqual(launch.run(ROOT, opener=opener, startup_timeout=30), 0)
        self.assertEqual(opened, [f"http://{launch.HOST}:{self.ports[0]}"])
        self.assertIsNotNone(self.spawned[0].poll(), "Owned server was left running")


if __name__ == "__main__":
    unittest.main()
