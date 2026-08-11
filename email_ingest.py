"""
Parsing for emails forwarded into JobPilot.

Users forward recruiting mail to a private address instead of granting access to
their whole mailbox, so nothing here talks to Google. The Cloudflare Worker hands
over the raw RFC822 message and this module turns it into the same
{subject, sender, date, body} shape that ai_services.parse_job_email already takes,
so the classifier is unchanged regardless of where the mail came from.

Handles both routes in one path:
  * a Gmail filter auto-forwarding new mail, one message at a time
  * "Forward as attachment" with many messages selected, for backfilling history
"""

import re
import email
from email import policy

MAX_BODY_CHARS = 4000


def _decode_header(value) -> str:
    return str(value).strip() if value else ""


def _part_text(part) -> str:
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if payload else ""
    if (part.get_content_type() or "").lower() == "text/html":
        content = _html_to_text(content)
    content = re.sub(r"[ \t]+", " ", content or "")
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", content).strip()


def _body_text(msg) -> str:
    """
    Readable text for a message, preferring text/plain but falling back to HTML.

    Plenty of senders attach a token text/plain part ("Please enable HTML to view
    this message") next to the real HTML. Taking text/plain unconditionally would
    hand the classifier that stub instead of the email, so a too-short plain part
    is treated as absent.
    """
    try:
        text = _part_text(msg.get_body(preferencelist=("plain",)))
    except Exception:
        text = ""

    if len(text) >= 40:
        return text

    try:
        html_text = _part_text(msg.get_body(preferencelist=("html",)))
    except Exception:
        html_text = ""

    return html_text if len(html_text) > len(text) else text


def html_to_text(html: str) -> str:
    """Flattens an HTML email body to readable text. Shared with gmail_services."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


# Kept as an alias so the private call sites in this module stay readable.
_html_to_text = html_to_text


def _attached_messages(msg) -> list:
    """
    Messages carried as attachments.

    Gmail's "Forward as attachment" works on a whole selection at once, which is the
    only practical way to backfill history without asking for mailbox access. Each
    selected email arrives as a message/rfc822 part.
    """
    found = []
    for part in msg.walk():
        if (part.get_content_type() or "").lower() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                found.extend(payload)
            elif payload is not None:
                found.append(payload)
    return found


# An inline "Forward" rewrites From to the person forwarding, burying the real
# sender in the quoted header block. Auto-forwarding by filter keeps the original
# From, so this only has to rescue the manual case.
_FORWARD_FROM = re.compile(r"^\s*(?:>\s*)?From:\s*(.+)$", re.I | re.M)
_FORWARD_SUBJECT = re.compile(r"^\s*(?:>\s*)?Subject:\s*(.+)$", re.I | re.M)


def _unwrap_inline_forward(subject: str, sender: str, body: str):
    """Recovers the original sender and subject from a manually forwarded email."""
    looks_forwarded = bool(re.match(r"^\s*(fwd|fw)\s*:", subject or "", re.I))
    if not looks_forwarded:
        return subject, sender

    original_sender = sender
    m = _FORWARD_FROM.search(body or "")
    if m:
        candidate = m.group(1).strip()
        if "@" in candidate:
            original_sender = candidate

    original_subject = re.sub(r"^\s*(fwd|fw)\s*:\s*", "", subject or "", flags=re.I).strip()
    m = _FORWARD_SUBJECT.search(body or "")
    if m and m.group(1).strip():
        original_subject = m.group(1).strip()

    return original_subject or subject, original_sender


def _to_record(msg) -> dict:
    subject = _decode_header(msg.get("Subject")) or "No Subject"
    sender = _decode_header(msg.get("From")) or "Unknown Sender"
    date = _decode_header(msg.get("Date")) or "Unknown Date"
    body = _body_text(msg)

    subject, sender = _unwrap_inline_forward(subject, sender, body)
    match = re.search(r"[\w\.\+-]+@[\w\.-]+\.\w+", sender)

    return {
        "subject": subject,
        "sender": sender,
        "sender_email": match.group(0).lower() if match else "",
        "date": date,
        "body": body[:MAX_BODY_CHARS],
        "message_id": _decode_header(msg.get("Message-ID")),
    }


def parse_raw_email(raw: bytes) -> list:
    """
    Turns one forwarded message into the records to classify.

    Returns a list because a single "forward as attachment" can carry dozens of
    emails. When attachments are present they are the real content and the wrapper
    is dropped, since the wrapper is just the user's own "here you go" message.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")

    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception as e:
        print(f"Could not parse forwarded email: {e}")
        return []

    attached = _attached_messages(msg)
    if attached:
        records = []
        for part in attached:
            try:
                records.append(_to_record(part))
            except Exception as e:
                print(f"Skipping unreadable attached message: {e}")
        records = [r for r in records if _is_meaningful(r)]
        if records:
            return records

    try:
        record = _to_record(msg)
    except Exception as e:
        print(f"Could not read forwarded email: {e}")
        return []

    return [record] if _is_meaningful(record) else []


