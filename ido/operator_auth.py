"""Single-operator access for one Reserved VM process; no broker permissions."""

import hashlib
from pathlib import Path
import json
import os
import hmac
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

COOKIE = "__Host-ido_operator"
ACCOUNT = "private-operator"
def _login_days():
    try:
        return max(0, min(36500, int(os.environ.get("IDO_LOGIN_DAYS", "0"))))
    except ValueError:
        return 0


# 0 = never log out automatically: you stay logged in until you press "Uitloggen" or change the password.
ABSOLUTE_SECONDS = (_login_days() or 36500) * 86400
IDLE_SECONDS = ABSOLUTE_SECONDS
COOKIE_SECONDS = min(ABSOLUTE_SECONDS, 400 * 86400)   # browsers cap cookies at 400 days; it is renewed on every visit
MAX_SESSIONS = 8
ATTEMPTS = 6
ATTEMPT_WINDOW = 300
API_LIMIT = 180
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def valid_password(value):
    return (isinstance(value, str) and 12 <= len(value) <= 128
            and value == value.strip() and value.isprintable())


@dataclass
class Session:
    csrf: str
    created: float
    last_seen: float
    requests: deque = field(default_factory=deque)


class OperatorAuth:
    def __init__(self, password="", clock=time.monotonic, store=None):
        self.clock = clock
        self.salt = secrets.token_bytes(24)
        self.verifier = self._derive(password) if valid_password(password) else None
        self.sessions = {}
        self.attempts = deque()
        self.lock = threading.Lock()
        self.verifying = threading.Lock()
        # Logins survive a server restart. Only sha256 hashes of the cookies are stored, tied to this password:
        # changing the password logs every device out.
        self.store = Path(store) if store and self.verifier else None
        self.tag = hashlib.sha256(b"ido-login-v1:" + password.encode("utf-8")).hexdigest() if self.store else None
        self._load()

    def _load(self):
        if not self.store:
            return
        try:
            data = json.loads(self.store.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("tag") != self.tag:
            return
        now = self.clock()
        for key, csrf in list(data.get("sessions", {}).items())[-MAX_SESSIONS:]:
            if isinstance(key, str) and isinstance(csrf, str) and TOKEN.fullmatch(csrf) and len(key) == 64:
                try:
                    self.sessions[bytes.fromhex(key)] = Session(csrf, now, now)
                except ValueError:
                    continue

    def _save(self):
        if not self.store:
            return
        data = {"tag": self.tag, "sessions": {k.hex(): s.csrf for k, s in self.sessions.items()}}
        try:
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.store)
        except OSError:
            pass

    @property
    def configured(self):
        return self.verifier is not None

    def _derive(self, password):
        return hashlib.scrypt(password.encode("utf-8"), salt=self.salt,
                              n=65536, r=8, p=1, maxmem=96 * 1024 * 1024)

    @staticmethod
    def _key(value):
        return hashlib.sha256(value.encode("ascii")).digest() if TOKEN.fullmatch(value) else None

    def _prune(self, now):
        for key, s in list(self.sessions.items()):
            if now - s.created >= ABSOLUTE_SECONDS or now - s.last_seen >= IDLE_SECONDS:
                self.sessions.pop(key)

    def sign_in(self, password, replace=""):
        # Global budget: the hosted proxy's peer IP is not a safe per-client identity.
        # Bound concurrent scrypt work; successful logins do not consume a failed-attempt slot.
        if not self.verifying.acquire(blocking=False):
            return 429, None, 2
        try:
            now = self.clock()
            with self.lock:
                while self.attempts and now - self.attempts[0] >= ATTEMPT_WINDOW:
                    self.attempts.popleft()
                if len(self.attempts) >= ATTEMPTS:
                    return 429, None, max(1, int(self.attempts[0] + ATTEMPT_WINDOW - now) + 1)
            if not self.configured or not valid_password(password) or not hmac.compare_digest(self._derive(password), self.verifier):
                with self.lock:
                    self.attempts.append(now)
                return 401, None, 0
            with self.lock:
                self._prune(now)
                self.sessions.pop(self._key(replace), None)
                if len(self.sessions) >= MAX_SESSIONS:
                    oldest = min(self.sessions, key=lambda key: self.sessions[key].last_seen)
                    self.sessions.pop(oldest)
                value = secrets.token_urlsafe(32)
                self.sessions[self._key(value)] = Session(secrets.token_urlsafe(32), now, now)
                self._save()
            return 200, value, 0
        finally:
            self.verifying.release()

    def check(self, value, csrf=None):
        key = self._key(value)
        if key is None:
            return 401, None
        now = self.clock()
        with self.lock:
            self._prune(now)
            s = self.sessions.get(key)
            if s is None:
                return 401, None
            if csrf is not None and (not TOKEN.fullmatch(csrf) or not hmac.compare_digest(s.csrf, csrf)):
                return 403, None
            while s.requests and now - s.requests[0] >= 60:
                s.requests.popleft()
            if len(s.requests) >= API_LIMIT:
                return 429, None
            s.requests.append(now)
            s.last_seen = now
            return 200, s.csrf

    def revoke(self, value):
        with self.lock:
            self.sessions.pop(self._key(value), None)
            self._save()
