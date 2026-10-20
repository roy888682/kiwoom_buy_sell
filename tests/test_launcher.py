"""One-click startup orchestration; no brokerage calls or desktop browser opens."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import launch


class ProbeTests(unittest.TestCase):
    def probe_response(self, body, status=200):
        response = Mock(status=status)
        response.read.return_value = body
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("launch.socket.create_connection"), patch("launch.http.client.HTTPConnection", return_value=connection):
            result = launch.probe_server()
        connection.request.assert_called_once_with("GET", "/api/health")
        connection.close.assert_called_once()
        response.read.assert_called_once_with(4097)
        return result

    def test_only_the_expected_healthy_service_is_reused(self):
        ready = json.dumps({"status": "ok", "service": "kiwoom-breakout"}).encode()
        self.assertEqual(self.probe_response(ready), "ready")
        for body, status in [
            (b'{"status":"ok","service":"another-app"}', 200),
            (b'{"status":"starting","service":"kiwoom-breakout"}', 200),
            (ready, 500), (b"not JSON", 200), (b"[]", 200),
            (ready + b" " * 4097, 200),
        ]:
            with self.subTest(body=body[:60], status=status):
                self.assertEqual(self.probe_response(body, status), "busy")

    def test_refused_connection_is_free_but_other_socket_errors_are_busy(self):
        with patch("launch.socket.create_connection", side_effect=ConnectionRefusedError), patch("launch.http.client.HTTPConnection") as http:
            self.assertEqual(launch.probe_server(), "free")
            http.assert_not_called()
        with patch("launch.socket.create_connection", side_effect=TimeoutError):
            self.assertEqual(launch.probe_server(), "busy")


class LockTests(unittest.TestCase):
    def test_competing_launcher_cannot_acquire_lock_and_file_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.lock"
            first, second = launch.InstanceLock(path), launch.InstanceLock(path)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.release()
                self.assertTrue(path.is_file())
                self.assertTrue(second.acquire())
            finally:
                first.release()
                second.release()


class ReadinessTests(unittest.TestCase):
    def test_waits_for_real_health_after_starting_or_unrelated_response(self):
        process = Mock()
        process.poll.return_value = None
        with patch("launch.probe_server", side_effect=["free", "busy", "ready"]) as probe, patch("launch.time.sleep"):
            launch.wait_for_ready(process, timeout=1)
        self.assertEqual(probe.call_count, 3)

    def test_child_exit_and_timeout_fail_instead_of_opening_early(self):
        process = Mock()
        process.poll.return_value = 1
        with patch("launch.probe_server") as probe, self.assertRaises(launch.LauncherError):
            launch.wait_for_ready(process, timeout=1)
        probe.assert_not_called()
        with self.assertRaises(launch.LauncherError):
            launch.wait_for_ready(timeout=0)


class BrowserTests(unittest.TestCase):
    def test_windows_per_user_brave_path_including_spaces(self):
        with tempfile.TemporaryDirectory(prefix="brave install ") as directory:
            executable = Path(directory) / "BraveSoftware/Brave-Browser/Application/brave.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            windows = SimpleNamespace(name="nt", environ={"LOCALAPPDATA": directory})
            with patch("launch.os", windows), patch("launch.shutil.which", return_value=None):
                self.assertEqual(launch.find_brave(), executable)

    def test_brave_is_invoked_with_separate_arguments_without_shell(self):
        executable = Path("Brave Browser") / "brave.exe"
        with patch("launch.find_brave", return_value=executable), patch("launch.subprocess.Popen") as spawn, patch("launch.webbrowser.open") as default:
            self.assertTrue(launch.open_dashboard())
        spawn.assert_called_once_with([str(executable), launch.URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        default.assert_not_called()

    def test_absent_or_failed_brave_falls_back_to_default_browser(self):
        for executable, error in [(None, None), (Path("brave.exe"), OSError("not executable"))]:
            with self.subTest(executable=executable), patch("launch.find_brave", return_value=executable), patch("launch.subprocess.Popen", side_effect=error), patch("launch.webbrowser.open", return_value=True) as default:
                self.assertTrue(launch.open_dashboard())
                default.assert_called_once_with(launch.URL, new=2)


class RuntimeTests(unittest.TestCase):
    def project(self, directory):
        root = Path(directory)
        (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
        (root / "uv.lock").write_text("fixture-lock\n")
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return root, python

    def test_prepared_environment_needs_no_installation_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root, python = self.project(directory)
            python.parent.mkdir(parents=True)
            python.touch()
            with patch("launch._succeeds", return_value=True), patch("launch._uv_command") as uv, patch("launch.subprocess.run") as install:
                self.assertEqual(launch.ensure_runtime(root), python)
                uv.assert_not_called()
                install.assert_not_called()

    def test_missing_environment_uses_frozen_lock_in_project_directory(self):
        with tempfile.TemporaryDirectory(prefix="프로그램 space ") as directory:
            root, python = self.project(directory)
            originals = {p.name: p.read_bytes() for p in (root / "pyproject.toml", root / "uv.lock")}

            def install(command, **options):
                self.assertEqual(options, {"cwd": root, "check": True})
                self.assertEqual(command[1:3], ["sync", "--frozen"])
                python.parent.mkdir(parents=True)
                python.touch()
                return subprocess.CompletedProcess(command, 0)

            with patch("launch._uv_command", return_value=["uv"]), patch("launch._succeeds", return_value=True), patch("launch.subprocess.run", side_effect=install), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launch.ensure_runtime(root), python)
            self.assertEqual(originals, {p.name: p.read_bytes() for p in (root / "pyproject.toml", root / "uv.lock")})


class RunTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stderr = contextlib.redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def test_existing_instance_only_opens_browser_without_process_or_state_change(self):
        opener = Mock(return_value=True)
        with patch("launch.probe_server", return_value="ready"), patch("launch.InstanceLock") as lock, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process, patch("launch.stop_owned_server") as stop:
            self.assertEqual(launch.run(opener=opener), 0)
            opener.assert_called_once_with(launch.URL)
            lock.assert_not_called()
            runtime.assert_not_called()
            process.assert_not_called()
            stop.assert_not_called()

    def test_busy_port_never_installs_starts_or_opens_unrelated_service(self):
        opener = Mock(return_value=True)
        with patch("launch.probe_server", return_value="busy"), patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
            self.assertEqual(launch.run(opener=opener), 1)
            opener.assert_not_called()
            runtime.assert_not_called()
            process.assert_not_called()

    def test_second_launcher_waits_then_reuses_without_starting_another_engine(self):
        opener = Mock(return_value=True)
        with patch("launch.probe_server", return_value="free"), patch("launch.InstanceLock") as factory, patch("launch.wait_for_ready") as ready, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
            factory.return_value.acquire.return_value = False
            self.assertEqual(launch.run(opener=opener, startup_timeout=2), 0)
            ready.assert_called_once_with(timeout=2)
            opener.assert_called_once_with(launch.URL)
            runtime.assert_not_called()
            process.assert_not_called()
            factory.return_value.release.assert_called_once()

    def test_rechecks_instance_after_lock_before_install_or_process(self):
        opener = Mock(return_value=True)
        with patch("launch.probe_server", side_effect=["free", "ready"]), patch("launch.InstanceLock") as factory, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
            factory.return_value.acquire.return_value = True
            self.assertEqual(launch.run(opener=opener), 0)
            opener.assert_called_once_with(launch.URL)
            runtime.assert_not_called()
            process.assert_not_called()

    def test_server_starts_from_korean_spaced_root_and_browser_waits_for_ready(self):
        with tempfile.TemporaryDirectory(prefix="프로그램 path ") as directory:
            root = Path(directory)
            python = root / ".venv" / "python fixture"
            events = []
            child = Mock()
            child.wait.return_value = 0
            opener = Mock(side_effect=lambda url: events.append(("browser", url)) or True)
            with patch("launch.probe_server", return_value="free"), patch("launch.InstanceLock") as factory, patch("launch.ensure_runtime", return_value=python), patch("launch.subprocess.Popen", return_value=child) as spawn, patch("launch.wait_for_ready", side_effect=lambda *a, **kw: events.append(("ready", None))) as ready, patch("launch.stop_owned_server") as stop:
                factory.return_value.acquire.return_value = True
                self.assertEqual(launch.run(root, opener=opener), 0)
                command = spawn.call_args.args[0]
                self.assertEqual(command, [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"])
                self.assertEqual(spawn.call_args.kwargs["cwd"], root)
                self.assertEqual(events, [("ready", None), ("browser", launch.URL)])
                ready.assert_called_once_with(child, timeout=45)
                stop.assert_called_once_with(child)
                factory.return_value.release.assert_called_once()

    def test_startup_failure_never_opens_browser_and_cleans_only_owned_child(self):
        opener = Mock(return_value=True)
        child = Mock()
        with patch("launch.probe_server", return_value="free"), patch("launch.InstanceLock") as factory, patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child), patch("launch.wait_for_ready", side_effect=launch.LauncherError("synthetic startup error")), patch("launch.stop_owned_server") as stop:
            factory.return_value.acquire.return_value = True
            self.assertEqual(launch.run(opener=opener), 1)
            opener.assert_not_called()
            stop.assert_called_once_with(child)
            factory.return_value.release.assert_called_once()

    def test_browser_failure_keeps_ready_server_running_for_manual_url(self):
        opener = Mock(return_value=False)
        child = Mock()
        child.wait.return_value = 0
        with patch("launch.probe_server", return_value="free"), patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child), patch("launch.wait_for_ready"), patch("launch.stop_owned_server"):
            self.assertEqual(launch.run(opener=opener), 0)
            child.wait.assert_called_once_with()
            self.assertIn(launch.URL, self.output.getvalue())


class ShutdownTests(unittest.TestCase):
    def test_already_exited_child_is_not_signaled(self):
        child = Mock()
        child.poll.return_value = 0
        launch.stop_owned_server(child)
        child.send_signal.assert_not_called()
        child.terminate.assert_not_called()

    def test_owned_child_gets_graceful_signal_and_wait(self):
        child = Mock()
        child.poll.return_value = None
        launch.stop_owned_server(child, grace=7)
        expected = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        child.send_signal.assert_called_once_with(expected)
        child.wait.assert_called_once_with(timeout=7)
        child.terminate.assert_not_called()

    def test_unresponsive_owned_child_is_terminated_then_killed(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 7), subprocess.TimeoutExpired("fixture", 5), 0]
        launch.stop_owned_server(child, grace=7)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()


class CommandLineTests(unittest.TestCase):
    def test_help_exits_before_setup_server_or_browser(self):
        with patch("launch.run") as run, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
            launch.main(["--help"])
        self.assertEqual(exited.exception.code, 0)
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Native cmd.exe validation runs on Windows CI only")
    def test_native_batch_resolves_own_korean_spaced_folder_and_forwards_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="실행 폴더 ") as directory:
            root = Path(directory)
            project = root / "프로그램 space"
            unrelated = root / "outside cwd"
            project.mkdir()
            unrelated.mkdir()
            for name in ("launch.py", "start.bat"):
                shutil.copyfile(launch.ROOT / name, project / name)
            for argument, expected in [("--help", 0), ("--bad-option", 2)]:
                with self.subTest(argument=argument):
                    command = f'cmd.exe /d /s /c ""{project / "start.bat"}" {argument}"'
                    result = subprocess.run(command, cwd=unrelated, input="\n", capture_output=True,
                                            text=True, encoding="utf-8", errors="replace", timeout=30)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertFalse((project / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
