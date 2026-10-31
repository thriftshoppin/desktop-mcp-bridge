"""Control-pipe hardening for the guardian (audit items DB-1 / DB-2): a read that cannot hang forever, and a cap on concurrent control connections.

Pure logic plus one Win32 call (CancelIoEx), so it is unit-testable without starting a guardian.
"""
import ctypes
import threading


def read_with_timeout(handle, nbytes: int, timeout_s: float) -> bytes:
    """ReadFile on a synchronous pipe handle that gives up after `timeout_s`: a watchdog calls CancelIoEx (the only safe way to
    interrupt a blocked synchronous read from another thread; closing the handle could block on the same I/O lock). Raises TimeoutError."""
    import win32file
    fired = threading.Event()

    def cancel() -> None:
        fired.set()
        ctypes.windll.kernel32.CancelIoEx(ctypes.c_void_p(int(handle)), None)

    timer = threading.Timer(timeout_s, cancel)
    timer.daemon = True
    timer.start()
    try:
        return win32file.ReadFile(handle, nbytes)[1]
    except Exception:
        if fired.is_set():
            raise TimeoutError(f"no request within {timeout_s:g} s") from None
        raise
    finally:
        timer.cancel()


class ConnLimiter:
    """Caps for control connections. `per_pid`: every open connection of one client process (waiting for the user's Allow answer counts too).
    `total_unread`: connections that have connected but not yet sent their request (these are the ones a flood of silent clients would pile up).
    A connection that already sent its request (e.g. waiting for the user to answer 'Allow client?') no longer counts against `total_unread`."""

    def __init__(self, per_pid: int = 4, total_unread: int = 32):
        self.per_pid = per_pid
        self.total_unread = total_unread
        self._lock = threading.Lock()
        self._conns: dict[int, list] = {}   # id -> [pid, read_done]
        self._n = 0

    def admit(self, pid: int):
        """-> connection id, or None when a cap is hit (the caller closes the connection at once)."""
        with self._lock:
            if sum(1 for p, _ in self._conns.values() if p == pid) >= self.per_pid:
                return None
            if sum(1 for _, done in self._conns.values() if not done) >= self.total_unread:
                return None
            self._n += 1
            self._conns[self._n] = [pid, False]
            return self._n

    def request_read(self, cid: int) -> None:
        with self._lock:
            if cid in self._conns:
                self._conns[cid][1] = True

    def release(self, cid: int) -> None:
        with self._lock:
            self._conns.pop(cid, None)

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return len(self._conns), sum(1 for _, done in self._conns.values() if not done)
