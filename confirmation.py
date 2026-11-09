"""Strict email confirmation parsing and non-destructive IMAP helpers."""
from __future__ import annotations

from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default
from email.utils import getaddresses
from html.parser import HTMLParser
import re
from typing import Protocol


def _addresses(message: Message, header: str) -> set[str]:
    return {address.lower() for _, address in getaddresses(message.get_all(header, [])) if address}


def _header_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeError, ValueError):
        return value.strip()


class _VisibleHTML(HTMLParser):
    """Extract visible HTML text while dropping common quoted-reply blocks."""

    QUOTE_CLASSES = {"gmail_quote", "yahoo_quoted", "moz-cite-prefix", "protonmail_quote"}
    QUOTE_IDS = {
        "divrplyfwdmsg",
        "appendonsend",
        "mail-editor-reference-message-container",
        "original-content",
        "original_message",
    }
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK_TAGS = {"p", "div", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool, bool]] = []

    @property
    def skipping(self) -> bool:
        return any(skip for _, _, skip in self.stack)

    @property
    def quoting(self) -> bool:
        return any(quote for _, quote, _ in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): (value or "") for key, value in attrs}
        classes = set(attrs_dict.get("class", "").lower().split())
        element_id = attrs_dict.get("id", "").lower()
        tag = tag.lower()
        is_quote = tag == "blockquote" or bool(classes & self.QUOTE_CLASSES) or element_id in self.QUOTE_IDS
        is_skip = tag in {"script", "style", "head"}
        if not self.skipping and not self.quoting and tag in self.BLOCK_TAGS | {"br"}:
            self.parts.append("\n")
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, is_quote, is_skip))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        del self.stack[index:]
        if not self.skipping and not self.quoting and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping and not self.quoting:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def _html_text(source: str) -> str:
    parser = _VisibleHTML()
    try:
        parser.feed(source)
        parser.close()
        return parser.text()
    except Exception:
        return ""


_QUOTE_LINE = re.compile(
    r"^(?:On .+wrote:|在.+写道[:：]|发件人[:：]|From[:：]|原始邮件[:：]|-----Original Message-----|_{5,}|-{5,})\s*$",
    re.IGNORECASE,
)
_CONFIRM_LINE = re.compile(r"^\s*确认\s*[。.!！?？,，;；]*\s*$")


def _unquoted_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">") or _QUOTE_LINE.match(stripped):
            break
        lines.append(stripped)
    return lines


def _body_text(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            try:
                payload = part.get_content()
            except (LookupError, UnicodeError, ValueError):
                continue
            if isinstance(payload, str):
                (html if part.get_content_type() == "text/html" else plain).append(
                    _html_text(payload) if part.get_content_type() == "text/html" else payload
                )
    else:
        try:
            payload = message.get_content()
        except (LookupError, UnicodeError, ValueError):
            payload = ""
        if isinstance(payload, str):
            (html if message.get_content_type() == "text/html" else plain).append(
                _html_text(payload) if message.get_content_type() == "text/html" else payload
            )
    # text/plain and text/html in multipart/alternative are duplicate renderings
    # of one reply. Prefer HTML because it exposes explicit quote containers.
    return "\n".join(html or plain)


def _normalized_subject(subject: str) -> str:
    subject = re.sub(r"\s+", " ", subject).strip()
    while True:
        updated = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", subject, flags=re.IGNORECASE).strip()
        if updated == subject:
            return subject.casefold()
        subject = updated


def _message_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().strip("<>").casefold() for item in re.findall(r"<([^<>]+)>", value) if item.strip()}


def parse_confirmation(
    raw: bytes,
    week_id: str,
    *,
    expected_sender: str,
    mailbox: str,
    preview_subject: str,
    preview_message_id: str | None = None,
    issue_token: str | None = None,
) -> bool:
    """Return true only for a standalone confirmation in the intended thread.

    Matching may use the original preview's message id, its exact subject after
    reply prefixes, or an explicit per-issue token. Sender and recipient are
    always checked independently.
    """
    message = message_from_bytes(raw, policy=default)
    sender = _addresses(message, "From")
    recipients = _addresses(message, "To") | _addresses(message, "Cc")
    if expected_sender.lower() not in sender or mailbox.lower() not in recipients:
        return False

    subject_matches = _normalized_subject(_header_text(message.get("Subject"))) == _normalized_subject(preview_subject)
    expected_id = (preview_message_id or "").strip().strip("<>").casefold()
    thread_ids = _message_ids(message.get("In-Reply-To")) | _message_ids(message.get("References"))
    message_id_matches = bool(expected_id and expected_id in thread_ids)
    body = _body_text(message)
    unquoted_body = "\n".join(_unquoted_lines(body))
    token_matches = bool(issue_token and issue_token in (_header_text(message.get("Subject")) + "\n" + unquoted_body))
    # Subject fallback is constrained to this issue's week to prevent a generic
    # or stale reply from authorizing another preview.
    issue_matches = bool(week_id and week_id in _header_text(message.get("Subject")))
    if not (message_id_matches or token_matches or (subject_matches and issue_matches)):
        return False

    for line in _unquoted_lines(body):
        if _CONFIRM_LINE.fullmatch(line):
            return True
    return False


class UIDMailClient(Protocol):
    def uid(self, command: str, *args: object) -> tuple[object, list[object]]: ...


def find_confirmation_uid(
    imap: UIDMailClient,
    *,
    week_id: str,
    expected_sender: str,
    mailbox: str,
    preview_subject: str,
    preview_message_id: str | None = None,
    issue_token: str | None = None,
    limit: int = 100,
) -> bytes | None:
    """Find an authorized message using UID and BODY.PEEK[], including Seen mail.

    This helper never changes message flags. The caller may mark only the
    returned UID as Seen after it has handled the confirmation.
    """
    status, search_data = imap.uid("SEARCH", None, "FROM", f'"{expected_sender}"')
    if status != "OK" or not search_data or not search_data[0]:
        return None
    uids = search_data[0].split()[-max(1, limit):]
    for uid in reversed(uids):
        status, fetch_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK":
            continue
        raw = next((item[1] for item in fetch_data or [] if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)), None)
        if raw and parse_confirmation(raw, week_id, expected_sender=expected_sender, mailbox=mailbox,
                                      preview_subject=preview_subject, preview_message_id=preview_message_id,
                                      issue_token=issue_token):
            return uid
    return None
