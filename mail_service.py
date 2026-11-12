"""HTTP client for the local QQ Postman mail service."""

from __future__ import annotations

import base64
from email.utils import parseaddr
import json
import logging
import mimetypes
import os
from pathlib import Path
import threading
from typing import Callable, Iterable
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


LOGGER = logging.getLogger(__name__)
_REQUEST_TIMEOUT = 45
_RECONNECT_SECONDS = 5


class MailService:
    def __init__(self) -> None:
        self.base_url = os.environ.get("MAIL_SERVICE_URL", "http://192.168.0.126:3000").strip().rstrip("/")
        self.api_key = os.environ.get("MAIL_SERVICE_API_KEY", "").strip()
        self.opener = build_opener(ProxyHandler({}))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    @staticmethod
    def _recipient_matches(value: object, target: str) -> bool:
        if not isinstance(value, str):
            return False
        address = parseaddr(value)[1] or value
        return address.strip().casefold() == target.strip().casefold()

    def send_mail(
        self,
        to: str,
        subject: str,
        text: str,
        attachments: Iterable[Path] = (),
    ) -> str | None:
        encoded_attachments = []
        for path in attachments:
            path = Path(path)
            encoded_attachments.append({
                "filename": path.name,
                "contentBase64": base64.b64encode(path.read_bytes()).decode("ascii"),
                "contentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            })

        body = json.dumps({
            "to": to,
            "subject": subject,
            "text": text,
            "attachments": encoded_attachments,
        }).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}/api/send", data=body, headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=_REQUEST_TIMEOUT) as response:
                status = response.getcode()
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Mail service returned HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"Mail service send failed: {exc}") from exc

        if status != 202 or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(f"Mail service rejected send: HTTP {status}, response={payload!r}")
        accepted = payload.get("accepted")
        rejected = payload.get("rejected")
        if not isinstance(accepted, list) or not isinstance(rejected, list):
            raise RuntimeError("Mail service response omitted accepted/rejected recipients")
        if not any(self._recipient_matches(item, to) for item in accepted):
            raise RuntimeError(f"Mail service did not accept recipient {to}")
        if any(self._recipient_matches(item, to) for item in rejected):
            raise RuntimeError(f"Mail service rejected recipient {to}")
        message_id = payload.get("messageId")
        return message_id if isinstance(message_id, str) and message_id else None

    def subscribe_forever(
        self,
        on_mail: Callable[[dict], None],
        stop: threading.Event,
    ) -> None:
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        while not stop.is_set():
            try:
                request = Request(f"{self.base_url}/api/subscribe", headers=headers, method="GET")
                with self.opener.open(request, timeout=_REQUEST_TIMEOUT) as response:
                    if response.getcode() != 200:
                        raise RuntimeError(f"Mail subscription returned HTTP {response.getcode()}")
                    self._read_events(response, on_mail, stop)
                if not stop.is_set():
                    raise ConnectionError("Mail subscription closed")
            except Exception:
                if not stop.is_set():
                    LOGGER.exception("Mail subscription failed; reconnecting")
                    stop.wait(_RECONNECT_SECONDS)

    @staticmethod
    def _read_events(response, on_mail: Callable[[dict], None], stop: threading.Event) -> None:
        event = ""
        data_lines: list[str] = []
        while not stop.is_set():
            raw_line = response.readline()
            if not raw_line:
                return
            line = raw_line.decode("utf-8").rstrip("\r\n") if isinstance(raw_line, bytes) else raw_line.rstrip("\r\n")
            if line == "":
                if event == "mail" and data_lines:
                    payload = json.loads("\n".join(data_lines))
                    if not isinstance(payload, dict):
                        raise ValueError("Mail event payload must be a JSON object")
                    on_mail(payload)
                event = ""
                data_lines.clear()
            elif line.startswith(":"):
                continue
            elif ":" in line:
                field, value = line.split(":", 1)
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event = value
                elif field == "data":
                    data_lines.append(value)

