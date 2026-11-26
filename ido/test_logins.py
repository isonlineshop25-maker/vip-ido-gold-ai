import os
import tempfile
import unittest
from pathlib import Path

from operator_auth import OperatorAuth

PW = "mijn-eigen-goudbot-1"


class LoginStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Path(self.dir.name) / "logins.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_login_survives_restart(self):
        status, token, _ = OperatorAuth(PW, store=self.store).sign_in(PW)
        self.assertEqual(status, 200)
        self.assertNotIn(token, self.store.read_text())          # only hashes on disk
        self.assertEqual(oct(os.stat(self.store).st_mode & 0o777), "0o600")
        self.assertEqual(OperatorAuth(PW, store=self.store).check(token)[0], 200)

    def test_new_password_logs_everyone_out(self):
        _, token, _ = OperatorAuth(PW, store=self.store).sign_in(PW)
        self.assertEqual(OperatorAuth("een-heel-ander-wachtwoord", store=self.store).check(token)[0], 401)

    def test_logout_is_remembered(self):
        auth = OperatorAuth(PW, store=self.store)
        _, token, _ = auth.sign_in(PW)
        auth.revoke(token)
        self.assertEqual(OperatorAuth(PW, store=self.store).check(token)[0], 401)

    def test_twelve_characters_allowed(self):
        self.assertEqual(OperatorAuth("Goud-Ido2026", store=self.store).sign_in("Goud-Ido2026")[0], 200)
        self.assertFalse(OperatorAuth("kort").configured)


if __name__ == "__main__":
    unittest.main()
