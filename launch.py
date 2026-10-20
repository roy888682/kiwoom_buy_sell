"""Double-click launcher: prepare the runtime, serve locally, then open Brave.

Only this launcher's child process is stopped on exit. Reusing an already healthy
instance never changes its trading state or closes it. Credentials stay in the
existing environment/.env and are never read or printed by this launcher.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"


class LauncherError(RuntimeError):
    """An actionable local startup failure."""


def probe_server():
    """Distinguish this dashboard, an unrelated listener, and a free port."""
    try:
        with socket.create_connection((HOST, PORT), timeout=0.8):
            pass
    except ConnectionRefusedError:
        return "free"
    except OSError:
        return "busy"
    connection = http.client.HTTPConnection(HOST, PORT, timeout=0.8)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status == 200 and len(raw) <= 4096:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("status") == "ok" and data.get("service") == "kiwoom-breakout":
                return "ready"
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return "busy"


class InstanceLock:
    """OS-released lock shared by launchers for this user's local port.

    Keep the lock file after release: unlinking permits a second inode to be
    locked concurrently. No account information is written into this file.
    """
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else Path(tempfile.gettempdir()) / "kiwoom-breakout-127001-8000.lock"
        self.file = None

    def acquire(self):
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self.file = os.fdopen(descriptor, "r+b")
        if os.fstat(descriptor).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def release(self):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file, fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


def find_brave():
    if os.name == "nt":
        for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(name)
            if base:
                candidate = Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"
                if candidate.is_file():
                    return candidate
    for name in ("brave-browser", "brave", "brave.exe"):
        candidate = shutil.which(name)
        if candidate:
            return Path(candidate)
    return None


def open_dashboard(url=URL):
    brave = find_brave()
    if brave is not None:
        try:
            subprocess.Popen([str(brave), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False


def _python_candidates():
    candidates = [[sys.executable]]
    if os.name == "nt" and shutil.which("py"):
        candidates.extend([["py", "-3.13"], ["py", "-3.12"], ["py", "-3.11"]])
    return candidates


def _succeeds(command, timeout=30):
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _uv_command():
    executable = shutil.which("uv")
    if executable:
        return [executable]
    candidates = _python_candidates()
    for python in candidates:
        if _succeeds(python + ["-m", "uv", "--version"]):
            return python + ["-m", "uv"]
    for python in candidates:
        # A uv-managed venv may not contain pip; use an existing installed Python.
        if _succeeds(python + ["-m", "pip", "--version"]):
            print("실행 도구 uv를 설치합니다. 처음 한 번은 인터넷 연결이 필요합니다.", flush=True)
            try:
                subprocess.run(python + ["-m", "pip", "install", "uv"], check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise LauncherError("uv 설치에 실패했습니다. 위 설치 오류와 인터넷 연결을 확인하세요.") from exc
            return python + ["-m", "uv"]
    raise LauncherError("Python 설치에 pip가 필요합니다. Python 3.11~3.13 설치 옵션을 확인하세요.")


def ensure_runtime(root=ROOT):
    root = Path(root)
    if not (root / "pyproject.toml").is_file() or not (root / "uv.lock").is_file():
        raise LauncherError("start.bat와 launch.py를 pyproject.toml이 있는 프로그램 폴더에 넣어주세요.")
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    check = "import sys; assert (3,11)<=sys.version_info[:2]<(3,14); import uvicorn,fastapi,httpx,exchange_calendars,dotenv,websockets"
    if python.is_file() and _succeeds([str(python), "-c", check]):
        return python
    print("실행 환경을 준비합니다. 처음 실행하거나 부품이 없을 때만 설치합니다.", flush=True)
    try:
        subprocess.run(_uv_command() + ["sync", "--frozen", "--python", sys.executable], cwd=root, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LauncherError("실행 환경 설치에 실패했습니다. 위 오류와 인터넷 연결을 확인하세요.") from exc
    if not python.is_file() or not _succeeds([str(python), "-c", check]):
        raise LauncherError("설치 후에도 필요한 부품을 확인할 수 없습니다. 이 창의 오류를 확인하세요.")
    return python


def wait_for_ready(process=None, timeout=45, poll_interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise LauncherError("프로그램이 시작 중 종료되었습니다. 위 실행 오류를 확인하세요.")
        if probe_server() == "ready":
            return
        time.sleep(poll_interval)
    raise LauncherError("프로그램 시작을 확인하지 못했습니다. 기존 실행 창과 위 오류를 확인하세요.")


def stop_owned_server(process, grace=20):
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        process.wait(timeout=grace)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run(root=ROOT, opener=None, startup_timeout=45):
    opener = opener or open_dashboard
    lock = None
    process = None
    try:
        if not (3, 11) <= sys.version_info[:2] < (3, 14):
            raise LauncherError("Python 3.11~3.13을 사용하세요.")
        state = probe_server()
        if state == "ready":
            print("이미 실행 중인 프로그램 화면을 엽니다. 기존 실행 창은 계속 켜두세요.", flush=True)
            if not opener(URL):
                raise LauncherError(f"브라우저 주소창에 {URL} 을 직접 입력하세요.")
            return 0
        if state == "busy":
            raise LauncherError("8000번 포트를 다른 프로그램이 사용 중이거나 기존 실행이 준비 중입니다. 기존 창을 확인하세요.")
        lock = InstanceLock()
        if not lock.acquire():
            print("다른 실행 창이 준비 중입니다. 준비가 끝나면 화면을 엽니다.", flush=True)
            wait_for_ready(timeout=startup_timeout)
            if not opener(URL):
                raise LauncherError(f"브라우저 주소창에 {URL} 을 직접 입력하세요.")
            return 0
        # Another launcher or a manual start may have finished since the first probe.
        state = probe_server()
        if state == "ready":
            if not opener(URL):
                raise LauncherError(f"브라우저 주소창에 {URL} 을 직접 입력하세요.")
            return 0
        if state != "free":
            raise LauncherError("8000번 포트가 사용 중입니다. 기존 실행 창을 확인하세요.")
        python = ensure_runtime(Path(root))
        state = probe_server()
        if state == "ready":
            if not opener(URL):
                raise LauncherError(f"브라우저 주소창에 {URL} 을 직접 입력하세요.")
            return 0
        if state != "free":
            raise LauncherError("8000번 포트가 사용 중입니다. 기존 실행 창을 확인하세요.")
        command = [str(python), "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)]
        options = {"cwd": Path(root)}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        print("프로그램을 시작합니다. 화면이 열릴 때까지 잠시 기다려주세요.", flush=True)
        process = subprocess.Popen(command, **options)
        wait_for_ready(process, timeout=startup_timeout)
        print("이 창은 프로그램 실행 창입니다. 사용하는 동안 닫지 마세요.", flush=True)
        print("화면에서 감시 시작을 눌러야 매매를 시작합니다. 종료는 이 창에서 Ctrl+C를 누르세요.", flush=True)
        if not opener(URL):
            print(f"Brave 또는 기본 브라우저 주소창에 {URL} 을 직접 입력하세요.", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        print("\n프로그램을 종료합니다. 잠시 기다려주세요.", flush=True)
        return 0
    except (LauncherError, OSError) as exc:
        print(f"\n실행하지 못했습니다: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if process is not None:
            stop_owned_server(process)
        if lock is not None:
            lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start Breakline locally and open Brave or your default browser.")
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
