"""
Google Ads refresh-token helper.

Runs the one-time OAuth consent flow and saves GOOGLE_ADS_REFRESH_TOKEN to
your .env. You only need to do this once per Google login.

BEFORE RUNNING, put these in .env (from your Google Cloud OAuth client):
    GOOGLE_ADS_CLIENT_ID=...
    GOOGLE_ADS_CLIENT_SECRET=...

Then:
    python google_auth.py

A browser window opens; sign in with the Google account that has access to
your Google Ads account, and approve. The refresh token is captured and saved
automatically.

Note: the OAuth client in Google Cloud must be of type "Desktop app" for this
local flow to work.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv, set_key

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

SCOPES = ["https://www.googleapis.com/auth/adwords"]


def main() -> None:
    client_id = os.environ.get("GOOGLE_ADS_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_ADS_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Add GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET to .env first.")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install google-ads")

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

    set_key(ENV_PATH, "GOOGLE_ADS_REFRESH_TOKEN", creds.refresh_token)
    print("\n=== Saved to .env ===")
    print(f"GOOGLE_ADS_REFRESH_TOKEN={creds.refresh_token[:12]}...")
    print("\nNow fill in GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID,")
    print("and GOOGLE_ADS_CUSTOMER_ID (digits only), then run:")
    print("    python run_sync.py --only google --days 7")


if __name__ == "__main__":
    main()