def _is_meaningful(record: dict) -> bool:
    """Drops empty shells so classification isn't paid for on nothing."""
    return bool((record.get("body") or "").strip()) or record.get("subject") != "No Subject"


# Gmail will not forward anywhere until the destination address proves it wants the
# mail, and it proves that by receiving a code. Since the bot owns that mailbox, the
# code lands here rather than anywhere the user can read — so it has to be relayed,
# or setup is impossible to complete.
_CONFIRM_SENDER = "forwarding-noreply@google.com"
_CONFIRM_CODE = re.compile(r"\(#(\d{6,})\)")
_CONFIRM_CODE_BODY = re.compile(r"confirmation code[:\s]+(\d{6,})", re.I)
# Google sends this from mail-settings.google.com, not mail.google.com, and the URL
# carries percent-encoded brackets — so match the host loosely and keep the path intact.
_CONFIRM_LINK = re.compile(r"https://[\w.-]*google\.com/\S*vf-\S+|https://[\w.-]*google\.com/mail/\S+")


def detect_forwarding_confirmation(record: dict):
    """
    Recognises Gmail's forwarding-confirmation email.

    Returns {'code', 'link', 'excerpt'} when this is one, else None.

    Recognition is deliberately loose and never gives up once the sender looks like
    Google's forwarding robot: if the code can't be parsed, the message is still
    relayed with an excerpt so the user can read the code themselves. Silently
    dropping this email makes forwarding impossible to enable, which is a far worse
    failure than relaying something slightly untidy.
    """
    sender = (record.get("sender_email") or "").lower()
    from_header = (record.get("sender") or "").lower()
    subject = record.get("subject") or ""
    body = record.get("body") or ""
    lowered = subject.lower()

    is_confirmation = (
        "forwarding-noreply" in sender
        or "forwarding-noreply" in from_header
        or "forwarding confirmation" in lowered
        or ("forward" in lowered and ("confirm" in lowered or "verify" in lowered))
        or "gmail forwarding" in lowered
        or "has requested to automatically forward mail" in body.lower()
    )
    if not is_confirmation:
        return None

    match = _CONFIRM_CODE.search(subject) or _CONFIRM_CODE_BODY.search(body)
    code = match.group(1) if match else None

    link_match = _CONFIRM_LINK.search(body)
    # Trailing punctuation from the surrounding sentence isn't part of the URL.
    link = link_match.group(0).rstrip(").,;>'\"") if link_match else None

    return {"code": code, "link": link, "excerpt": body[:700], "subject": subject}


def address_token(to_address: str) -> str:
    """
    Extracts the per-user token from the address the mail was sent to.

    Supports both delivery styles:
      own domain  ->  u7f3a9k2m@inbox.example.com
      shared Gmail ->  jobpilot.inbox+u7f3a9k2m@gmail.com

    With a shared mailbox the token lives in the plus-tag, so that part is the
    token rather than something to strip.
    """
    if not to_address:
        return ""
    match = re.search(r"[\w\.\+-]+@[\w\.-]+", to_address)
    local = (match.group(0).split("@")[0] if match else to_address).strip().lower()

    if "+" in local:
        local = local.split("+", 1)[1]

    return local[1:] if local.startswith("u") else local
