from email.message import EmailMessage
import unittest

from confirmation import find_confirmation_uid, parse_confirmation


SENDER = "zhouminjie1986@foxmail.com"
MAILBOX = "17014842@qq.com"
WEEK = "2026-11-02-11-08"
SUBJECT = f"[GitHub周榜贴图预览] {WEEK} · 示例项目"
MESSAGE_ID = "preview-123@example.test"


def make_message(*, sender=SENDER, to=MAILBOX, subject=f"Re: {SUBJECT}", body="确认", html=False,
                 plain_body="plain alternative that is intentionally ignored", in_reply_to=f"<{MESSAGE_ID}>"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = "<reply@example.test>"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if html:
        message.set_content(plain_body)
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


def authorized(raw):
    return parse_confirmation(raw, WEEK, expected_sender=SENDER, mailbox=MAILBOX,
                              preview_subject=SUBJECT, preview_message_id=MESSAGE_ID)


class ConfirmationTests(unittest.TestCase):
    def test_accepts_standalone_confirmation_in_html_reply(self):
        raw = make_message(body="<html><body><div>确认</div><blockquote>旧邮件里也有确认</blockquote></body></html>", html=True)
        self.assertTrue(authorized(raw))

    def test_rejects_confirmation_only_in_quoted_html(self):
        raw = make_message(body="<html><body><div>收到，谢谢。</div><blockquote>确认</blockquote></body></html>", html=True)
        self.assertFalse(authorized(raw))

    def test_rejects_qq_original_content_quote(self):
        raw = make_message(body="<html><body><div>收到，谢谢。</div><div id=\"original-content\"><br>确认</div></body></html>", html=True)
        self.assertFalse(authorized(raw))

    def test_nested_divs_inside_qq_quote_do_not_end_quote_early(self):
        raw = make_message(body=("<html><body><div>收到。</div>"
                                 "<div id=\"original-content\"><div><div>确认</div></div></div>"
                                 "</body></html>"), html=True)
        self.assertFalse(authorized(raw))

    def test_html_confirmation_is_used_instead_of_plain_alternative(self):
        raw = make_message(body="<html><body><div>确认</div><div id=\"original-content\">旧内容</div></body></html>",
                           html=True, plain_body="收到，谢谢，旧邮件内容如下：\n确认")
        self.assertTrue(authorized(raw))

    def test_rejects_confirmation_only_in_plain_quoted_reply(self):
        self.assertFalse(authorized(make_message(body="收到\n> 确认")))

    def test_accepts_confirmation_before_plain_quote(self):
        self.assertTrue(authorized(make_message(body="确认\n\n> 原邮件内容")))

    def test_rejects_wrong_sender_or_recipient(self):
        self.assertFalse(authorized(make_message(sender="someone@example.com")))
        self.assertFalse(authorized(make_message(to="other@example.com")))

    def test_rejects_an_unrelated_subject_and_message_id(self):
        raw = make_message(subject="Re: weekly report", in_reply_to="<other@example.test>")
        self.assertFalse(authorized(raw))

    def test_subject_match_requires_the_issue_week(self):
        raw = make_message(subject="Re: [GitHub周榜贴图预览] 2026-10-26 · 示例项目", in_reply_to=None)
        self.assertFalse(authorized(raw))

    def test_accepts_issue_token_without_preview_message_id(self):
        raw = make_message(subject="Re: weekly report [issue-token-abc]", in_reply_to=None)
        self.assertTrue(parse_confirmation(raw, WEEK, expected_sender=SENDER, mailbox=MAILBOX,
                                           preview_subject=SUBJECT, issue_token="issue-token-abc"))

    def test_quoted_issue_token_does_not_match(self):
        raw = make_message(subject="Re: weekly report", body="<div>确认</div><blockquote>issue-token-abc</blockquote>",
                           html=True, in_reply_to=None)
        self.assertFalse(parse_confirmation(raw, WEEK, expected_sender=SENDER, mailbox=MAILBOX,
                                            preview_subject=SUBJECT, issue_token="issue-token-abc"))


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            return "OK", [b"1 2 3"]
        if command == "FETCH":
            uid = args[0]
            return "OK", [(b"1 (BODY[] {size})".replace(b"{size}", str(len(self.messages[uid])).encode()), self.messages[uid])]
        raise AssertionError(f"unexpected IMAP command: {command}")


class IMAPTests(unittest.TestCase):
    def test_scans_seen_and_unseen_by_uid_with_peek_without_changing_flags(self):
        messages = {
            b"1": make_message(body="确认", subject="unrelated"),
            b"2": make_message(body="没有确认"),
            b"3": make_message(body="确认"),
        }
        imap = FakeIMAP(messages)
        found = find_confirmation_uid(imap, week_id=WEEK, expected_sender=SENDER, mailbox=MAILBOX,
                                      preview_subject=SUBJECT, preview_message_id=MESSAGE_ID)
        self.assertEqual(found, b"3")
        self.assertEqual(imap.calls[0], ("SEARCH", (None, "FROM", f'"{SENDER}"')))
        self.assertTrue(all(args[1] == "(BODY.PEEK[])" for command, args in imap.calls if command == "FETCH"))
        self.assertFalse(any(command == "STORE" for command, _ in imap.calls))


if __name__ == "__main__":
    unittest.main()
