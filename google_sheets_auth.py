"""
Google Sheets + Apps Script refresh-token helper.

Runs the one-time OAuth consent flow and saves GOOGLE_SHEETS_REFRESH_TOKEN to
your .env. You only need to do this once per Google login.

Separate credential from GOOGLE_ADS_* (google_auth.py) and from the GA4 /
Merchant Center service account -- Sheets/Apps Script/Drive is a different
API surface with its own scopes, and it acts as the signed-in USER (so it can
edit only Sheets that user can edit), not as a service account. You can point
GOOGLE_SHEETS_CLIENT_ID/SECRET at the same Google Cloud "Desktop app" OAuth
client you made for Google Ads; the refresh token is what's separate.

Also enable, in that Google Cloud project, the Google Sheets API and the
Apps Script API, and switch the Apps Script API ON for the signing-in user at
https://script.google.com/home/usersettings (a per-user toggle, off by
default) -- otherwise every script_* call 403s even with a valid token.

BEFORE RUNNING, put these in .env (from your Google Cloud OAuth client --
reuse the existing "Desktop app" client if one already exists in the project,
otherwise create one: Console > APIs & Services > Credentials > Create
Credentials > OAuth client ID > Desktop app):
    GOOGLE_SHEETS_CLIENT_ID=...
    GOOGLE_SHEETS_CLIENT_SECRET=...

Then:
    python google_sheets_auth.py

A browser window opens; sign in with the Google account that owns/edits the
target Sheet, and approve. The refresh token is captured and saved
automatically.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv, set_key

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

# drive.file (not full drive scope) -- this credential only ever needs to
# touch spreadsheets it creates or that are explicitly shared with it, never
# the account's whole Drive.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/drive.file",
]


def main() -> None:
    client_id = os.environ.get("GOOGLE_SHEETS_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_SHEETS_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Add GOOGLE_SHEETS_CLIENT_ID and GOOGLE_SHEETS_CLIENT_SECRET to .env first.")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install google-auth-oauthlib")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh token comes back
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    if not creds.refresh_token:
        sys.exit("No refresh token returned. Re-run and make sure you approve the consent screen.")

    set_key(ENV_PATH, "GOOGLE_SHEETS_REFRESH_TOKEN", creds.refresh_token)
    print("\n=== Saved to .env ===")
    print(f"GOOGLE_SHEETS_REFRESH_TOKEN={creds.refresh_token[:12]}...")
    print("\nSheets/Apps Script write tools are now usable via google_sheets_script.py")
    print("and, once WAREHOUSE_MCP_ENABLE_WRITES=1 is set, via the MCP server.")


if __name__ == "__main__":
    main()
