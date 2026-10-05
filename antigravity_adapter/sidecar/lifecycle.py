"""Windows lifecycle ownership for the Antigravity Sidecar."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Callable


ERROR_ALREADY_EXISTS = 183
ERROR_FILE_EXISTS = 80
INFINITE = 0xFFFFFFFF
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MOVEFILE_WRITE_THROUGH = 0x8
WAIT_OBJECT_0 = 0


class LifecycleFailure(RuntimeError):
    pass


def mutex_name(rendezvous: Path) -> str:
    canonical = os.path.normcase(str(rendezvous.resolve(strict=False)))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return rf"Local\AgentDiscordBridge.Sidecar.{digest}"


class WindowsMutex:
    def __init__(self, name: str) -> None:
        if os.name != "nt":
            raise LifecycleFailure("parent_watch_unavailable")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise LifecycleFailure("instance_conflict")
        self._kernel32 = kernel32
        self._handle = handle
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            self._handle = None
            raise LifecycleFailure("instance_conflict")

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._kernel32.CloseHandle(handle)


class ParentHandle:
    def __init__(self, parent_pid: int | None = None, *, _kernel32: object | None = None, _worker_created: int | None = None) -> None:
        if os.name != "nt":
            raise LifecycleFailure("parent_watch_unavailable")
        self.parent_pid = parent_pid if parent_pid is not None else os.getppid()
        kernel32 = _kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD]
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        worker_created = _worker_created if _worker_created is not None else _creation_time(kernel32, kernel32.GetCurrentProcess())
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, self.parent_pid)
        if not handle:
            raise LifecycleFailure("parent_watch_unavailable")
        try:
            parent_created = _creation_time(kernel32, handle)
            if parent_created > worker_created:
                raise LifecycleFailure("parent_watch_unavailable")
            cancel = kernel32.CreateEventW(None, True, False, None)
            if not cancel:
                raise LifecycleFailure("parent_watch_unavailable")
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        self._kernel32 = kernel32
        self._handle = handle
        self._cancel = cancel
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    def watch(self, callback: Callable[[], None]) -> threading.Thread:
        if self._thread is not None:
            raise LifecycleFailure("parent_watch_unavailable")
        handles = (wintypes.HANDLE * 2)(self._handle, self._cancel)

        def wait() -> None:
            outcome = self._kernel32.WaitForMultipleObjects(2, handles, False, INFINITE)
            if outcome == WAIT_OBJECT_0:
                callback()

        thread = threading.Thread(target=wait, name="sidecar-parent-watch", daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._kernel32.SetEvent(self._cancel)
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._lock:
            handle, cancel = self._handle, self._cancel
            self._handle = self._cancel = None
        if handle:
            self._kernel32.CloseHandle(handle)
        if cancel:
            self._kernel32.CloseHandle(cancel)


def _creation_time(kernel32: object, handle: object) -> int:
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise LifecycleFailure("parent_watch_unavailable")
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def publish_exclusive(path: Path, data: str) -> None:
    if os.name != "nt":
        raise LifecycleFailure("rendezvous_conflict")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _move_no_replace(Path(temporary), path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _move_no_replace(source: Path, destination: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    if not kernel32.MoveFileExW(str(source), str(destination), MOVEFILE_WRITE_THROUGH):
        error = ctypes.get_last_error()
        if error in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
            raise LifecycleFailure("rendezvous_conflict")
        raise LifecycleFailure("rendezvous_conflict")


def classify_existing_rendezvous(path: Path, timeout: float = 1.0) -> None:
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        host, port = value["host"], value["port"]
        instance, token = value["instanceId"], value["token"]
        if host != "127.0.0.1" or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError
        if not all(isinstance(item, str) and item for item in (instance, token)):
            raise ValueError
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}", "X-Sidecar-Instance": instance},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and body.get("status") == "ready":
                raise LifecycleFailure("live_instance_conflict")
            raise LifecycleFailure("rendezvous_conflict")
    except LifecycleFailure:
        raise
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
            raise LifecycleFailure("stale_rendezvous") from exc
        raise LifecycleFailure("rendezvous_conflict") from exc
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LifecycleFailure("rendezvous_conflict") from exc


def remove_owned_rendezvous(path: Path, instance_id: str, token: str) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current.get("instanceId") == instance_id and current.get("token") == token:
            path.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        return


class Lifecycle:
    def __init__(self, mutex: WindowsMutex, parent: ParentHandle) -> None:
        self.mutex = mutex
        self.parent = parent
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.parent.close()
            self.mutex.close()


class ExclusiveSocketMixin:
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
