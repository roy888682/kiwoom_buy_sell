"""Local detached server with authenticated, graceful shutdown of its own worker.

No PID is killed. A foreground server is only opened, never adopted. Control
metadata contains local random tokens, not brokerage credentials.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time

import launch
from app.instance import FileLock, InstanceInUseError, project_identity


def paths(root):
    directory = Path(root).resolve() / ".runtime"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory, directory / "background.json", directory / "background.log"


def create_log_path(root):
    """Reserve a private log for one attempt; never reopen a previous log.

    Windows can deny access to an existing log held by another process or
    marked read-only. Closing this reservation before spawning also ensures
    that only the worker owns the output stream, including under pythonw.
    """
    try:
        directory, _, _ = paths(root)
        descriptor, name = tempfile.mkstemp(
            prefix=f"background-{datetime.now():%Y%m%d-%H%M%S}-", suffix=".log", dir=directory)
        os.close(descriptor)
        return Path(name)
    except OSError as exc:
        directory = Path(root).resolve() / ".runtime"
        raise launch.LauncherError(
            f"실행 기록을 저장할 수 없습니다: {directory}. "
            "이 폴더의 쓰기 권한과 Windows 폴더 보호 알림을 확인하세요."
        ) from exc


def worker_owns_lock(root):
    """A second click may see its child exit while the first is still loading."""
    directory, _, _ = paths(root)
    lock = FileLock(directory / "background.lock")
    try:
        lock.acquire()
        return False
    except InstanceInUseError:
        return True
    finally:
        lock.release()


def read_record(root):
    _, record_path, _ = paths(root)
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if (record.get("project_id") != project_identity(root)
                or type(record.get("port")) is not int
                or not 1 <= record["port"] <= 65535
                or type(record.get("control_port")) is not int
                or not 1 <= record["control_port"] <= 65535
                or not all(isinstance(record.get(k), str) and record[k] for k in ("startup_token", "control_token"))):
            return None
        return record
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def record_ready(root, record):
    return bool(record and launch.probe_server(record["port"], root,
                expected_token=record["startup_token"]) == "ready")


def write_record(path, record):
    temporary = path.with_name("background-" + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def request_shutdown(record):
    connection = http.client.HTTPConnection(launch.HOST, record["control_port"], timeout=5)
    try:
        connection.request("POST", "/stop", body=b"", headers={
            "Authorization": "Bearer " + record["control_token"], "Content-Length": "0"})
        response = connection.getresponse()
        payload = response.read(4096)
        data = json.loads(payload)
        return (response.status == 200 and isinstance(data, dict)
                and data.get("startup_token") == record["startup_token"])
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def serve(root=launch.ROOT, *, log_path=None):
    """Own Uvicorn in this interpreter, so pythonw needs no console signals."""
    root = Path(root).resolve()
    directory, record_path, _ = paths(root)
    lock = FileLock(directory / "background.lock")
    port_lock = launch.InstanceLock()
    control = None
    record = None
    try:
        lock.acquire()
        if not port_lock.acquire():
            return 2
        port, state = launch.choose_endpoint(root)
        if state in {"ready", "legacy"}:
            return 0
        os.environ["BREAKLINE_BACKGROUND"] = "1"
        os.environ["BREAKLINE_STARTUP_TOKEN"] = secrets.token_urlsafe(32)
        import uvicorn
        server = uvicorn.Server(uvicorn.Config("app.main:app", host=launch.HOST,
                                port=port, access_log=False))
        control_token = secrets.token_urlsafe(32)

        class ControlHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log authentication headers or control tokens.

            def do_POST(self):
                if self.path != "/stop" or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + control_token):
                    self.send_error(403)
                    return
                # An authorized stop still applies if the client disconnects
                # before reading the response.
                server.should_exit = True
                raw = json.dumps({"startup_token": os.environ["BREAKLINE_STARTUP_TOKEN"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        control = ThreadingHTTPServer((launch.HOST, 0), ControlHandler)
        control.daemon_threads = True
        threading.Thread(target=control.serve_forever, daemon=True).start()
        record = {"project_id": project_identity(root), "pid": os.getpid(), "port": port,
                  "control_port": control.server_address[1], "control_token": control_token,
                  "startup_token": os.environ["BREAKLINE_STARTUP_TOKEN"]}
        if log_path is not None:
            record["log_file"] = Path(log_path).name
        write_record(record_path, record)
        server.run()
        return 0 if server.started else 1
    except (InstanceInUseError, launch.LauncherError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        if control:
            control.shutdown()
            control.server_close()
        if record:
            latest = read_record(root)
            if latest and latest["startup_token"] == record["startup_token"]:
                with contextlib.suppress(FileNotFoundError):
                    record_path.unlink()
        port_lock.release()
        lock.release()


def start(root=launch.ROOT, *, opener=None, timeout=60):
    root = Path(root).resolve()
    opener = opener or launch.open_dashboard
    record = read_record(root)
    if record_ready(root, record):
        url = f"http://{launch.HOST}:{record['port']}"
        if not opener(url):
            raise launch.LauncherError(f"프로그램은 실행 중입니다. 브라우저 주소창에 {url} 을 직접 입력하세요.")
        return 0
    existing = launch.existing_endpoint(launch.scan_endpoints(root))
    if existing:
        print("기존 프로그램 화면을 엽니다. 일반 실행 중이면 그 창에서 Ctrl+C로 종료한 뒤 백그라운드 실행을 다시 누르세요.", flush=True)
        url = f"http://{launch.HOST}:{existing[0]}"
        if not opener(url):
            raise launch.LauncherError(f"프로그램은 실행 중입니다. 브라우저 주소창에 {url} 을 직접 입력하세요.")
        return 0
    # Validate known port conflicts before detaching; never start a second
    # trading process beside an unidentified older server.
    launch.choose_endpoint(root)
    python = launch.ensure_runtime(root)
    if os.name == "nt":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.is_file():
            python = pythonw
    log_path = create_log_path(root)
    options = {"cwd": root, "env": {**os.environ, "PYTHONUTF8": "1"}}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    # pythonw has no Python console streams. The worker installs its own log
    # before configuring Uvicorn; parent and worker never open it together.
    process = subprocess.Popen([str(python), str(root / "background.py"), "--serve",
                                "--log-file", str(log_path)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = read_record(root)
        if record_ready(root, record):
            url = f"http://{launch.HOST}:{record['port']}"
            print(f"백그라운드 실행 완료: {url}\n이 창과 브라우저를 닫아도 PC가 켜져 있으면 계속 실행됩니다.", flush=True)
            if not opener(url):
                raise launch.LauncherError(f"백그라운드 프로그램은 실행 중입니다. 브라우저 주소창에 {url} 을 직접 입력하세요.")
            return 0
        existing = launch.existing_endpoint(launch.scan_endpoints(root))
        if existing:
            print("다른 실행 창에서 준비한 화면을 엽니다. 그 실행 창의 종료 방식은 그대로 유지됩니다.", flush=True)
            url = f"http://{launch.HOST}:{existing[0]}"
            if not opener(url):
                raise launch.LauncherError(f"프로그램은 실행 중입니다. 브라우저 주소창에 {url} 을 직접 입력하세요.")
            return 0
        if process.poll() is not None and not worker_owns_lock(root):
            raise launch.LauncherError(
                f"백그라운드 프로그램이 준비 전에 종료되었습니다 (종료 코드 {process.returncode}). "
                f"실행 기록: {log_path}. 파일이 비어 있으면 .runtime 폴더의 쓰기 권한과 Windows 보호 알림을 확인하세요.")
        time.sleep(0.25)
    raise launch.LauncherError(f"백그라운드 시작을 확인하지 못했습니다. {log_path} 파일의 마지막 오류를 확인하세요.")


def stop(root=launch.ROOT, *, timeout=60):
    root = Path(root).resolve()
    record = read_record(root)
    if not record:
        print("확인된 백그라운드 실행이 없습니다. 일반 실행 창은 그 창에서 Ctrl+C로 종료하세요.", flush=True)
        return 0
    if not request_shutdown(record):
        raise launch.LauncherError("백그라운드 실행을 확인할 수 없어 종료하지 않았습니다. 기존 실행 창과 .runtime 폴더의 가장 최근 background-로 시작하는 로그 파일을 확인하세요.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = read_record(root)
        if not latest or latest["startup_token"] != record["startup_token"]:
            print("백그라운드 프로그램을 종료했습니다.", flush=True)
            return 0
        time.sleep(0.25)
    raise launch.LauncherError("종료 처리가 진행 중입니다. 잠시 후 stop-background.bat을 다시 실행하세요.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args()
    if not args.serve:
        parser.error("Use start-background.bat or stop-background.bat")
    directory, _, _ = paths(launch.ROOT)
    log_path = args.log_file.resolve() if args.log_file else create_log_path(launch.ROOT)
    if log_path.parent != directory or not log_path.name.startswith("background-") or log_path.suffix != ".log":
        parser.error("Log files must be background-*.log files in this project's .runtime folder")
    # pythonw may start with stdout/stderr=None. Install streams before Uvicorn
    # creates its console logging handlers.
    with log_path.open("a", encoding="utf-8", buffering=1) as output:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            return serve(log_path=log_path)


if __name__ == "__main__":
    raise SystemExit(main())
