import base64
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from mail_service import MailService


class FakeResponse:
    def __init__(self, body=b"", status=202, lines=()):
        self.body = body
        self.status = status
        self.lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self.body

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class StopOnWait:
    def __init__(self):
        self.stopped = False
        self.waits = []
        self.stop_after = 1

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = len(self.waits) >= self.stop_after


class MailServiceTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {
            "MAIL_SERVICE_URL": "http://mail.test:3000/",
            "MAIL_SERVICE_API_KEY": "test-key",
        })
        env.start()
        self.addCleanup(env.stop)
        self.service = MailService()

    def test_send_includes_base64_attachments_and_returns_message_id(self):
        attachment = Path("report.png")
        response = FakeResponse(json.dumps({
            "ok": True,
            "messageId": "<sent-1@mail.test>",
            "accepted": ["Recipient <target@example.com>"],
            "rejected": [],
        }).encode())
        with mock.patch.object(Path, "read_bytes", return_value=b"file bytes"), \
             mock.patch.object(self.service.opener, "open", return_value=response) as open_request:
            result = self.service.send_mail("target@example.com", "Subject", "Body", [attachment])

        self.assertEqual(result, "<sent-1@mail.test>")
        request = open_request.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://mail.test:3000/api/send")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(payload["attachments"], [{
            "filename": "report.png",
            "contentBase64": base64.b64encode(b"file bytes").decode("ascii"),
            "contentType": "image/png",
        }])
        open_request.assert_called_once_with(request, timeout=45)

    def test_rejected_target_raises_without_retry(self):
        response = FakeResponse(json.dumps({
            "ok": True,
            "messageId": "message-2",
            "accepted": ["other@example.com"],
            "rejected": ["target@example.com"],
        }).encode())
        with mock.patch.object(self.service.opener, "open", return_value=response) as open_request:
            with self.assertRaisesRegex(RuntimeError, "did not accept recipient"):
                self.service.send_mail("target@example.com", "Subject", "Body")
        open_request.assert_called_once()

    def test_rejected_list_target_raises(self):
        response = FakeResponse(json.dumps({
            "ok": True,
            "accepted": ["target@example.com"],
            "rejected": ["TARGET@example.com"],
        }).encode())
        with mock.patch.object(self.service.opener, "open", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "rejected recipient"):
                self.service.send_mail("target@example.com", "Subject", "Body")

    def test_non_202_or_not_ok_response_raises(self):
        response = FakeResponse(json.dumps({"ok": False}).encode(), status=500)
        with mock.patch.object(self.service.opener, "open", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                self.service.send_mail("target@example.com", "Subject", "Body")

    def test_sse_ignores_heartbeat_and_parses_multiline_mail_data(self):
        lines = [
            b": heartbeat\n", b"\n", b"event: other\n", b"data: ignored\n", b"\n",
            b"event: mail\n", b'data: {"id":"mail-1",\n', b'data: "subject":"Hello"}\n', b"\n",
        ]
        mails = []
        response = FakeResponse(status=200, lines=lines)
        self.service._read_events(response, mails.append, threading.Event())
        self.assertEqual(mails, [{"id": "mail-1", "subject": "Hello"}])

    def test_subscription_reconnects_after_stream_ends(self):
        first = FakeResponse(status=200, lines=[b"event: mail\n", b'data: {"id":"mail-1"}\n', b"\n"])
        second = FakeResponse(status=200, lines=[])
        stop = StopOnWait()
        stop.stop_after = 2
        mails = []
        with mock.patch.object(self.service.opener, "open", side_effect=[first, second]) as open_request, \
             self.assertLogs("mail_service", level="ERROR"):
            self.service.subscribe_forever(mails.append, stop)
        self.assertEqual(mails, [{"id": "mail-1"}])
        self.assertEqual(stop.waits, [5, 5])
        self.assertEqual(open_request.call_count, 2)
        self.assertEqual(open_request.call_args_list[0].args[0].full_url,
                         "http://mail.test:3000/api/subscribe")

    def test_stream_error_reconnects_after_five_seconds(self):
        stop = StopOnWait()
        with mock.patch.object(self.service.opener, "open", side_effect=OSError("disconnected")) as open_request, \
             self.assertLogs("mail_service", level="ERROR"):
            self.service.subscribe_forever(lambda _mail: None, stop)
        open_request.assert_called_once()
        self.assertEqual(stop.waits, [5])


if __name__ == "__main__":
    unittest.main()
