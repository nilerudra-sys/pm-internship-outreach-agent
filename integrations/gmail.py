"""Gmail API with OAuth (installed-app flow) and the send-only scope.

The app can only SEND mail as you; it cannot read your inbox. Authorise once from the dashboard
(Settings) or with:  python -m integrations.gmail authorize
That opens a Google sign-in page in your browser and saves the token to GMAIL_TOKEN_FILE.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# Optional (reply tracking): message headers only (From, Subject, dates); never message bodies.
METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
SCOPES = [SEND_SCOPE]


class GmailNotConnected(RuntimeError):
    """No usable OAuth token: authorise Gmail first."""


class GmailSendError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def authorize(credentials_file: Path, token_file: Path, reply_tracking: bool = False) -> None:
    """Interactive: opens the Google consent screen in your browser and stores the token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not Path(credentials_file).exists():
        raise GmailNotConnected(f"OAuth client file not found: {credentials_file}. Download it from Google Cloud "
                                "Console (Credentials → OAuth client ID → Desktop app).")
    scopes = SCOPES + ([METADATA_SCOPE] if reply_tracking else [])
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
    creds = flow.run_local_server(port=0, prompt="consent")
    Path(token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(token_file).write_text(creds.to_json(), encoding="utf-8")


def load_credentials(token_file: Path):
    """Stored credentials, refreshed if expired. Raises GmailNotConnected if missing or revoked."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = Path(token_file)
    if not path.exists():
        raise GmailNotConnected("Gmail is not connected yet. Use Settings → Connect Gmail.")
    try:
        creds = Credentials.from_authorized_user_file(str(path))   # keeps whatever scopes were granted
    except (ValueError, KeyError) as exc:
        raise GmailNotConnected(f"Gmail token file is unreadable ({exc}); connect Gmail again.") from None
    if SEND_SCOPE not in (creds.scopes or []):
        raise GmailNotConnected("Gmail token lacks the send permission; connect Gmail again.")
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise GmailNotConnected(f"Gmail access was revoked or expired ({exc}); connect Gmail again.") from None
            path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise GmailNotConnected("Gmail token is invalid; connect Gmail again.")
    return creds


def build_message(to: str, subject: str, body: str, sender_email: str | None = None,
                  sender_name: str | None = None, in_reply_to: str | None = None) -> EmailMessage:
    """Plain-text UTF-8 email. From is omitted when unknown (Gmail fills in your address).
    in_reply_to threads a follow-up under the original message in the recipient's mail client."""
    msg = EmailMessage()
    msg["To"] = to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if sender_email:
        msg["From"] = formataddr((sender_name or "", sender_email)) if sender_name else sender_email
        msg["Message-ID"] = make_msgid(domain=sender_email.split("@", 1)[1])
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def encode_message(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailClient:
    def __init__(self, token_file: Path | None = None, service=None, reply_tracking: bool = False):
        if service is None:
            from googleapiclient.discovery import build

            creds = load_credentials(token_file)
            reply_tracking = METADATA_SCOPE in (creds.scopes or [])
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._service = service
        self.reply_tracking = reply_tracking

    def thread_reply_state(self, thread_id: str, my_email: str) -> str | None:
        """'bounce', 'reply' or None, from message headers only (needs the gmail.metadata permission)."""
        if not self.reply_tracking:
            raise GmailNotConnected("Reply tracking is not enabled (Settings → Enable reply tracking).")
        thread = self._service.users().threads().get(
            userId="me", id=thread_id, format="metadata", metadataHeaders=["From"]).execute()
        state = None
        for m in thread.get("messages", [])[1:]:
            sender = next((h["value"] for h in m.get("payload", {}).get("headers", [])
                           if h.get("name", "").lower() == "from"), "").lower()
            if "mailer-daemon" in sender or "postmaster" in sender:
                return "bounce"
            if my_email.lower() not in sender:
                state = "reply"
        return state

    def send(self, msg: EmailMessage, thread_id: str | None = None) -> dict:
        """Send now (in an existing thread if thread_id is given). Returns {"id": ..., "threadId": ...}."""
        try:
            from googleapiclient.errors import HttpError
        except ImportError:  # pragma: no cover
            HttpError = Exception  # noqa: N806
        body = {"raw": encode_message(msg)}
        if thread_id:
            body["threadId"] = thread_id
        try:
            result = self._service.users().messages().send(userId="me", body=body).execute()
        except HttpError as exc:  # type: ignore[misc]
            status = getattr(getattr(exc, "resp", None), "status", None)
            raise GmailSendError(f"Gmail rejected the message (HTTP {status}): {str(exc)[:200]}", status) from None
        if not result or not result.get("id"):
            raise GmailSendError("Gmail returned no message id")
        return {"id": result["id"], "threadId": result.get("threadId")}


if __name__ == "__main__":
    import sys

    from config.settings import get_settings

    if sys.argv[1:] == ["authorize"]:
        s = get_settings()
        authorize(s.gmail_credentials_file, s.gmail_token_file)
        print(f"Gmail connected; token saved to {s.gmail_token_file}")
    else:
        print("usage: python -m integrations.gmail authorize")
