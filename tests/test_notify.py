"""Tests for warehouse/notify.py — the Slack/Google Chat/email push utility.

Hermetic: no network, no real webhook URLs or SMTP credentials.
`requests.post` and `smtplib.SMTP` are both monkeypatched.
"""
from __future__ import annotations

import os
import unittest
from email import message_from_string
from unittest.mock import Mock, patch

from warehouse import notify


class ToChatMarkdownTests(unittest.TestCase):
    def test_h1_becomes_bold_line(self) -> None:
        self.assertEqual(notify.to_chat_markdown("# Weekly Digest — week"),
                          "*Weekly Digest — week*")

    def test_h2_becomes_bold_line(self) -> None:
        self.assertEqual(notify.to_chat_markdown("## Top wins"), "*Top wins*")

    def test_double_star_bold_becomes_single_star(self) -> None:
        self.assertEqual(notify.to_chat_markdown("**Verdict:** good"),
                          "*Verdict:* good")

    def test_bullets_pass_through_unchanged(self) -> None:
        self.assertEqual(notify.to_chat_markdown("- a win"), "- a win")

    def test_multiline_digest(self) -> None:
        md = "# Weekly Digest — week of 2026-07-27\n\n**On track**\n\n## Top wins\n- thing one"
        out = notify.to_chat_markdown(md)
        self.assertIn("*Weekly Digest — week of 2026-07-27*", out)
        self.assertIn("*On track*", out)
        self.assertIn("*Top wins*", out)
        self.assertIn("- thing one", out)


class TargetsTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in list(os.environ):
            if key.endswith(("_SLACK_WEBHOOK", "_GCHAT_WEBHOOK", "_EMAIL_TO")):
                os.environ.pop(key)

    def test_no_env_vars_yields_empty(self) -> None:
        self.assertEqual(notify._targets("weekly_digest"), {})

    def test_dest_prefix_is_uppercased(self) -> None:
        os.environ["WEEKLY_DIGEST_SLACK_WEBHOOK"] = "https://hooks.slack.test/x"
        self.assertEqual(notify._targets("weekly_digest"),
                          {"slack": "https://hooks.slack.test/x"})

    def test_only_configured_platform_is_returned(self) -> None:
        os.environ["WEEKLY_DIGEST_GCHAT_WEBHOOK"] = "https://chat.googleapis.test/x"
        targets = notify._targets("weekly_digest")
        self.assertEqual(targets, {"google_chat": "https://chat.googleapis.test/x"})

    def test_email_recipients_included_when_configured(self) -> None:
        os.environ["WEEKLY_DIGEST_EMAIL_TO"] = "a@example.com,b@example.com"
        self.assertEqual(notify._targets("weekly_digest"),
                          {"email": "a@example.com,b@example.com"})

    def test_all_three_platforms_can_coexist(self) -> None:
        os.environ["WEEKLY_DIGEST_SLACK_WEBHOOK"] = "https://hooks.slack.test/x"
        os.environ["WEEKLY_DIGEST_GCHAT_WEBHOOK"] = "https://chat.googleapis.test/x"
        os.environ["WEEKLY_DIGEST_EMAIL_TO"] = "a@example.com"
        self.assertEqual(set(notify._targets("weekly_digest")),
                          {"slack", "google_chat", "email"})


class EmailSubjectTests(unittest.TestCase):
    def test_strips_bold_markup(self) -> None:
        self.assertEqual(notify._email_subject("*Weekly Inventory Digest — 2026-08-06*"),
                          "Weekly Inventory Digest — 2026-08-06")

    def test_strips_bullet_markup(self) -> None:
        self.assertEqual(notify._email_subject("• some bullet"), "some bullet")

    def test_uses_first_line_only(self) -> None:
        self.assertEqual(notify._email_subject("*Title*\nSecond line\nThird line"), "Title")

    def test_empty_text_falls_back_to_default(self) -> None:
        self.assertEqual(notify._email_subject(""), "Warehouse notification")


