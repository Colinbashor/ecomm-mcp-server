r"""
Push notifications to Slack, Google Chat, and/or email.

Slack and Google Chat both accept a plain POST of {"text": "..."} to a
webhook URL and both render the same minimal markdown subset (single-
asterisk *bold*, no real headers) — so one formatter and one send path cover
both, no per-platform branching needed. Email is sent as plain text over SMTP
(the *bold*/bullet characters read fine as plain text) plus an HTML
alternative — see EMAIL below.

Webhook URLs / email recipients are secrets and belong in .env, keyed per
named DESTINATION (not per platform globally) so different reports can go to
different channels/spaces/inboxes without any code change: <DEST>_SLACK_WEBHOOK,
<DEST>_GCHAT_WEBHOOK, <DEST>_EMAIL_TO (comma-separated addresses), with
`dest` upper-cased (e.g. dest="weekly_digest" -> WEEKLY_DIGEST_SLACK_WEBHOOK /
WEEKLY_DIGEST_GCHAT_WEBHOOK / WEEKLY_DIGEST_EMAIL_TO). A destination with
none, or only some, of these set is NOT an error — notifications are meant to
be additive to whatever pipeline calls send(), never a hard requirement, so a
missing or unreachable target must never fail the caller.

SETUP:
  - Slack: create an "Incoming Webhook" for a channel (Slack App settings ->
    Incoming Webhooks) and put the URL in `<DEST>_SLACK_WEBHOOK`.
  - Google Chat: add a "Webhook" to a Space (Space settings -> Apps &
    integrations -> Webhooks) and put the URL in `<DEST>_GCHAT_WEBHOOK`.
  - Email: set SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/SMTP_FROM in .env
    (these are GLOBAL — one sending mailbox for the whole project, shared
    across every destination) and `<DEST>_EMAIL_TO` per destination. Defaults
    assume Gmail's SMTP relay (smtp.gmail.com:587, STARTTLS); if the account
    has 2-Step Verification on, SMTP_PASSWORD needs to be a Google APP
    PASSWORD (myaccount.google.com/apppasswords), not the account's normal
    login password — Gmail rejects the latter over SMTP with a
    "5.7.9 Application-specific password required" error. Any other SMTP
    provider works too; just point SMTP_HOST/SMTP_PORT at it. No
    SMTP_PASSWORD configured = email is skipped like any other unconfigured
    destination, not an error.

EMAIL IS SENT AS multipart/alternative: `to_email_html()` converts the same
chat-markdown text used for Slack/Chat into real HTML (bold, bulleted `<ul>`
lists, a clickable link for a "Label: https://..." line), and every email
carries both the plain-text original (for text-only clients) and the HTML
version — one message body to build per report, two renderings.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT_SECONDS = 10
# Google Chat's plain-text message cap is ~4096 chars; Slack's is much higher.
# Truncate to the tighter limit (with margin) so one send() path fits both.
# Email has no such constraint and is sent untruncated.
MAX_CHARS = 3800

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER


def to_chat_markdown(md_text: str) -> str:
    """Convert '#'/'##' headers + '**bold**' into the single-asterisk-bold,
    no-headers subset Slack and Google Chat both render. Headers become a
    bold line since neither platform gives '#' any special meaning."""
    lines = []
    for line in md_text.splitlines():
        if line.startswith("#"):
            lines.append(f"*{line.lstrip('#').strip()}*")
        else:
            lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def _targets(dest: str) -> dict[str, str]:
    """Every configured target for `dest`: webhook URLs for slack/google_chat,
    a comma-separated recipient list for email. Whichever of these env vars
    are unset are simply absent from the result — see module docstring."""
    prefix = dest.upper()
    candidates = {
        "slack": os.environ.get(f"{prefix}_SLACK_WEBHOOK", ""),
        "google_chat": os.environ.get(f"{prefix}_GCHAT_WEBHOOK", ""),
        "email": os.environ.get(f"{prefix}_EMAIL_TO", ""),
    }
    return {platform: value for platform, value in candidates.items() if value}


def _email_subject(text: str) -> str:
    """First line, stripped of the '*bold*'/'-'/'•' chat markup, as the
    subject — a digest's own header line doubles as a sensible subject
    without a separate subject argument on every send() call."""
    first_line = text.splitlines()[0] if text else ""
    return first_line.strip("*-• ").strip() or "Warehouse notification"


_URL_RE = re.compile(r"(https?://\S+)")
_BOLD_RE = re.compile(r"\*(.+?)\*")
_BULLET_RE = re.compile(r"^[•\-]\s+(.*)$")
# "Label: https://..." -> a clean "Label" hyperlink rather than repeating the
# raw URL as the visible text — a report's link line commonly looks like
# "Full report: https://...".
_LABELED_LINK_RE = re.compile(r"^(.+?):\s*(https?://\S+)\s*$")


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_html(s: str) -> str:
    """One line's worth of chat markdown -> HTML: escape first (so a stray
    '<'/'>' can't break the markup), THEN apply *bold* and linkify — in that
    order, so the tags this function adds are never themselves escaped. A
    whole line that's just "Label: url" becomes one clean hyperlink; any
    other bare URL is linkified in place (visible text = the URL itself,
    since there's no better label to use)."""
    labeled = _LABELED_LINK_RE.match(s)
    if labeled:
        label, url = labeled.groups()
        return f'<a href="{_escape_html(url)}">{_escape_html(label)}</a>'
    s = _escape_html(s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _URL_RE.sub(r'<a href="\1">\1</a>', s)
    return s


def to_email_html(text: str) -> str:
    """Convert the same *bold*/•-bullet chat-markdown text used for Slack/
    Chat into simple HTML for the email body: *bold* -> <b>, consecutive
    bullet lines -> one <ul>, a blank line -> a paragraph break, and any bare
    URL (e.g. a "Label: https://..." link line) -> a clickable link.
    Deliberately minimal — this is for internal reporting, not a marketing
    template."""
    parts: list[str] = []
    in_list = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append("<div>&nbsp;</div>")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            if not in_list:
                parts.append('<ul style="margin:2px 0 8px 0;padding-left:20px">')
                in_list = True
            parts.append(f"<li>{_inline_html(bullet.group(1))}</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        parts.append(f"<div>{_inline_html(line)}</div>")
    if in_list:
        parts.append("</ul>")
    body = "\n".join(parts)
    return (
        '<html><body style="font-family:Arial,Helvetica,sans-serif;'
        'font-size:14px;line-height:1.5;color:#222">'
        f"{body}</body></html>"
    )


def _send_email(to_csv: str, subject: str, body: str) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER/SMTP_PASSWORD not configured in .env")
    recipients = [addr.strip() for addr in to_csv.split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError(f"no valid recipients in {to_csv!r}")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(recipients)
    # Plain text first, HTML second — email clients render the LAST alternative
    # part they understand, and HTML is the one we want when supported.
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(to_email_html(body), "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, recipients, msg.as_string())


def send(text: str, dest: str) -> None:
    """Push `text` to every target configured for `dest` (Slack/Chat webhook
    POST, or an email send). Best-effort per platform: a failing or
    unconfigured destination is logged and skipped, never raised — the
    caller (typically a report-rendering step) must keep running regardless
    of notification state."""
    targets = _targets(dest)
    if not targets:
        print(f"[notify] no target configured for dest={dest!r} — skipped")
        return
    body = text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n…(truncated)"
    for platform, value in targets.items():
        try:
            if platform == "email":
                _send_email(value, _email_subject(text), text)
            else:
                r = requests.post(value, json={"text": body}, timeout=TIMEOUT_SECONDS)
                r.raise_for_status()
            print(f"[notify] posted to {platform} (dest={dest})")
        except Exception as exc:  # noqa: BLE001 - best-effort, never propagate
            print(f"[notify] {platform} (dest={dest}) failed: {exc}")
