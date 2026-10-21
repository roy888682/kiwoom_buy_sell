"""Detached ownership and graceful shutdown; never use a brokerage account."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import background
import launch
from app.instance import FileLock, InstanceInUseError, project_identity


def record_for(root, **changes):
    return {"project_id": project_identity(root), "port": 8001,
            "control_port": 23456, "pid": 12345,
            "startup_token": "worker-owned-token", "control_token": "control-owned-token",
            **changes}


class RecordTests(unittest.TestCase):
    def test_only_valid_records_from_this_project_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, path, _ = background.paths(root)
            self.assertIsNone(background.read_record(root))
            original = record_for(root)
            for invalid in ({**original, "project_id": "another-project"},
                            {**original, "port": 0}, {**original, "port": True},
                            {**original, "control_port": "8000"},
                            {**original, "control_port": 65536},
                            {**original, "control_token": ""}, [], None):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertIsNone(background.read_record(root))
            path.write_text("truncated", encoding="utf-8")
            self.assertIsNone(background.read_record(root))
            background.write_record(path, original)
            self.assertEqual(background.read_record(root), original)
            self.assertEqual(list(path.parent.glob("background-*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_readiness_requires_startup_token_in_health_response(self):
        original = record_for(launch.ROOT)
        for status in ("ready", "other", "legacy", "free", "busy"):
            with self.subTest(status=status), patch("launch.probe_server", return_value=status) as probe:
                self.assertEqual(background.record_ready(launch.ROOT, original), status == "ready")
                probe.assert_called_once_with(8001, launch.ROOT, expected_token="worker-owned-token")

    def test_shutdown_requires_matching_authenticated_response(self):
        record = record_for(launch.ROOT)
        for payload, status, expected in [
            ({"startup_token": record["startup_token"]}, 200, True),
            ({"startup_token": "other-worker"}, 200, False),
            ({"startup_token": record["startup_token"]}, 403, False),
            ([], 200, False), (None, 200, False),
        ]:
            with self.subTest(payload=payload, status=status), patch("background.http.client.HTTPConnection") as http:
                response = http.return_value.getresponse.return_value
                response.status = status
                response.read.return_value = json.dumps(payload).encode()
                self.assertEqual(background.request_shutdown(record), expected)
                http.assert_called_once_with("127.0.0.1", record["control_port"], timeout=5)
                http.return_value.request.assert_called_once_with("POST", "/stop", body=b"", headers={
                    "Authorization": "Bearer " + record["control_token"], "Content-Length": "0"})
                http.return_value.close.assert_called_once()

    def test_stale_record_never_results_in_pid_termination(self):
        with patch("background.read_record", return_value=record_for(launch.ROOT)), patch("background.request_shutdown", return_value=False), patch("background.subprocess.Popen") as spawn, patch("os.kill") as kill:
            with self.assertRaises(launch.LauncherError):
                background.stop()
            spawn.assert_not_called()
            kill.assert_not_called()


class StartupTests(unittest.TestCase):
    def test_existing_background_is_opened_without_new_process(self):
        record = record_for(launch.ROOT)
        opener = Mock(return_value=True)
        with patch("background.read_record", return_value=record), patch("background.record_ready", return_value=True), patch("background.subprocess.Popen") as spawn:
            self.assertEqual(background.start(opener=opener), 0)
        opener.assert_called_once_with("http://127.0.0.1:8001")
        spawn.assert_not_called()

    def test_failed_browser_keeps_actionable_address_without_stopping_worker(self):
        record = record_for(launch.ROOT)
        with patch("background.read_record", return_value=record), patch("background.record_ready", return_value=True), patch("background.request_shutdown") as stop, self.assertRaisesRegex(launch.LauncherError, "http://127.0.0.1:8001"):
            background.start(opener=Mock(return_value=False))
        stop.assert_not_called()

    def test_foreground_is_opened_without_being_adopted_or_stopped(self):
        opener = Mock(return_value=True)
        with patch("background.read_record", return_value=None), patch("launch.scan_endpoints", return_value=[(8000, "free"), (8001, "ready")]), patch("background.subprocess.Popen") as spawn, patch("background.write_record") as write, patch("background.request_shutdown") as stop, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(background.start(opener=opener), 0)
        opener.assert_called_once_with("http://127.0.0.1:8001")
        self.assertIn("Ctrl+C", output.getvalue())
        spawn.assert_not_called()
        write.assert_not_called()
        stop.assert_not_called()

    def test_unknown_port_conflict_blocks_spawning(self):
        with patch("background.read_record", return_value=None), patch("launch.scan_endpoints", return_value=[(8000, "other")]), patch("launch.choose_endpoint", side_effect=launch.LauncherError("unknown listener")), patch("background.subprocess.Popen") as spawn, self.assertRaises(launch.LauncherError):
            background.start()
        spawn.assert_not_called()

    def spawn_case(self, windows):
        with tempfile.TemporaryDirectory(prefix="백그라운드 실행 ") as directory:
            root = Path(directory)
            python = root / "venv" / ("python.exe" if windows else "python")
            python.parent.mkdir()
            python.touch()
            if windows:
                python.with_name("pythonw.exe").touch()
            record = record_for(root)
            opener = Mock(return_value=True)
            # Patch only background's os binding, preserving native pathlib.
            platform_os = SimpleNamespace(name="nt" if windows else "posix", environ={"KEEP": "value"})
            with patch("background.os", platform_os), patch("background.read_record", side_effect=[None, record]), patch("background.record_ready", side_effect=[False, True]), patch("launch.scan_endpoints", return_value=[(8000, "free")]), patch("launch.choose_endpoint", return_value=(8000, "free")), patch("launch.ensure_runtime", return_value=python), patch.object(subprocess, "DETACHED_PROCESS", 0x8, create=True), patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), patch("background.subprocess.Popen") as spawn, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(background.start(root, opener=opener), 0)
            command = spawn.call_args.args[0]
            self.assertEqual(command, [str(python.with_name("pythonw.exe") if windows else python), str(root / "background.py"), "--serve"])
            options = spawn.call_args.kwargs
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertIs(options["stdout"], options["stderr"])
            self.assertEqual(options["env"], {"KEEP": "value", "PYTHONUTF8": "1"})
            self.assertEqual(options["cwd"], root)
            if windows:
                self.assertEqual(options["creationflags"], 0x208)
                self.assertNotIn("start_new_session", options)
            else:
                self.assertTrue(options["start_new_session"])
            opener.assert_called_once_with("http://127.0.0.1:8001")

    def test_windows_detaches_pythonw_and_preserves_utf8_environment(self):
        self.spawn_case(True)

    def test_posix_detaches_and_keeps_stdio_in_log(self):
        self.spawn_case(False)

    def test_launch_flags_dispatch_without_foreground_start(self):
        with patch("background.start", return_value=0) as start, patch("background.stop", return_value=0) as stop, patch("launch.run") as foreground:
            self.assertEqual(launch.main(["--background"]), 0)
            self.assertEqual(launch.main(["--stop-background"]), 0)
        start.assert_called_once_with()
        stop.assert_called_once_with()
        foreground.assert_not_called()


class WorkerIntegrationTests(unittest.TestCase):
    def test_actual_detached_worker_reuses_identity_and_only_accepts_its_stop_token(self):
        """Exercise a copied app, its real health endpoint and graceful lifespan."""
        with tempfile.TemporaryDirectory(prefix="백그라운드 test ") as directory:
            root = Path(directory)
            for name in ("background.py", "launch.py"):
                shutil.copy2(launch.ROOT / name, root / name)
            shutil.copytree(launch.ROOT / "app", root / "app",
                            ignore=shutil.ignore_patterns("__pycache__", "static"))
            with socket.socket() as listener:
                listener.bind((launch.HOST, 0))
                port = listener.getsockname()[1]
            script = (
                "import functools, launch, background; "
                "launch.CANDIDATE_PORTS=(int(__import__('os').environ['TEST_PORT']),); "
                "launch.InstanceLock=functools.partial(launch.InstanceLock, launch.ROOT/'test-port.lock'); "
                "raise SystemExit(background.serve())"
            )
            options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            env = {**os.environ, "PYTHONUTF8": "1", "TEST_PORT": str(port),
                   "TRADER_DATA_DIR": str(root / "test-data"), "ALLOW_LIVE_TRADING": "false"}
            # Do not inherit real API credentials into an integration fixture.
            for key in tuple(env):
                if key.startswith("KIWOOM_"):
                    env[key] = ""
            log_path = root / "worker-test.log"
            record = None
            with log_path.open("w", encoding="utf-8") as log:
                child = subprocess.Popen([sys.executable, "-c", script], cwd=root, env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        record = background.read_record(root)
                        if background.record_ready(root, record):
                            break
                        if child.poll() is not None:
                            self.fail(log_path.read_text(encoding="utf-8"))
                        time.sleep(0.05)
                    self.assertTrue(background.record_ready(root, record), log_path.read_text(encoding="utf-8"))
                    self.assertIsNone(child.poll())
                    self.assertFalse(background.request_shutdown({**record, "control_token": "wrong-token"}))
                    self.assertTrue(background.record_ready(root, record))
                    contender = FileLock(root / ".runtime/background.lock")
                    with self.assertRaises(InstanceInUseError):
                        contender.acquire()
                    opener = Mock(return_value=True)
                    with patch("background.subprocess.Popen") as spawn:
                        self.assertEqual(background.start(root, opener=opener), 0)
                        spawn.assert_not_called()
                    opener.assert_called_once_with(f"http://127.0.0.1:{port}")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(background.stop(root, timeout=15), 0)
                    self.assertEqual(child.wait(timeout=10), 0)
                    self.assertIsNone(background.read_record(root))
                    self.assertTrue(launch.port_available(port))
                    contender.acquire()
                    contender.release()
                finally:
                    if child.poll() is None:
                        if record:
                            background.request_shutdown(record)
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            # Only terminate this test's own child, never by a
                            # saved PID or a discovered port owner.
                            child.terminate()
                            child.wait(timeout=10)
