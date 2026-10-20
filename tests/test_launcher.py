"""One-click startup orchestration; no brokerage calls or desktop browser opens."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

import launch
from app.instance import project_identity


class ProbeTests(unittest.TestCase):
    def probe_response(self, body, status=200, **options):
        response = Mock(status=status)
        response.read.return_value = body
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("launch.port_available", return_value=False), patch("launch.http.client.HTTPConnection", return_value=connection) as http:
            result = launch.probe_server(**options)
        http.assert_called_once_with(launch.HOST, options.get("port", launch.PORT), timeout=0.8)
        connection.request.assert_called_once_with("GET", "/api/health")
        connection.close.assert_called_once()
        response.read.assert_called_once_with(4097)
        return result

    def test_only_the_expected_healthy_service_is_reused(self):
        data = {"status": "ok", "service": "kiwoom-breakout",
                "project_id": project_identity(launch.ROOT), "instance_lock_version": 1, "pid": 1234}
        ready = json.dumps(data).encode()
        self.assertEqual(self.probe_response(ready), "ready")
        self.assertEqual(self.probe_response(ready, port=8001, expected_pid=1234), "ready")
        for body, status in [
            (b'{"status":"ok","service":"another-app"}', 200),
            (b'{"status":"starting","service":"kiwoom-breakout"}', 200),
            (ready, 500), (b"not JSON", 200), (b"[]", 200),
            (ready + b" " * 4097, 200),
            (json.dumps({**data, "project_id": "another-checkout"}).encode(), 200),
            (json.dumps({**data, "instance_lock_version": 0}).encode(), 200),
        ]:
            with self.subTest(body=body[:60], status=status):
                self.assertEqual(self.probe_response(body, status), "other")
        self.assertEqual(self.probe_response(ready, expected_pid=5678), "other")
        with tempfile.TemporaryDirectory(prefix="다른 프로젝트 ") as directory:
            self.assertEqual(self.probe_response(ready, root=Path(directory)), "other")

    def test_legacy_health_can_be_discovered_but_cannot_confirm_new_child(self):
        body = b'{"status":"ok","service":"kiwoom-breakout"}'
        self.assertEqual(self.probe_response(body), "legacy")
        self.assertEqual(self.probe_response(body, expected_pid=1234), "other")
        self.assertEqual(self.probe_response(body, expected_token="owned-spawn"), "other")

    def test_startup_token_identifies_real_interpreter_behind_windows_venv_redirector(self):
        data = {"status": "ok", "service": "kiwoom-breakout",
                "project_id": project_identity(launch.ROOT), "instance_lock_version": 1,
                "pid": 5678, "startup_token": "owned-spawn"}
        body = json.dumps(data).encode()
        self.assertEqual(self.probe_response(body, expected_token="owned-spawn"), "ready")
        self.assertEqual(self.probe_response(body, expected_token="another-spawn"), "other")
        self.assertEqual(self.probe_response(json.dumps({key: value for key, value in data.items() if key != "startup_token"}).encode(), expected_token="owned-spawn"), "other")

    def test_failed_http_requires_actual_bind_check_before_calling_port_free(self):
        for available, expected in [(True, "free"), (False, "busy")]:
            with self.subTest(available=available), patch("launch.http.client.HTTPConnection") as http, patch("launch.port_available", side_effect=[False, available]) as check:
                http.return_value.request.side_effect = TimeoutError
                self.assertEqual(launch.probe_server(port=8002), expected)
                self.assertEqual([call.args for call in check.call_args_list], [(8002,), (8002,)])
                http.return_value.close.assert_called_once_with()

    def test_free_port_skips_http_without_waiting_for_connection_timeout(self):
        with patch("launch.port_available", return_value=True) as check, patch("launch.http.client.HTTPConnection") as http:
            self.assertEqual(launch.probe_server(port=8002), "free")
            check.assert_called_once_with(8002)
            http.assert_not_called()

    def test_bound_listener_is_unavailable_and_release_makes_port_available(self):
        with socket.socket() as listener:
            listener.bind((launch.HOST, 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(launch.port_available(port))
        self.assertTrue(launch.port_available(port))


class EndpointTests(unittest.TestCase):
    def test_healthy_alternate_is_preferred_over_free_default_without_inspection(self):
        endpoints = [(8000, "free"), (8001, "ready"), (8002, "free")]
        with patch("launch.scan_endpoints", return_value=endpoints), patch("launch.unsafe_legacy_listener") as inspect:
            self.assertEqual(launch.choose_endpoint(), (8001, "ready"))
            inspect.assert_not_called()

    def test_busy_default_becomes_ready_before_starting_another_server(self):
        with patch("launch.scan_endpoints", side_effect=[[(8000, "busy"), (8001, "free")], [(8000, "ready"), (8001, "free")]]) as scan, patch("launch.time.sleep"), patch("launch.unsafe_legacy_listener") as inspect:
            self.assertEqual(launch.choose_endpoint(busy_timeout=1), (8000, "ready"))
            self.assertEqual(scan.call_count, 2)
            inspect.assert_not_called()

    def test_unrelated_listener_or_reserved_port_uses_first_free_alternate(self):
        for state in ("other", "busy"):
            with self.subTest(state=state), patch("launch.scan_endpoints", return_value=[(8000, state), (8001, "free"), (8002, "free")]), patch("launch.unsafe_legacy_listener", return_value=False) as inspect:
                self.assertEqual(launch.choose_endpoint(busy_timeout=0), (8001, "free"))
                inspect.assert_called_once_with(8000)

    def test_unknown_or_legacy_trading_listener_prevents_fallback(self):
        with patch("launch.scan_endpoints", return_value=[(8000, "busy"), (8001, "free")]), patch("launch.unsafe_legacy_listener", return_value=True), self.assertRaises(launch.LauncherError):
            launch.choose_endpoint(busy_timeout=0)

    def test_only_default_legacy_service_is_eligible_for_unidentified_reuse(self):
        self.assertEqual(launch.existing_endpoint([(8000, "legacy"), (8001, "free")]), (8000, "legacy"))
        self.assertIsNone(launch.existing_endpoint([(8000, "free"), (8001, "legacy")]))
        self.assertEqual(launch.existing_endpoint([(8000, "legacy"), (8001, "ready")]), (8001, "ready"))

    def test_all_unrelated_ports_occupied_reports_no_available_port(self):
        with patch("launch.scan_endpoints", return_value=[(8000, "other"), (8001, "other")]), patch("launch.unsafe_legacy_listener", return_value=False), self.assertRaises(launch.LauncherError):
            launch.choose_endpoint(busy_timeout=0)

    def test_lock_contender_discovers_alternate_port_and_does_not_require_default(self):
        with patch("launch.scan_endpoints", side_effect=[[(8000, "other"), (8001, "free")], [(8000, "other"), (8001, "ready")]]), patch("launch.time.sleep"):
            self.assertEqual(launch.wait_for_existing(timeout=1), 8001)


class ListenerSafetyTests(unittest.TestCase):
    def test_windows_inspection_returns_no_owners_only_after_successful_query(self):
        windows = SimpleNamespace(name="nt")
        result = SimpleNamespace(returncode=0, stdout="[]")
        with patch("launch.os", windows), patch.object(launch.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), patch("launch.subprocess.run", return_value=result) as query:
            self.assertEqual(launch._listener_processes(8000), [])
        command = query.call_args.args[0]
        self.assertEqual(command[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("Get-NetTCPConnection -State Listen -ErrorAction Stop", command[4])
        self.assertIn("Where-Object", command[4])
        self.assertNotIn("SilentlyContinue", command[4])

    def test_windows_failed_owner_query_is_unknown_instead_of_no_listener(self):
        windows = SimpleNamespace(name="nt")
        for result in (SimpleNamespace(returncode=1, stdout="[]"),
                       SimpleNamespace(returncode=0, stdout="invalid JSON"),
                       SimpleNamespace(returncode=0, stdout='{"Name":"python.exe"}')):
            with self.subTest(result=result), patch("launch.os", windows), patch.object(launch.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), patch("launch.subprocess.run", return_value=result):
                self.assertIsNone(launch._listener_processes(8000))
        with patch("launch.os", windows), patch.object(launch.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), patch("launch.subprocess.run", side_effect=subprocess.TimeoutExpired("powershell.exe", 10)):
            self.assertIsNone(launch._listener_processes(8000))

    def test_process_inspection_failures_and_old_python_servers_are_unsafe(self):
        for rows in (None, [{"Name": "python.exe", "CommandLine": None}],
                     [{"Name": "python.exe", "CommandLine": "python -m uvicorn app.main:app"}],
                     [{"Name": "uvicorn", "CommandLine": "uvicorn other.app:app"}],
                     [{"Name": "", "CommandLine": ""}]):
            with self.subTest(rows=rows), patch("launch._listener_processes", return_value=rows):
                self.assertTrue(launch.unsafe_legacy_listener(8000))

    def test_known_foreign_listeners_and_no_listener_allow_fallback(self):
        for rows in ([], [{"Name": "node.exe", "CommandLine": "node server.js"}],
                     [{"Name": "python.exe", "CommandLine": "python -m http.server 8000"}]):
            with self.subTest(rows=rows), patch("launch._listener_processes", return_value=rows):
                self.assertFalse(launch.unsafe_legacy_listener(8000))


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
        process.pid = 1234
        process.poll.return_value = None
        with patch("launch.probe_server", side_effect=["free", "busy", "ready"]) as probe, patch("launch.time.sleep"):
            launch.wait_for_ready(process, timeout=1, port=8001, startup_token="owned-spawn")
        self.assertEqual(probe.call_count, 3)
        for call in probe.call_args_list:
            self.assertEqual(call.args, (8001, launch.ROOT))
            self.assertEqual(call.kwargs, {"expected_token": "owned-spawn"})

    def test_legacy_or_wrong_child_health_never_opens_as_ready(self):
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch("launch.probe_server", side_effect=["legacy", "other", "ready"]) as probe, patch("launch.time.sleep"):
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
        scan = patch("launch.scan_endpoints", return_value=[(8000, "free")])
        self.scan = scan.start()
        self.addCleanup(scan.stop)
        choose = patch("launch.choose_endpoint", return_value=(8000, "free"))
        self.choose = choose.start()
        self.addCleanup(choose.stop)

    def test_existing_instance_only_opens_browser_without_process_or_state_change(self):
        opener = Mock(return_value=True)
        self.scan.return_value = [(8000, "ready")]
        with patch("launch.InstanceLock") as lock, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process, patch("launch.stop_owned_server") as stop:
            self.assertEqual(launch.run(opener=opener), 0)
            opener.assert_called_once_with(launch.URL)
            lock.assert_not_called()
            runtime.assert_not_called()
            process.assert_not_called()
            stop.assert_not_called()

    def test_existing_alternate_instance_is_reused_without_install_or_child(self):
        opener = Mock(return_value=True)
        self.scan.return_value = [(8000, "other"), (8001, "ready")]
        with patch("launch.InstanceLock") as lock, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
            self.assertEqual(launch.run(opener=opener), 0)
            opener.assert_called_once_with("http://127.0.0.1:8001")
            lock.assert_not_called()
            runtime.assert_not_called()
            process.assert_not_called()

    def test_unsafe_busy_listener_never_installs_starts_or_opens_browser(self):
        opener = Mock(return_value=True)
        self.choose.side_effect = launch.LauncherError("이전 실행을 확인하세요.")
        with patch("launch.InstanceLock"), patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
            self.assertEqual(launch.run(opener=opener), 1)
            opener.assert_not_called()
            runtime.assert_not_called()
            process.assert_not_called()

    def test_second_launcher_waits_then_reuses_without_starting_another_engine(self):
        opener = Mock(return_value=True)
        with patch("launch.InstanceLock") as factory, patch("launch.wait_for_existing", return_value=8001) as ready, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
            factory.return_value.acquire.return_value = False
            self.assertEqual(launch.run(opener=opener, startup_timeout=2), 0)
            ready.assert_called_once_with(launch.ROOT, timeout=2)
            opener.assert_called_once_with("http://127.0.0.1:8001")
            runtime.assert_not_called()
            process.assert_not_called()
            factory.return_value.release.assert_called_once()

    def test_rechecks_instance_after_lock_before_install_or_process(self):
        opener = Mock(return_value=True)
        self.choose.return_value = (8000, "ready")
        with patch("launch.InstanceLock") as factory, patch("launch.ensure_runtime") as runtime, patch("launch.subprocess.Popen") as process:
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
            with patch("launch.InstanceLock") as factory, patch("launch.ensure_runtime", return_value=python), patch("launch.subprocess.Popen", return_value=child) as spawn, patch("launch.wait_for_ready", side_effect=lambda *a, **kw: events.append(("ready", None))) as ready, patch("launch.stop_owned_server") as stop:
                factory.return_value.acquire.return_value = True
                self.assertEqual(launch.run(root, opener=opener), 0)
                command = spawn.call_args.args[0]
                self.assertEqual(command, [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"])
                self.assertEqual(spawn.call_args.kwargs["cwd"], root.resolve())
                self.assertEqual(events, [("ready", None), ("browser", launch.URL)])
                ready.assert_called_once_with(child, timeout=45, port=8000, root=root.resolve(), startup_token=ANY)
                stop.assert_called_once_with(child)
                factory.return_value.release.assert_called_once()

    def test_startup_failure_never_opens_browser_and_cleans_only_owned_child(self):
        opener = Mock(return_value=True)
        child = Mock()
        child.poll.return_value = None
        with patch("launch.InstanceLock") as factory, patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child) as spawn, patch("launch.wait_for_ready", side_effect=launch.LauncherError("synthetic startup error")), patch("launch.stop_owned_server") as stop:
            factory.return_value.acquire.return_value = True
            self.assertEqual(launch.run(opener=opener), 1)
            opener.assert_not_called()
            spawn.assert_called_once()
            stop.assert_called_once_with(child)
            factory.return_value.release.assert_called_once()

    def test_browser_failure_keeps_ready_server_running_for_manual_url(self):
        opener = Mock(return_value=False)
        child = Mock()
        child.wait.return_value = 0
        self.choose.return_value = (8001, "free")
        with patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child), patch("launch.wait_for_ready"), patch("launch.stop_owned_server"):
            self.assertEqual(launch.run(opener=opener), 0)
            child.wait.assert_called_once_with()
            self.assertIn("http://127.0.0.1:8001", self.output.getvalue())

    def test_safe_busy_default_starts_selected_alternate_and_opens_exact_url(self):
        self.scan.return_value = [(8000, "other"), (8001, "free")]
        self.choose.return_value = (8001, "free")
        opener = Mock(return_value=True)
        child = Mock()
        child.wait.return_value = 0
        with patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child) as spawn, patch("launch.wait_for_ready") as ready, patch("launch.stop_owned_server"):
            self.assertEqual(launch.run(opener=opener), 0)
            self.assertEqual(spawn.call_args.args[0][-2:], ["--port", "8001"])
            ready.assert_called_once_with(child, timeout=45, port=8001, root=launch.ROOT, startup_token=ANY)
            opener.assert_called_once_with("http://127.0.0.1:8001")

    def test_child_gets_fresh_startup_token_without_changing_parent_environment(self):
        child = Mock()
        child.wait.return_value = 0
        opener = Mock(return_value=True)
        parent_values = {"BREAKLINE_STARTUP_TOKEN": "parent-old-token", "LAUNCH_TEST_PRESERVED": "kept"}
        with patch.dict("launch.os.environ", parent_values), patch("launch.secrets.token_urlsafe", return_value="fresh-child-token") as token, patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child) as spawn, patch("launch.wait_for_ready") as ready, patch("launch.stop_owned_server"):
            self.assertEqual(launch.run(opener=opener), 0)
            token.assert_called_once_with(32)
            child_environment = spawn.call_args.kwargs["env"]
            self.assertEqual(child_environment["BREAKLINE_STARTUP_TOKEN"], "fresh-child-token")
            self.assertEqual(child_environment["LAUNCH_TEST_PRESERVED"], "kept")
            self.assertEqual(os.environ["BREAKLINE_STARTUP_TOKEN"], "parent-old-token")
            ready.assert_called_once_with(child, timeout=45, port=8000, root=launch.ROOT, startup_token="fresh-child-token")

    def test_manual_instance_appearing_during_setup_is_reused_before_spawn(self):
        self.choose.side_effect = [(8000, "free"), (8002, "ready")]
        opener = Mock(return_value=True)
        with patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen") as spawn:
            self.assertEqual(launch.run(opener=opener), 0)
            opener.assert_called_once_with("http://127.0.0.1:8002")
            spawn.assert_not_called()

    def test_bind_race_stops_failed_child_and_reselects_without_opening_wrong_port(self):
        self.choose.side_effect = [(8000, "free"), (8000, "free"), (8001, "free")]
        first, second = Mock(), Mock()
        first.poll.return_value = 1
        second.wait.return_value = 0
        opener = Mock(return_value=True)
        with patch("launch.secrets.token_urlsafe", side_effect=["first-spawn-token", "second-spawn-token"]), patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", side_effect=[first, second]) as spawn, patch("launch.wait_for_ready", side_effect=[launch.LauncherError("bind race"), None]) as ready, patch("launch.port_available", return_value=False), patch("launch.stop_owned_server") as stop:
            self.assertEqual(launch.run(opener=opener), 0)
            self.assertEqual([call.args[0][-1] for call in spawn.call_args_list], ["8000", "8001"])
            self.assertEqual([call.kwargs["env"]["BREAKLINE_STARTUP_TOKEN"] for call in spawn.call_args_list], ["first-spawn-token", "second-spawn-token"])
            self.assertEqual([call.kwargs["startup_token"] for call in ready.call_args_list], ["first-spawn-token", "second-spawn-token"])
            self.assertEqual([call.args[0] for call in stop.call_args_list], [first, second])
            opener.assert_called_once_with("http://127.0.0.1:8001")

    def test_startup_exit_on_free_port_does_not_loop_or_mask_actual_error(self):
        child = Mock()
        child.poll.return_value = 1
        opener = Mock(return_value=True)
        with patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", return_value=child) as spawn, patch("launch.wait_for_ready", side_effect=launch.LauncherError("real startup failure")), patch("launch.port_available", return_value=True), patch("launch.stop_owned_server") as stop:
            self.assertEqual(launch.run(opener=opener), 1)
            spawn.assert_called_once()
            stop.assert_called_once_with(child)
            opener.assert_not_called()
            self.assertIn("real startup failure", self.output.getvalue())

    def test_bind_race_retries_are_bounded_and_never_open_unready_browser(self):
        children = [Mock() for _ in range(3)]
        for child in children:
            child.poll.return_value = 1
        opener = Mock(return_value=True)
        with patch("launch.InstanceLock"), patch("launch.ensure_runtime", return_value=Path("python")), patch("launch.subprocess.Popen", side_effect=children) as spawn, patch("launch.wait_for_ready", side_effect=launch.LauncherError("bind race")), patch("launch.port_available", return_value=False), patch("launch.stop_owned_server") as stop:
            self.assertEqual(launch.run(opener=opener), 1)
            self.assertEqual(spawn.call_count, 3)
            self.assertEqual([call.args[0] for call in stop.call_args_list], children)
            opener.assert_not_called()


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
