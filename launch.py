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
CANDIDATE_PORTS = tuple(range(PORT, PORT + 11))


class LauncherError(RuntimeError):
    """An actionable local startup failure."""


def port_available(port):
    """Check binding too: Windows can reserve a port without a listener."""
    try:
        with socket.socket() as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((HOST, port))
        return True
    except OSError:
        return False


def probe_server(port=PORT, root=ROOT, expected_pid=None):
    """Use direct loopback HTTP, independent of browser/system proxy settings."""
    from app.instance import project_identity

    connection = http.client.HTTPConnection(HOST, port, timeout=0.8)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status == 200 and len(raw) <= 4096:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("status") == "ok" and data.get("service") == "kiwoom-breakout":
                if not data.get("project_id"):
                    return "legacy" if expected_pid is None else "other"
                if data.get("project_id") == project_identity(root) and data.get("instance_lock_version") == 1:
                    if expected_pid is None or data.get("pid") == expected_pid:
                        return "ready"
        return "other"
    except (OSError, ValueError, http.client.HTTPException):
        return "free" if port_available(port) else "busy"
    finally:
        connection.close()


def _listener_processes(port):
    """Read owner names/commands privately; never log command lines or keys."""
    if os.name == "nt":
        script = (
            "$ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "$owners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | "
            f"Where-Object {{$_.LocalPort -eq {int(port)}}} | "
            "Select-Object -ExpandProperty OwningProcess -Unique); "
            "$rows=@(foreach($owner in $owners){ "
            "Get-CimInstance Win32_Process -Filter ('ProcessId = '+$owner) | "
            "Select-Object Name,CommandLine }); "
            "ConvertTo-Json -InputObject $rows -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                return None
            rows = json.loads(result.stdout)
            return rows if isinstance(rows, list) else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    if sys.platform.startswith("linux"):
        try:
            inodes = set()
            for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
                for line in table.read_text().splitlines()[1:]:
                    fields = line.split()
                    if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(fields[9])
            if not inodes:
                return []
            socket_links = {f"socket:[{inode}]" for inode in inodes}
            rows = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    owns_socket = False
                    for link in (entry / "fd").iterdir():
                        try:
                            if link.readlink().as_posix() in socket_links:
                                owns_socket = True
                                break
                        except OSError:
                            continue  # File descriptors can close during inspection.
                    if owns_socket:
                        rows.append({"Name": (entry / "comm").read_text().strip(),
                                     "CommandLine": (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")})
                except (OSError, PermissionError):
                    continue
            return rows or None
        except (OSError, ValueError, IndexError):
            return None
    return None


def unsafe_legacy_listener(port):
    """An old unresponsive trader has no data lock; do not duplicate it."""
    rows = _listener_processes(port)
    if rows is None:
        return True
    for row in rows:
        name = str(row.get("Name") or "").lower()
        command = str(row.get("CommandLine") or "").lower()
        if "app.main:app" in command or "uvicorn" in name or "uvicorn" in command:
            return True
        if name.startswith(("python", "pypy")) or name in {"py", "py.exe"}:
            # This known unrelated server is useful for local development.
            if "-m http.server" not in command:
                return True
        elif not name:
            return True
    return False


def scan_endpoints(root):
    return [(port, probe_server(port, root)) for port in CANDIDATE_PORTS]


def existing_endpoint(endpoints):
    for status in ("ready", "legacy"):
        for port, state in endpoints:
            if state == status and (status != "legacy" or port == PORT):
                return port, state
    return None


def choose_endpoint(root=ROOT, busy_timeout=3, poll_interval=0.25):
    endpoints = scan_endpoints(root)
    deadline = time.monotonic() + busy_timeout
    while True:
        existing = existing_endpoint(endpoints)
        if existing:
            return existing
        if not any(state == "busy" for _, state in endpoints) or time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
        endpoints = scan_endpoints(root)
    for port, state in endpoints:
        if state == "legacy" or (state in {"busy", "other"} and unsafe_legacy_listener(port)):
            raise LauncherError(
                f"{port}번의 기존 서버가 이 프로그램의 이전 실행인지 확인할 수 없습니다. "
                "이전에 프로그램을 실행했던 검은 창에서 Ctrl+C를 누르고 start.bat을 다시 실행하세요."
            )
    for port, state in endpoints:
        if state == "free":
            return port, state
    raise LauncherError("8000~8010번에 사용할 수 있는 포트가 없습니다. 기존 프로그램 실행 창을 확인하세요.")


def wait_for_existing(root=ROOT, timeout=45, poll_interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        existing = existing_endpoint(scan_endpoints(root))
        if existing:
            return existing[0]
        time.sleep(poll_interval)
    raise LauncherError("다른 실행 창의 준비가 끝나지 않았습니다. 그 창에 표시된 오류를 확인하세요.")


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


def wait_for_ready(process=None, timeout=45, poll_interval=0.25, port=PORT, root=ROOT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise LauncherError("프로그램이 시작 중 종료되었습니다. 위 실행 오류를 확인하세요.")
        if probe_server(port, root, expected_pid=process.pid if process is not None else None) == "ready":
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
        root = Path(root).resolve()
        existing = existing_endpoint(scan_endpoints(root))
        if existing:
            url = f"http://{HOST}:{existing[0]}"
            print("이미 실행 중인 프로그램 화면을 엽니다. 기존 실행 창은 계속 켜두세요.", flush=True)
            if not opener(url):
                raise LauncherError(f"브라우저 주소창에 {url} 을 직접 입력하세요.")
            return 0
        lock = InstanceLock()
        if not lock.acquire():
            print("다른 실행 창이 준비 중입니다. 준비가 끝나면 화면을 엽니다.", flush=True)
            port = wait_for_existing(root, timeout=startup_timeout)
            url = f"http://{HOST}:{port}"
            if not opener(url):
                raise LauncherError(f"브라우저 주소창에 {url} 을 직접 입력하세요.")
            return 0
        port, state = choose_endpoint(root)
        if state in {"ready", "legacy"}:
            url = f"http://{HOST}:{port}"
            if not opener(url):
                raise LauncherError(f"브라우저 주소창에 {url} 을 직접 입력하세요.")
            return 0
        python = ensure_runtime(root)
        options = {"cwd": root}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        for attempt in range(3):
            # Installation and manual starts can change availability. Recheck
            # while holding the launcher lock, then verify the actual child PID.
            port, state = choose_endpoint(root)
            url = f"http://{HOST}:{port}"
            if state in {"ready", "legacy"}:
                if not opener(url):
                    raise LauncherError(f"브라우저 주소창에 {url} 을 직접 입력하세요.")
                return 0
            if port != PORT:
                print(f"8000번 대신 사용 가능한 {port}번 포트로 엽니다. 주소: {url}", flush=True)
            command = [str(python), "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(port)]
            print("프로그램을 시작합니다. 화면이 열릴 때까지 잠시 기다려주세요.", flush=True)
            process = subprocess.Popen(command, **options)
            try:
                wait_for_ready(process, timeout=startup_timeout, port=port, root=root)
            except LauncherError:
                exited = process.poll() is not None
                stop_owned_server(process)
                process = None
                if exited and not port_available(port) and attempt < 2:
                    print("시작 중 포트 사용 상태가 바뀌었습니다. 사용할 주소를 다시 확인합니다.", flush=True)
                    continue
                raise
            print("이 창은 프로그램 실행 창입니다. 사용하는 동안 닫지 마세요.", flush=True)
            print("화면에서 감시 시작을 눌러야 매매를 시작합니다. 종료는 이 창에서 Ctrl+C를 누르세요.", flush=True)
            if not opener(url):
                print(f"Brave 또는 기본 브라우저 주소창에 {url} 을 직접 입력하세요.", flush=True)
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
