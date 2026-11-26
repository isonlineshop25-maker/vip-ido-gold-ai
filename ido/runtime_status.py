"""Process-local heartbeats. Never include provider URLs or account details."""

import sqlite3
import threading
import time

DEADLINES = {"market": 45, "engine": 25, "validation": 60}


class WorkerStatus:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.started = clock()
        self.lock = threading.Lock()
        self.last_success = {}
        self.failed = set()

    def complete(self, worker, ok):
        with self.lock:
            if ok:
                self.last_success[worker] = (self.clock(), int(self.wall()))
                self.failed.discard(worker)
            else:
                self.failed.add(worker)

    def report(self):
        with self.lock:
            now = self.clock()
            result = {}
            for worker, deadline in DEADLINES.items():
                at, stamp = self.last_success.get(worker, (self.started, None))
                status = ("degraded" if worker in self.failed or now - at > deadline else
                          "starting" if stamp is None else "ok")
                result[worker] = dict(status=status, lastSuccessAt=stamp)
            return result


def sqlite_failure(exc):
    """Report only SQLite's numeric category; exception text can contain paths."""
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    return primary in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED), code
