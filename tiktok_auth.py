"""
TikTok Shop auth helper.

Turns a one-time `auth_code` (the string after code= in the redirect URL)
into the values the connector needs: access token, refresh token, shop
cipher, shop id.

USAGE
-----
First time (you have a fresh auth code):
    python tiktok_auth.py PASTE_THE_CODE_HERE

If the token already saved but the shop lookup failed, just fetch the shop
(no new code needed — uses the saved access token):
    python tiktok_auth.py --shops

Refresh an expired access token later (no browser needed):
    python tiktok_auth.py --refresh
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
import urllib.parse

import requests
from dotenv import load_dotenv, set_key

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

AUTH_HOST = "https://auth.tiktok-shops.com"
API_HOST = "https://open-api.tiktokglobalshop.com"


def _need(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing {name} in .env — add it first.")
    return val


def _sign(path: str, params: dict, secret: str) -> str:
    """
    TikTok Shop request signature for GET calls (no body).
    Concatenate secret + path + each sorted key/value + secret, HMAC-SHA256.
    'sign' and 'access_token' are excluded from the signed string.
    """
    ordered = "".join(
        f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token")
    )
    base_string = f"{secret}{path}{ordered}{secret}"
    return hmac.new(secret.encode(), base_string.encode(), hashlib.sha256).hexdigest()


def exchange(auth_code: str) -> dict:
    """auth_code -> access_token + refresh_token (token host is NOT signed)."""
    auth_code = urllib.parse.unquote(auth_code).strip()
    resp = requests.get(
        f"{AUTH_HOST}/api/v2/token/get",
        params={
            "app_key": _need("TIKTOK_APP_KEY"),
            "app_secret": _need("TIKTOK_APP_SECRET"),
            "auth_code": auth_code,
            "grant_type": "authorized_code",
        },
        timeout=60,
    )
    data = resp.json()
    if data.get("code") != 0:
        sys.exit(f"TikTok token error: {data}")
    return data["data"]


def refresh() -> dict:
    resp = requests.get(
        f"{AUTH_HOST}/api/v2/token/refresh",
        params={
            "app_key": _need("TIKTOK_APP_KEY"),
            "app_secret": _need("TIKTOK_APP_SECRET"),
            "refresh_token": _need("TIKTOK_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=60,
    )
    data = resp.json()
    if data.get("code") != 0:
        sys.exit(f"TikTok refresh error: {data}")
    return data["data"]


def get_shops(access_token: str) -> list[dict]:
    """Fetch authorized shops. open-api host REQUIRES a signed request."""
    app_key = _need("TIKTOK_APP_KEY")
    secret = _need("TIKTOK_APP_SECRET")
    path = "/authorization/202309/shops"
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time())),
    }
    params["sign"] = _sign(path, params, secret)

    resp = requests.get(
        f"{API_HOST}{path}",
        params=params,
        headers={"x-tts-access-token": access_token},
        timeout=60,
    )
    data = resp.json()
    if data.get("code") != 0:
        sys.exit(f"TikTok shops error ({resp.status_code}): {data}")
    return (data.get("data") or {}).get("shops", [])


def _save(access_token="", refresh_token="", shop_cipher="", shop_id="") -> None:
    if access_token:
        set_key(ENV_PATH, "TIKTOK_ACCESS_TOKEN", access_token)
    if refresh_token:
        set_key(ENV_PATH, "TIKTOK_REFRESH_TOKEN", refresh_token)
    if shop_cipher:
        set_key(ENV_PATH, "TIKTOK_SHOP_CIPHER", shop_cipher)
    if shop_id:
        set_key(ENV_PATH, "TIKTOK_SHOP_ID", shop_id)


def _report_shops(shops: list[dict]) -> None:
    if not shops:
        print("No authorized shops came back. Make sure the app is authorized "
              "to your shop in Partner Center.")
        return
    s = shops[0]
    _save(shop_cipher=s.get("cipher", ""), shop_id=s.get("id", ""))
    print("\n=== Shop saved to .env ===")
    print(f"TIKTOK_SHOP_CIPHER={s.get('cipher')}")
    print(f"TIKTOK_SHOP_ID={s.get('id')}   ({s.get('name')})")
    if len(shops) > 1:
        print(f"\n({len(shops)} shops found; saved the first. Others:)")
        for o in shops[1:]:
            print(f"   - {o.get('name')}: id={o.get('id')} cipher={o.get('cipher')}")
    print("\nYou can now run:  python run_sync.py --only tiktok")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    arg = sys.argv[1]

    if arg == "--refresh":
        tok = refresh()
        _save(tok["access_token"], tok["refresh_token"])
        print("Access token refreshed and saved to .env.")
        return

    if arg == "--shops":
        _report_shops(get_shops(_need("TIKTOK_ACCESS_TOKEN")))
        return

    # first-time flow: exchange the auth code, then fetch shops
    tok = exchange(arg)
    _save(tok["access_token"], tok["refresh_token"])
    print("Access + refresh tokens saved to .env.")
    _report_shops(get_shops(tok["access_token"]))


if __name__ == "__main__":
    main()
