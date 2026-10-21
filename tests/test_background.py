"""Detached ownership and graceful shutdown; never use a brokerage account."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
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
    def test_each_attempt_gets_a_private_closed_log_without_touching_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, _, legacy = background.paths(root)
            legacy.write_text("original diagnostics", encoding="utf-8")
            first = background.create_log_path(root)
            second = background.create_log_path(root)
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, legacy.parent)
            self.assertEqual(second.parent, legacy.parent)
            self.assertEqual(legacy.read_text(encoding="utf-8"), "original diagnostics")
            for path in (first, second):
                # Closing the reservation lets the worker own the only stream.
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("한글 실행 기록\n")
                self.assertEqual(path.read_text(encoding="utf-8"), "한글 실행 기록\n")
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_protected_runtime_directory_reports_access_problem_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("background.read_record", return_value=None), patch("background.record_ready", return_value=False), patch("launch.scan_endpoints", return_value=[(8000, "free")]), patch("launch.choose_endpoint", return_value=(8000, "free")), patch("launch.ensure_runtime", return_value=Path(sys.executable)), patch("background.tempfile.mkstemp", side_effect=PermissionError("denied")), patch("background.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(launch.LauncherError, "폴더의 쓰기 권한"):
                    background.start(Path(directory), opener=Mock(return_value=True))
                spawn.assert_not_called()

    def test_main_installs_utf8_streams_even_when_pythonw_has_no_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            log_path = background.create_log_path(root)

            def run_worker(*, log_path):
                print("한글 표준 출력", flush=True)
                print("한글 오류 출력", file=sys.stderr, flush=True)
                return 17

            with patch("launch.ROOT", root), patch("sys.argv", ["background.py", "--serve", "--log-file", str(log_path)]), patch("sys.stdout", None), patch("sys.stderr", None), patch("background.serve", side_effect=run_worker) as serve:
                self.assertEqual(background.main(), 17)
            serve.assert_called_once_with(log_path=log_path)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "한글 표준 출력\n한글 오류 출력\n")

    def test_early_child_failure_reports_reserved_log_without_waiting_full_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = Mock(returncode=1)
            child.poll.return_value = 1
            with patch("background.read_record", return_value=None), patch("background.record_ready", return_value=False), patch("launch.scan_endpoints", return_value=[(8000, "free")]), patch("launch.choose_endpoint", return_value=(8000, "free")), patch("launch.ensure_runtime", return_value=Path(sys.executable)), patch("background.subprocess.Popen", return_value=child) as spawn, patch("background.worker_owns_lock", return_value=False), patch("background.time.sleep") as sleep:
                with self.assertRaisesRegex(launch.LauncherError, "준비 전에 종료") as error:
                    background.start(root, opener=Mock(return_value=True))
            self.assertIn(spawn.call_args.args[0][-1], str(error.exception))
            sleep.assert_not_called()

    def test_losing_child_waits_for_worker_that_already_owns_the_project_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = record_for(root)
            child = Mock(returncode=1)
            child.poll.return_value = 1
            opener = Mock(return_value=True)
            with patch("background.read_record", side_effect=[None, None, record]), patch("background.record_ready", side_effect=[False, False, True]), patch("launch.scan_endpoints", return_value=[(8000, "free")]), patch("launch.choose_endpoint", return_value=(8000, "free")), patch("launch.ensure_runtime", return_value=Path(sys.executable)), patch("background.subprocess.Popen", return_value=child), patch("background.worker_owns_lock", return_value=True), patch("background.time.sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(background.start(root, opener=opener), 0)
            sleep.assert_called_once_with(0.25)
            opener.assert_called_once_with("http://127.0.0.1:8001")

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
            # Windows temp directories can use an 8.3 alias (RUNNER~1).
            # The launcher deliberately canonicalizes paths before spawning.
            root = Path(directory).resolve()
            python = root / "venv" / ("python.exe" if windows else "python")
            python.parent.mkdir()
            python.touch()
            if windows:
                python.with_name("pythonw.exe").touch()
            record = record_for(root)
            opener = Mock(return_value=True)
            # Patch only background's os binding, preserving native pathlib.
            platform_os = SimpleNamespace(name="nt" if windows else "posix", environ={"KEEP": "value"}, close=os.close)
            with patch("background.os", platform_os), patch("background.read_record", side_effect=[None, record]), patch("background.record_ready", side_effect=[False, True]), patch("launch.scan_endpoints", return_value=[(8000, "free")]), patch("launch.choose_endpoint", return_value=(8000, "free")), patch("launch.ensure_runtime", return_value=python), patch.object(subprocess, "DETACHED_PROCESS", 0x8, create=True), patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), patch("background.subprocess.Popen") as spawn, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(background.start(root, opener=opener), 0)
            command = spawn.call_args.args[0]
            self.assertEqual(command[:4], [str(python.with_name("pythonw.exe") if windows else python), str(root / "background.py"), "--serve", "--log-file"])
            self.assertEqual(len(command), 5)
            log_path = Path(command[4])
            self.assertEqual(log_path.parent, root / ".runtime")
            self.assertTrue(log_path.is_file())
            self.assertRegex(log_path.name, r"^background-.+\.log$")
            options = spawn.call_args.kwargs
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertEqual(options["stdout"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
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

    def test_posix_detaches_with_worker_owned_logging(self):
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
        """Real start -> pythonw/main -> Uvicorn, despite a blocked legacy log."""
        with tempfile.TemporaryDirectory(prefix="백그라운드 test ") as directory:
            root = Path(directory).resolve()
            for name in ("background.py", "launch.py"):
                shutil.copy2(launch.ROOT / name, root / name)
            shutil.copytree(launch.ROOT / "app", root / "app",
                            ignore=shutil.ignore_patterns("__pycache__", "static"))
            with socket.socket() as listener:
                listener.bind((launch.HOST, 0))
                port = listener.getsockname()[1]
            # Child imports this isolated launch module through the real CLI.
            # Only the port and launcher lock are test-specific.
            with (root / "launch.py").open("a", encoding="utf-8") as module:
                module.write("\nimport functools\n"
                             "CANDIDATE_PORTS=(int(os.environ['TEST_PORT']),)\n"
                             "InstanceLock=functools.partial(InstanceLock, ROOT/'test-port.lock')\n")
            env = {**os.environ, "PYTHONUTF8": "1", "TEST_PORT": str(port),
                   "TRADER_DATA_DIR": str(root / "test-data"), "ALLOW_LIVE_TRADING": "false"}
            # Do not inherit real API credentials into an integration fixture.
            for key in tuple(env):
                if key.startswith("KIWOOM_"):
                    env[key] = ""
            _, _, legacy_log = background.paths(root)
            legacy_log.write_text("Keep the previous diagnostic log.\n", encoding="utf-8")
            legacy_log.chmod(stat.S_IREAD)
            blocked_handle = None
            record = None
            children = []
            real_spawn = subprocess.Popen

            def track_child(*args, **kwargs):
                child = real_spawn(*args, **kwargs)
                children.append(child)
                return child

            try:
                if os.name == "nt":
                    # Hold the existing filename with no sharing, reproducing
                    # the Windows PermissionError instead of mocking open().
                    import ctypes
                    from ctypes import wintypes
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                        wintypes.DWORD, wintypes.HANDLE]
                    kernel.CreateFileW.restype = wintypes.HANDLE
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.CloseHandle.restype = wintypes.BOOL
                    blocked_handle = kernel.CreateFileW(str(legacy_log), 0x80000000,
                        0, None, 3, 0x80, None)  # GENERIC_READ, share=0, OPEN_EXISTING
                    self.assertNotEqual(blocked_handle, ctypes.c_void_p(-1).value)
                    with self.assertRaises(PermissionError):
                        with legacy_log.open("a", encoding="utf-8"):
                            pass
                with patch.dict(os.environ, env, clear=True), patch("launch.CANDIDATE_PORTS", (port,)), patch("launch.ensure_runtime", return_value=Path(sys.executable)), patch("background.subprocess.Popen", side_effect=track_child) as spawn, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(background.start(root, opener=Mock(return_value=True), timeout=30), 0)
                self.assertEqual(len(children), 1)
                child = children[0]
                if os.name == "nt":
                    self.assertEqual(Path(spawn.call_args.args[0][0]).name.lower(), "pythonw.exe")
                record = background.read_record(root)
                self.assertTrue(background.record_ready(root, record))
                log_path = root / ".runtime" / record["log_file"]
                self.assertNotEqual(log_path, legacy_log)
                self.assertTrue(log_path.is_file())
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
                # This log can only contain startup/shutdown messages if the
                # real main() installed the Python streams before Uvicorn.
                diagnostic = log_path.read_text(encoding="utf-8")
                self.assertIn("Application startup complete", diagnostic)
                self.assertIn("Application shutdown complete", diagnostic)
            finally:
                for child in children:
                    if child.poll() is not None:
                        continue
                    record = background.read_record(root)
                    if record:
                        background.request_shutdown(record)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        # Only terminate this test's own child, never by a
                        # saved PID or a discovered port owner.
                        child.terminate()
                        child.wait(timeout=10)
                if blocked_handle is not None:
                    kernel.CloseHandle(blocked_handle)
                legacy_log.chmod(stat.S_IREAD | stat.S_IWRITE)
            self.assertEqual(legacy_log.read_text(encoding="utf-8"), "Keep the previous diagnostic log.\n")
