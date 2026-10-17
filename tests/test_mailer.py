"""mailer.send must never raise: a CR/LF in a header value used to escape as ValueError
because the message was built outside the try block. SMTP itself is faked.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_mailer.py < /dev/null
"""

from __future__ import annotations

import os
import unittest

os.environ["SMTP_HOST"] = "smtp.test.invalid"
os.environ["MAIL_FROM"] = "post@sporlos.no"

from app import mailer  # noqa: E402


class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


class MailerTest(unittest.TestCase):
    def setUp(self):
        self._real = mailer.smtplib.SMTP
        mailer.smtplib.SMTP = _FakeSMTP
        _FakeSMTP.sent.clear()

    def tearDown(self):
        mailer.smtplib.SMTP = self._real

    def test_normal_mail_is_sent(self):
        self.assertTrue(mailer.send("a@example.no", "Hei", "Tekst"))
        self.assertEqual(len(_FakeSMTP.sent), 1)

    def test_line_break_in_subject_returns_false_instead_of_raising(self):
        self.assertFalse(mailer.send("a@example.no", "Ny kunde: X\nBcc: b@example.no", "Tekst"))
        self.assertEqual(_FakeSMTP.sent, [])

    def test_line_break_in_recipient_returns_false_instead_of_raising(self):
        self.assertFalse(mailer.send("a@example.no\r\nBcc: b@example.no", "Hei", "Tekst"))
        self.assertEqual(_FakeSMTP.sent, [])


if __name__ == "__main__":
    unittest.main()
