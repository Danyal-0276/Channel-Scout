"""Authorize a sending mailbox with Gmail's send-only scope, interactively."""

import argparse
import json
import re
import secrets
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from channel_scout.config import ConfigError
from channel_scout.domain import normalize_email


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("downloaded_desktop_client", type=Path)
    parser.add_argument("--sender", required=True)
    args = parser.parse_args()
    sender = normalize_email(args.sender)
    client_config = json.loads(args.downloaded_desktop_client.read_text(encoding="utf-8-sig"))
    info = client_config.get("installed", {})
    if (not info.get("client_id") or not info.get("client_secret")
            or info.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth"
            or info.get("token_uri") != "https://oauth2.googleapis.com/token"):
        raise ConfigError("Expected a Google Desktop OAuth client JSON file.")
    root = Path(__file__).resolve().parents[1]
    env = root / ".env"
    original = env.read_text(encoding="utf-8-sig")
    fields = ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN", "GMAIL_SENDER_EMAIL")
    for field in fields:
        matches = re.findall(rf"^{field}=(.*)$", original, re.M)
        if len(matches) > 1 or any(value.strip() for value in matches):
            raise ConfigError(f"{field} is already configured or repeated. Review the local file first.")
    flow = InstalledAppFlow.from_client_config(client_config, scopes=["https://www.googleapis.com/auth/gmail.send"])
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, login_hint=sender,
                                        prompt="consent", timeout_seconds=180, authorization_prompt_message="")
    if not credentials.refresh_token:
        raise ConfigError("Google did not provide a refresh token.")
    values = (credentials.client_id, credentials.client_secret, credentials.refresh_token, sender)
    updated = original
    for field, value in zip(fields, values):
        if not re.fullmatch(r"[A-Za-z0-9_./@+=-]+", value or ""):
            raise ConfigError(f"Unexpected format for {field}. The local file was preserved.")
        pattern = re.compile(rf"^{field}=.*$", re.M)
        updated = pattern.sub(lambda _: f"{field}={value}", updated) if pattern.search(updated) else (
            updated.rstrip("\n") + f"\n{field}={value}\n"
        )
    if env.read_text(encoding="utf-8-sig") != original:
        raise ConfigError(".env changed during authorization. The local file was preserved.")
    temporary = root / f".env.scout-{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(env)
    finally:
        temporary.unlink(missing_ok=True)
    print("Gmail OAuth credentials saved. Sender mode and its database switch were preserved.")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as error:
        sys.exit(str(error))
    except Exception as error:
        sys.exit(f"Gmail setup stopped ({type(error).__name__}).")
