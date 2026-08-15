r"""
Klaviyo OAuth 2.0 auth helper (PKCE authorization-code flow).

Klaviyo "apps" authenticate with OAuth, NOT a `pk_` private key. This helper
turns a one-time authorization `code` into the long-lived value the connector
needs: a refresh token (KLAVIYO_REFRESH_TOKEN). `klaviyo_sync.py` then mints a
fresh ~1-hour Bearer access token from that refresh token whenever OAuth is
the configured auth mode (see its `_auth_mode()` / `_session()`) — this is an
ALTERNATIVE to the simpler private-API-key path klaviyo_sync.py supports by
default, useful if you'd rather not hand out a long-lived private key, or
you're building a multi-tenant integration.

Mirrors amazon_auth.py / tiktok_auth.py / google_auth.py's shape (manual
paste-the-code flow so it works with an https://localhost return URL — the
browser lands on a dead page but the ?code= in the address bar is what you
copy back).

OAUTH CONTRACT (verify against Klaviyo's current docs before assuming this
never drifts):
  - authorize:  https://www.klaviyo.com/oauth/authorize   (user consent, in browser)
  - token:      https://a.klaviyo.com/oauth/token         (exchange + refresh)
  - PKCE is MANDATORY: we generate a code_verifier, send its S256 code_challenge
    on the authorize URL, and send the plain code_verifier on the exchange.
  - token calls authenticate with HTTP Basic (client_id:client_secret), body is
    application/x-www-form-urlencoded.
  - access tokens live ~1 hour (rely on expires_in); refresh tokens do NOT
    rotate on every call, but we persist a new one whenever the response
    returns one.
  - API requests use `Authorization: Bearer <access_token>`.

ONE-TIME SETUP (in the Klaviyo app you create, once):
  1. Put the app's Client ID + Secret in .env as KLAVIYO_CLIENT_ID /
     KLAVIYO_CLIENT_SECRET.
  2. In the app settings, add an Allowed Redirect URI that BYTE-matches
     KLAVIYO_REDIRECT_URI in .env (default https://localhost — no trailing
     slash; trailing slashes matter, same lesson as amazon_auth.py).
  3. Enable these scopes on the app (the authorize request asks for exactly
     these; the app must permit them or consent fails):
       accounts:read campaigns:read flows:read metrics:read segments:read
     Override the requested set with KLAVIYO_OAUTH_SCOPES if needed.

USAGE
-----
Print the consent URL to open in your browser (also stashes the PKCE verifier):
    python klaviyo_auth.py --url

Approve with your Klaviyo account. Your browser lands on the redirect URI
with ?code=XXXX in the address bar (the page itself won't load — that's
fine). Copy that code and run:
    python klaviyo_auth.py PASTE_THE_CODE_HERE

That saves KLAVIYO_REFRESH_TOKEN. Sanity-check that a live access token can be
minted (no new code needed):
    python klaviyo_auth.py --refresh
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
import urllib.parse

import requests
from dotenv import load_dotenv, set_key

load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

AUTHORIZE_URL = "https://www.klaviyo.com/oauth/authorize"
TOKEN_URL = "https://a.klaviyo.com/oauth/token"

# Read-only scopes the connector needs: campaigns/flows/segments reports +
# the conversion metric + account lookup. Overridable via .env.
DEFAULT_SCOPES = "accounts:read campaigns:read flows:read metrics:read segments:read"


class KlaviyoAuthError(RuntimeError):
    """Auth/token failure. RuntimeError so a caller can catch + log it per
    section instead of a whole scheduled step hard-exiting."""


def _need(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise KlaviyoAuthError(
            f"Missing {name} in .env - add it first (see the header of this file).")
    return val


def _redirect_uri() -> str:
    return os.environ.get("KLAVIYO_REDIRECT_URI", "https://localhost")


def _scopes() -> str:
    return os.environ.get("KLAVIYO_OAUTH_SCOPES", DEFAULT_SCOPES)


# --------------------------------------------------------------------------- #
#  PKCE
# --------------------------------------------------------------------------- #
def _make_verifier() -> str:
    # 43-128 chars, URL-safe (RFC 7636). token_urlsafe(64) -> ~86 chars.
    return secrets.token_urlsafe(64)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
#  flow steps
# --------------------------------------------------------------------------- #
def auth_url() -> str:
    """Build the consent URL and stash the PKCE verifier for the exchange step."""
    verifier = _make_verifier()
    set_key(ENV_PATH, "KLAVIYO_OAUTH_VERIFIER", verifier)
    params = {
        "response_type": "code",
        "client_id": _need("KLAVIYO_CLIENT_ID"),
        "redirect_uri": _redirect_uri(),
        "scope": _scopes(),
        "state": secrets.token_urlsafe(16),
        "code_challenge_method": "S256",
        "code_challenge": _challenge(verifier),
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _token_request(data: dict) -> dict:
    """POST to the token endpoint with HTTP Basic client auth."""
    resp = requests.post(
        TOKEN_URL,
        data=data,
        auth=(_need("KLAVIYO_CLIENT_ID"), _need("KLAVIYO_CLIENT_SECRET")),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if not resp.ok:
        raise KlaviyoAuthError(f"Klaviyo token endpoint {resp.status_code}: {resp.text[:600]}")
    return resp.json()


def exchange(code: str) -> dict:
    """auth code (+ stashed PKCE verifier) -> access + refresh tokens."""
    code = urllib.parse.unquote(code).strip()
    verifier = os.environ.get("KLAVIYO_OAUTH_VERIFIER")
    if not verifier:
        raise KlaviyoAuthError(
            "No KLAVIYO_OAUTH_VERIFIER in .env - run `python klaviyo_auth.py --url` "
            "first (same run that prints the URL stashes the PKCE verifier).")
    data = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": verifier,
    })
    if "refresh_token" not in data:
        raise KlaviyoAuthError(f"Klaviyo exchange returned no refresh_token: {data}")
    return data


def access_token() -> str:
    """Mint a fresh Bearer access token from the stored refresh token.

    Persists a rotated refresh token if Klaviyo returns one (it usually
    doesn't, but be safe). klaviyo_sync.py implements its own minimal version
    of this refresh call (see its `_oauth_access_token()`) rather than
    importing this module, matching how the other connectors in this project
    keep their auth helper CLI separate from the sync script - but this
    function is the reference implementation and is handy for a manual
    sanity-check (`python klaviyo_auth.py --refresh`)."""
    data = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": _need("KLAVIYO_REFRESH_TOKEN"),
    })
    if "access_token" not in data:
        raise KlaviyoAuthError(f"Klaviyo refresh returned no access_token: {data}")
    new_rt = data.get("refresh_token")
    if new_rt and new_rt != os.environ.get("KLAVIYO_REFRESH_TOKEN"):
        set_key(ENV_PATH, "KLAVIYO_REFRESH_TOKEN", new_rt)
        os.environ["KLAVIYO_REFRESH_TOKEN"] = new_rt
    return data["access_token"]


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    try:
        _dispatch(sys.argv[1])
    except KlaviyoAuthError as e:
        sys.exit(str(e))


def _dispatch(arg: str) -> None:

    if arg == "--url":
        url = auth_url()  # computed first so a missing-credential error prints cleanly
        print("\nOpen this URL in your browser, sign in to your Klaviyo account, "
              "and approve:\n")
        print(url)
        print(f"\n(Requesting scopes: {_scopes()})")
        print(f"(Redirect URI: {_redirect_uri()} - it must be in the app's allowlist)")
        print("\nAfter approving, copy the ?code=... value from the address bar and run:")
        print("    python klaviyo_auth.py PASTE_THE_CODE_HERE")
        return

    if arg == "--refresh":
        tok = access_token()
        print(f"OK - minted an access token ({len(tok)} chars). Refresh token is valid.")
        print("You can now run:  python klaviyo_sync.py")
        return

    # first-time flow: exchange the code for tokens
    data = exchange(arg)
    set_key(ENV_PATH, "KLAVIYO_REFRESH_TOKEN", data["refresh_token"])
    set_key(ENV_PATH, "KLAVIYO_OAUTH_VERIFIER", "")  # consumed; clear it
    scope = data.get("scope", "(not returned)")
    print("Refresh token saved to .env.")
    print(f"Granted scopes: {scope}")
    print("\nSanity-check + first sync:")
    print("    python klaviyo_auth.py --refresh")
    print("    python klaviyo_sync.py")


if __name__ == "__main__":
    main()
