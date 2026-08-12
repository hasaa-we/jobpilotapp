import os
import re
import json
import time
import hmac
import base64
import hashlib
import requests
import urllib.parse
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

# We need a secure key to encrypt/decrypt tokens in Supabase
FERNET_KEY = os.getenv("FERNET_KEY")
if FERNET_KEY:
    cipher_suite = Fernet(FERNET_KEY.encode())
else:
    cipher_suite = None

WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Kept as a fallback for local development only.
CLIENT_SECRETS_FILE = "client_secret.json"


def _state_key() -> bytes:
    """Signing key for OAuth state. FERNET_KEY already exists and is server-side only."""
    return (os.getenv("FERNET_KEY") or os.getenv("TELEGRAM_BOT_TOKEN") or "jobpilot").encode()


def make_oauth_state(user_id: str, ttl_seconds: int = 900) -> str:
    """
    Signs the user id into a short-lived OAuth `state`.

    Passing the bare user id let anyone who reached the callback URL attach a mailbox
    to an arbitrary account. Signing proves the flow was started by this server, and
    the expiry keeps a leaked link from being replayed later.
    """
    expires = int(time.time()) + ttl_seconds
    payload = f"{user_id}:{expires}"
    signature = hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{signature}"


def verify_oauth_state(state: str):
    """Returns the user id if the state is authentic and unexpired, else None."""
    try:
        user_id, expires, signature = (state or "").rsplit(":", 2)
    except ValueError:
        return None

    expected = hmac.new(
        _state_key(), f"{user_id}:{expires}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return None
    if int(expires) < int(time.time()):
        return None
    return user_id


def get_client_credentials():
    """
    OAuth client id and secret, from the environment first.

    Reading these from a file meant deploying a secret file to every host, and the
    file is absent here entirely — which is why connecting Gmail currently fails
    outright. Environment variables work the same locally and in production, and
    keep the credentials out of the repo.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    try:
        with open(CLIENT_SECRETS_FILE, 'r') as f:
            web = json.load(f).get('web', {})
            return web.get('client_id'), web.get('client_secret')
    except FileNotFoundError:
        print("Google OAuth not configured: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
    except Exception as e:
        print(f"Could not read {CLIENT_SECRETS_FILE}: {e}")
    return None, None

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/userinfo.email'
]

def encrypt_token(token_data: dict) -> str:
    """Encrypts the OAuth token dictionary into a string."""
    if not cipher_suite: return json.dumps(token_data)
    json_data = json.dumps(token_data).encode('utf-8')
    encrypted_data = cipher_suite.encrypt(json_data)
    return encrypted_data.decode('utf-8')

def decrypt_token(encrypted_string: str) -> dict:
    """Decrypts the string back into the OAuth token dictionary."""
    if not encrypted_string: return None
    if not cipher_suite: return json.loads(encrypted_string)
    decrypted_data = cipher_suite.decrypt(encrypted_string.encode('utf-8'))
    return json.loads(decrypted_data.decode('utf-8'))

def get_auth_url(user_id: str) -> str:
    """Generates the OAuth consent screen URL."""
    try:
        client_id, _ = get_client_credentials()
        if not client_id:
            return None

        redirect_uri = f"{WEBHOOK_URL}/auth/gmail/callback"
        
        # Build the URL manually to avoid PKCE state issues
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": make_oauth_state(user_id)
        }
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
        return url
    except Exception as e:
        print(f"Error generating auth URL: {e}")
        return None

def exchange_code_for_token(code: str) -> dict:
    """Exchanges the Google authorization code for access/refresh tokens."""
    try:
        client_id, client_secret = get_client_credentials()
        if not (client_id and client_secret):
            return None

        redirect_uri = f"{WEBHOOK_URL}/auth/gmail/callback"
        
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri
        }
        
        response = requests.post("https://oauth2.googleapis.com/token", data=data)
        response.raise_for_status()
        
        token_info = response.json()
        access_token = token_info.get('access_token')
        
        # Fetch the user's email address
        user_info_resp = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_info = user_info_resp.json()
        email_address = user_info.get("email")
        
        token_data = {
            'token': access_token,
            'refresh_token': token_info.get('refresh_token'),
            'token_uri': "https://oauth2.googleapis.com/token",
            'client_id': client_id,
            'client_secret': client_secret,
            'scopes': SCOPES,
            'email_address': email_address
        }
        return token_data
    except Exception as e:
        print(f"Error exchanging code: {e}")
        return None

# Applicant tracking systems that send nearly all recruiting mail. Matching on the
# sender catches "Update on your candidacy" and other wordings no keyword list would,
# which is where the old subject-only query lost most of its recall.
ATS_DOMAINS = [
    "greenhouse.io", "lever.co", "myworkday.com", "workday.com", "smartrecruiters.com",
    "taleo.net", "icims.com", "ashbyhq.com", "jobvite.com", "bamboohr.com",
    "recruitee.com", "workable.com", "breezy.hr", "teamtailor.com", "successfactors.com",
    "oraclecloud.com", "avature.net", "eightfold.ai", "hire.google.com", "paylocity.com",
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "wellfound.com",
    "angel.co", "hired.com", "dice.com", "monster.com", "bayt.com", "naukri.com",
]

SUBJECT_TERMS = [
    "application", "applied", "interview", "offer", "rejected", "rejection",
    "shortlisted", "assessment", "hired", "position", "opportunity", "regret",
    "unfortunately", "recruitment", "recruiter", "candidate", "candidacy", "vacancy",
    "selection", "congratulations", "hiring", "role", "screening", "onsite",
    "your resume", "your cv", "talent", "career", "job",
]

BODY_PHRASES = [
    "your application", "you applied", "thank you for applying",
    "thank you for your interest", "application received", "application submitted",
    "we received your application", "next steps", "hiring process", "your interview",
    "job offer", "not move forward", "not been selected", "no longer under consideration",
    "move forward with other candidates", "decided to proceed", "your candidacy",
    "schedule a call", "schedule an interview", "technical assessment", "coding challenge",
    "we regret to inform", "pleased to offer", "background check", "reference check",
]


def build_job_queries(days_back: int = 7) -> list:
    """
    Builds the Gmail searches for recruiting mail, as several separate queries
    whose results get unioned.

    Deliberately not one giant OR: Gmail caps query length near 2048 characters and
    a single combined query already runs past 1800, so adding one more phrase would
    silently start returning nothing at all. Split, each query stays short, and the
    term lists can grow without a ceiling.
    """
    groups = [
        " OR ".join(f'subject:"{t}"' if " " in t else f"subject:{t}" for t in SUBJECT_TERMS),
        " OR ".join(f'"{p}"' for p in BODY_PHRASES),
        " OR ".join(f"from:{d}" for d in ATS_DOMAINS),
    ]
    suffix = f" newer_than:{days_back}d" if days_back and days_back > 0 else ""
    return [f"({g}){suffix}" for g in groups if g]


def _header(headers: list, name: str, default: str = "") -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", default)
    return default


def _walk_parts(payload: dict):
    """Yields every MIME part, at any nesting depth."""
    if not payload:
        return
    yield payload
    for part in payload.get("parts") or []:
        yield from _walk_parts(part)


def _decode(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _html_to_text(html: str) -> str:
    """Flattens an HTML email body to readable text."""
    from email_ingest import html_to_text   # single implementation, shared
    text = re.sub(r"[ \t]+", " ", html_to_text(html))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def extract_email_body(payload: dict, max_chars: int = 4000) -> str:
    """
    Pulls the readable body out of a Gmail payload.

    Walks the MIME tree at every depth and falls back to HTML. The old version only
    looked one level deep for text/plain, so any multipart/alternative nested inside
    multipart/mixed — which is what most ATS mail looks like — fell through to the
    ~200-character snippet, and HTML-only mail always did.
    """
    plain, html = [], []
    for part in _walk_parts(payload):
        mime = (part.get("mimeType") or "").lower()
        # Skip attachments; they are never the message body.
        if part.get("filename"):
            continue
        if mime == "text/plain":
            plain.append(_decode(part))
        elif mime == "text/html":
            html.append(_decode(part))

    text = "\n".join(t for t in plain if t).strip()
    if len(text) < 40:
        text = _html_to_text("\n".join(h for h in html if h)).strip()

    return text[:max_chars]


def _fetch_messages(service, ids: list) -> list:
    """
    Fetches full messages, batching to keep large syncs fast.

    Gmail allows batching these lookups, which turns hundreds of sequential
    round-trips into a handful. Individual failures are collected rather than
    raised so one unreadable message cannot abort an entire sync.
    """
    fetched = []

    def handle(request_id, response, exception):
        if exception is None and response:
            fetched.append(response)
        elif exception is not None:
            print(f"Gmail message fetch failed ({request_id}): {exception}")

    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        try:
            batch = service.new_batch_http_request(callback=handle)
            for mid in chunk:
                batch.add(service.users().messages().get(userId="me", id=mid, format="full"))
            batch.execute()
        except Exception as e:
            print(f"Gmail batch failed ({e}); falling back to sequential")
            for mid in chunk:
                try:
                    fetched.append(
                        service.users().messages().get(userId="me", id=mid, format="full").execute()
                    )
                except Exception as inner:
                    print(f"Gmail message {mid} failed: {inner}")
    return fetched


def fetch_recent_job_emails(token_dict: dict, max_results: int = None, days_back: int = 7,
                            include_spam_trash: bool = False) -> list:
    """
    Fetches job-related emails from Gmail.

    days_back: how far back to search (1=24h, 7=week, 30=month, 0=all time)
    max_results: cap on messages processed (None for unlimited)
    """
    creds = Credentials.from_authorized_user_info(token_dict, SCOPES)
    service = build('gmail', 'v1', credentials=creds)

    message_ids = []
    seen = set()

    for query in build_job_queries(days_back):
        page_token = None
        while True:
            if max_results and len(message_ids) >= max_results:
                break
            try:
                results = service.users().messages().list(
                    userId='me', q=query, maxResults=500,
                    pageToken=page_token, includeSpamTrash=include_spam_trash,
                ).execute()
            except Exception as e:
                # One failing query must not sink the whole sync; the other
                # queries still contribute their matches.
                print(f"Gmail search failed for a query group: {e}")
                break

            for m in results.get('messages', []):
                if m['id'] not in seen:
                    seen.add(m['id'])
                    message_ids.append(m['id'])

            page_token = results.get('nextPageToken')
            if not page_token:
                break

    if max_results:
        message_ids = message_ids[:max_results]
    if not message_ids:
        return []

    emails = []
    seen_ids = set()
    for msg in _fetch_messages(service, message_ids):
        if msg.get('id') in seen_ids:
            continue
        seen_ids.add(msg.get('id'))

        payload = msg.get('payload') or {}
        headers = payload.get('headers') or []
        sender = _header(headers, 'From', 'Unknown Sender')
        match = re.search(r'[\w\.\+-]+@[\w\.-]+\.\w+', sender)

        body = extract_email_body(payload)
        emails.append({
            'message_id': msg.get('id'),
            'thread_id': msg.get('threadId'),
            'subject': _header(headers, 'Subject', 'No Subject'),
            'sender': sender,
            'sender_email': match.group(0).lower() if match else '',
            'date': _header(headers, 'Date', 'Unknown Date'),
            'labels': msg.get('labelIds', []),
            # The snippet is kept as a floor: if a message is body-less or unparseable,
            # the classifier still gets Gmail's own preview text rather than nothing.
            'body': body or msg.get('snippet', ''),
        })

    return emails