class ToEmailHtmlTests(unittest.TestCase):
    def test_bold_becomes_b_tag(self) -> None:
        out = notify.to_email_html("*Weekly Inventory Digest — 2026-08-06*")
        self.assertIn("<b>Weekly Inventory Digest — 2026-08-06</b>", out)
        self.assertNotIn("*Weekly", out)

    def test_bullet_lines_become_a_list(self) -> None:
        out = notify.to_email_html("*Shipments*\n• PO-1001 — 897 units\n• PO-1002 — 312 units")
        self.assertIn("<ul", out)
        self.assertIn("<li>PO-1001 — 897 units</li>", out)
        self.assertIn("<li>PO-1002 — 312 units</li>", out)

    def test_labeled_link_line_becomes_a_clean_hyperlink(self) -> None:
        # "Label: url" (a common report link line) shows the LABEL as the
        # visible text, not the raw URL repeated.
        out = notify.to_email_html("Full report: https://docs.google.com/spreadsheets/d/abc123/edit")
        self.assertIn('<a href="https://docs.google.com/spreadsheets/d/abc123/edit">'
                      "Full report</a>", out)
        self.assertNotIn(">https://docs.google.com", out)

    def test_bare_url_with_no_label_is_still_linkified(self) -> None:
        out = notify.to_email_html("see https://example.com/x for details")
        self.assertIn('<a href="https://example.com/x">https://example.com/x</a>', out)

    def test_html_special_characters_are_escaped(self) -> None:
        out = notify.to_email_html("Product <Fancy & Co> arrived")
        self.assertIn("&lt;Fancy &amp; Co&gt;", out)
        self.assertNotIn("<Fancy", out)

    def test_blank_line_ends_a_list(self) -> None:
        out = notify.to_email_html("• item one\n\n*Next section*")
        # the closing </ul> must appear before the next section's div
        self.assertLess(out.index("</ul>"), out.index("Next section"))

    def test_wrapped_in_html_document(self) -> None:
        out = notify.to_email_html("hello")
        self.assertTrue(out.startswith("<html>"))
        self.assertTrue(out.endswith("</html>"))


_SMTP_ENV = {
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "sender@example.com",
    "SMTP_PASSWORD": "app-password",
    "SMTP_FROM": "sender@example.com",
}


class SmtpConfigTests(unittest.TestCase):
    """_smtp_config() reads os.environ at CALL time (not import time), so a
    caller that sets/clears these env vars right before sending sees the
    change take effect immediately."""

    def setUp(self) -> None:
        for key in _SMTP_ENV:
            os.environ.pop(key, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in _SMTP_ENV])

    def test_unconfigured_returns_none(self) -> None:
        self.assertIsNone(notify._smtp_config())

    def test_missing_password_returns_none(self) -> None:
        os.environ["SMTP_USER"] = "sender@example.com"
        self.assertIsNone(notify._smtp_config())

    def test_configured_returns_tuple(self) -> None:
        with patch.dict(os.environ, _SMTP_ENV):
            self.assertEqual(notify._smtp_config(),
                              ("smtp.gmail.com", 587, "sender@example.com",
                               "app-password", "sender@example.com"))

    def test_host_and_port_default_to_gmail(self) -> None:
        with patch.dict(os.environ, {"SMTP_USER": "sender@example.com",
                                      "SMTP_PASSWORD": "app-password"}):
            host, port, _, _, _ = notify._smtp_config()
            self.assertEqual((host, port), ("smtp.gmail.com", 587))

    def test_from_defaults_to_user(self) -> None:
        with patch.dict(os.environ, {"SMTP_USER": "sender@example.com",
                                      "SMTP_PASSWORD": "app-password"}):
            *_, from_addr = notify._smtp_config()
            self.assertEqual(from_addr, "sender@example.com")


class SendEmailFunctionTests(unittest.TestCase):
    """Tests for send_email() -- the one SMTP-sending path in this module,
    used both directly (a ready-made HTML document sent verbatim) and by
    send(dest=...)'s email target (an HTML body auto-converted from
    chat-markdown text)."""

    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, _SMTP_ENV)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_missing_credentials_returns_false_without_raising(self) -> None:
        with patch.dict(os.environ, {"SMTP_PASSWORD": ""}):
            self.assertFalse(notify.send_email("Subj", "<p>hi</p>", ["a@example.com"]))

    def test_no_recipients_returns_false_without_raising(self) -> None:
        self.assertFalse(notify.send_email("Subj", "<p>hi</p>", []))

    @patch("warehouse.notify.smtplib.SMTP")
    def test_sends_html_and_plaintext_alternative(self, smtp_cls: Mock) -> None:
        server = Mock()
        smtp_cls.return_value.__enter__.return_value = server
        ok = notify.send_email("Report subject", "<p>rich html</p>", ["a@example.com"],
                                plaintext_body="plain fallback")
        self.assertTrue(ok)
        smtp_cls.assert_called_once_with("smtp.gmail.com", 587,
                                          timeout=notify.SMTP_TIMEOUT_SECONDS)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("sender@example.com", "app-password")
        args, _ = server.sendmail.call_args
        from_addr, recipients, raw_message = args
        self.assertEqual(from_addr, "sender@example.com")
        self.assertEqual(recipients, ["a@example.com"])
        parsed = message_from_string(raw_message)
        self.assertEqual(parsed["Subject"], "Report subject")
        by_type = {part.get_content_type(): part for part in parsed.get_payload()}
        self.assertIn("plain fallback", by_type["text/plain"].get_payload(decode=True).decode())
        self.assertIn("rich html", by_type["text/html"].get_payload(decode=True).decode())

    @patch("warehouse.notify.smtplib.SMTP")
    def test_cc_recipients_get_the_message_and_the_header(self, smtp_cls: Mock) -> None:
        server = Mock()
        smtp_cls.return_value.__enter__.return_value = server
        notify.send_email("Subj", "<p>hi</p>", ["a@example.com"], cc=["b@example.com"])
        _, recipients, raw_message = server.sendmail.call_args.args
        self.assertEqual(recipients, ["a@example.com", "b@example.com"])
        self.assertEqual(message_from_string(raw_message)["Cc"], "b@example.com")

    @patch("warehouse.notify.time.sleep")
    @patch("warehouse.notify.smtplib.SMTP")
    def test_retries_on_transient_failure_then_succeeds(self, smtp_cls: Mock,
                                                          sleep: Mock) -> None:
        server = Mock()
        smtp_cls.return_value.__enter__.side_effect = [
            ConnectionError("421 Temporary System Problem"),
            server,
        ]
        ok = notify.send_email("Subj", "<p>hi</p>", ["a@example.com"])
        self.assertTrue(ok)
        self.assertEqual(smtp_cls.call_count, 2)
        sleep.assert_called_once_with(notify.SMTP_RETRY_BACKOFF_SECONDS)

    @patch("warehouse.notify.time.sleep")
    @patch("warehouse.notify.smtplib.SMTP")
    def test_exhausting_retries_returns_false_without_raising(self, smtp_cls: Mock,
                                                                sleep: Mock) -> None:
        smtp_cls.return_value.__enter__.side_effect = ConnectionError("boom")
        ok = notify.send_email("Subj", "<p>hi</p>", ["a@example.com"])
        self.assertFalse(ok)
        self.assertEqual(smtp_cls.call_count, notify.SMTP_SEND_RETRIES)


class SendTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in list(os.environ):
            if key.endswith(("_SLACK_WEBHOOK", "_GCHAT_WEBHOOK", "_EMAIL_TO")):
                os.environ.pop(key)

    def test_no_destination_configured_does_not_raise(self) -> None:
        notify.send("hello", "unconfigured_dest")  # must not raise

    @patch("warehouse.notify.send_email")
    def test_email_destination_calls_send_email(self, send_email: Mock) -> None:
        os.environ["WEEKLY_DIGEST_EMAIL_TO"] = "a@example.com"
        send_email.return_value = True
        notify.send("*Weekly Digest*\nline two", "weekly_digest")
        send_email.assert_called_once_with(
            "Weekly Digest", notify.to_email_html("*Weekly Digest*\nline two"),
            ["a@example.com"], plaintext_body="*Weekly Digest*\nline two")

    @patch("warehouse.notify.send_email")
    def test_email_failure_does_not_raise_or_block_other_platforms(self, send_email: Mock) -> None:
        os.environ["WEEKLY_DIGEST_EMAIL_TO"] = "a@example.com"
        send_email.return_value = False  # send_email() reports failure via return, not raise
        notify.send("hello", "weekly_digest")  # must not raise

    def test_blank_email_to_is_treated_as_unconfigured(self) -> None:
        # A whitespace/comma-only value (no real address) must be skipped
        # the same as an unset env var, not attempted with zero recipients.
        os.environ["WEEKLY_DIGEST_EMAIL_TO"] = " , ,"
        with patch("warehouse.notify.send_email") as send_email:
            notify.send("hello", "weekly_digest")
        send_email.assert_not_called()

    @patch("warehouse.notify.requests.post")
    def test_posts_json_text_to_each_configured_webhook(self, post: Mock) -> None:
        os.environ["WEEKLY_DIGEST_SLACK_WEBHOOK"] = "https://hooks.slack.test/x"
        os.environ["WEEKLY_DIGEST_GCHAT_WEBHOOK"] = "https://chat.googleapis.test/x"
        post.return_value = Mock(raise_for_status=Mock())
        notify.send("hello", "weekly_digest")
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["json"], {"text": "hello"})
            self.assertEqual(call.kwargs["timeout"], notify.TIMEOUT_SECONDS)

    @patch("warehouse.notify.requests.post")
    def test_one_platform_failing_does_not_raise_or_block_the_other(self, post: Mock) -> None:
        os.environ["WEEKLY_DIGEST_SLACK_WEBHOOK"] = "https://hooks.slack.test/x"
        os.environ["WEEKLY_DIGEST_GCHAT_WEBHOOK"] = "https://chat.googleapis.test/x"

        def side_effect(url, json, timeout):  # noqa: ANN001
            if "slack" in url:
                raise ConnectionError("boom")
            return Mock(raise_for_status=Mock())

        post.side_effect = side_effect
        notify.send("hello", "weekly_digest")  # must not raise
        self.assertEqual(post.call_count, 2)

    @patch("warehouse.notify.requests.post")
    def test_long_text_is_truncated(self, post: Mock) -> None:
        os.environ["WEEKLY_DIGEST_SLACK_WEBHOOK"] = "https://hooks.slack.test/x"
        post.return_value = Mock(raise_for_status=Mock())
        notify.send("x" * 5000, "weekly_digest")
        sent = post.call_args.kwargs["json"]["text"]
        self.assertLessEqual(len(sent), notify.MAX_CHARS + len("\n…(truncated)"))
        self.assertTrue(sent.endswith("(truncated)"))


if __name__ == "__main__":
    unittest.main()
