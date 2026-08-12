"""
Amazon Advertising auth helper (Login with Amazon / LWA).

Turns a one-time authorization `code` into the values the Amazon Ads connector
needs: a long-lived refresh token (AMAZON_ADS_REFRESH_TOKEN) and the advertising
profile id (AMAZON_ADS_PROFILE_ID). Mirrors google_auth.py / tiktok_auth.py.

ONE-TIME SETUP (do this in a browser, once):
  1. Create a Login-with-Amazon security profile at https://developer.amazon.com
     (App Console > Login with Amazon). Copy its Client ID + Client Secret into
     .env as AMAZON_ADS_CLIENT_ID / AMAZON_ADS_CLIENT_SECRET.
  2. Apply for Amazon Advertising API access and get the app approved — this is
     an external approval gate, not something this script can do.
  3. In the security profile's "Web Settings", add an Allowed Return URL that
     exactly matches AMAZON_ADS_REDIRECT_URI in .env (default: https://localhost).
  4. Set AMAZON_ADS_REGION in .env (NA, EU, or FE).

USAGE
-----
Print the consent URL to open in your browser:
    python amazon_auth.py --url

Then approve. Your browser lands on the return URL with ?code=XXXX in the
address bar (the page itself won't load — that's fine). Copy that code:
    python amazon_auth.py PASTE_THE_CODE_HERE

This saves the refresh token AND looks up + saves your profile id. If the
profile lookup ever needs re-running on its own (no new code needed):
    python amazon_auth.py --profiles
"""
from __future__ import annotations

import os
import sys
import urllib.parse

import requests
from dotenv import load_dotenv, set_key

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

SCOPE = "advertising::campaign_management"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# Where the user logs in + approves. Token exchange always uses api.amazon.com.
AUTH_HOSTS = {
    "NA": "https://www.amazon.com/ap/oa",
    "EU": "https://eu.account.amazon.com/ap/oa",
    "FE": "https://apac.account.amazon.com/ap/oa",
}

# Amazon Ads API hosts (for the /v2/profiles lookup).
API_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}


def _need(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing {name} in .env - add it first (see the header of this file).")
    return val


def _region() -> str:
    region = os.environ.get("AMAZON_ADS_REGION", "NA").upper()
    if region not in AUTH_HOSTS:
        sys.exit(f"AMAZON_ADS_REGION must be one of NA/EU/FE (got {region!r}).")
    return region


def _redirect_uri() -> str:
    return os.environ.get("AMAZON_ADS_REDIRECT_URI", "https://localhost")


def auth_url() -> str:
    params = {
        "client_id": _need("AMAZON_ADS_CLIENT_ID"),
        "scope": SCOPE,
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
    }
    return f"{AUTH_HOSTS[_region()]}?{urllib.parse.urlencode(params)}"


def exchange(code: str) -> str:
    """auth code -> refresh token."""
    code = urllib.parse.unquote(code).strip()
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "client_id": _need("AMAZON_ADS_CLIENT_ID"),
            "client_secret": _need("AMAZON_ADS_CLIENT_SECRET"),
        },
        timeout=60,
    )
    data = resp.json()
    if "refresh_token" not in data:
        sys.exit(f"Amazon token exchange failed ({resp.status_code}): {data}")
    return data["refresh_token"]


def _access_token() -> str:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": _need("AMAZON_ADS_REFRESH_TOKEN"),
            "client_id": _need("AMAZON_ADS_CLIENT_ID"),
            "client_secret": _need("AMAZON_ADS_CLIENT_SECRET"),
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_profiles() -> list[dict]:
    host = API_HOSTS[_region()]
    resp = requests.get(
        f"{host}/v2/profiles",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Amazon-Advertising-API-ClientId": _need("AMAZON_ADS_CLIENT_ID"),
        },
        timeout=60,
    )
    if resp.status_code != 200:
        sys.exit(f"Amazon profiles lookup failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _report_profiles(profiles: list[dict]) -> None:
    if not profiles:
        print("No advertising profiles came back. Make sure the account is "
              "linked and the app is approved for the Advertising API.")
        return
    first = profiles[0]
    set_key(ENV_PATH, "AMAZON_ADS_PROFILE_ID", str(first["profileId"]))
    info = first.get("accountInfo", {})
    print("\n=== Profile saved to .env ===")
    print(f"AMAZON_ADS_PROFILE_ID={first['profileId']}   "
          f"({info.get('name')}, {first.get('countryCode')}, {first.get('currencyCode')})")
    if len(profiles) > 1:
        print(f"\n({len(profiles)} profiles found; saved the first. Others:)")
        for p in profiles[1:]:
            pi = p.get("accountInfo", {})
            print(f"   - {pi.get('name')}: id={p['profileId']} "
                  f"country={p.get('countryCode')} currency={p.get('currencyCode')}")
    print("\nYou can now run:  python run_sync.py --only amazon --days 7")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    arg = sys.argv[1]

    if arg == "--url":
        url = auth_url()  # computed first so a missing-credential error prints cleanly
        print("\nOpen this URL in your browser, sign in, and approve:\n")
        print(url)
        print("\nAfter approving, copy the ?code=... value from the address bar and run:")
        print("    python amazon_auth.py PASTE_THE_CODE_HERE")
        return

    if arg == "--profiles":
        _report_profiles(get_profiles())
        return

    # first-time flow: exchange the code, then fetch profiles
    refresh_token = exchange(arg)
    set_key(ENV_PATH, "AMAZON_ADS_REFRESH_TOKEN", refresh_token)
    print("Refresh token saved to .env.")
    _report_profiles(get_profiles())


if __name__ == "__main__":
    main()
