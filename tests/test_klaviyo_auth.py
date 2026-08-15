"""Hermetic tests for klaviyo_auth.py - no network, no real .env writes.

Every test that would otherwise call `set_key()` patches it out, so nothing
here ever touches this checkout's real .env file. Covers: PKCE verifier/
challenge generation, the redirect-uri/scopes overrides, the auth-URL
builder, the token-exchange and refresh-token flows (mocked HTTP, including
the missing-credential and missing-verifier error paths), and CLI dispatch.
"""
from __future__ import annotations

import base64
import hashlib
import unittest
from unittest.mock import MagicMock, patch

import klaviyo_auth as ka


class PKCETests(unittest.TestCase):
    def test_verifier_is_url_safe_and_correctly_sized(self) -> None:
        v = ka._make_verifier()
        # RFC 7636: 43-128 chars, unreserved charset [A-Z a-z 0-9 - . _ ~]
        self.assertGreaterEqual(len(v), 43)
        self.assertLessEqual(len(v), 128)
        self.assertTrue(all(c.isalnum() or c in "-._~" for c in v))

    def test_verifier_is_not_constant(self) -> None:
        self.assertNotEqual(ka._make_verifier(), ka._make_verifier())

    def test_challenge_is_s256_of_verifier_with_no_padding(self) -> None:
        verifier = "test-verifier-value"
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        challenge = ka._challenge(verifier)
        self.assertEqual(challenge, expected)
        self.assertNotIn("=", challenge)


class ConfigDefaultsTests(unittest.TestCase):
    def test_redirect_uri_defaults_to_localhost(self) -> None:
        with patch.dict(ka.os.environ, {}, clear=False):
            ka.os.environ.pop("KLAVIYO_REDIRECT_URI", None)
            self.assertEqual(ka._redirect_uri(), "https://localhost")

    def test_redirect_uri_can_be_overridden(self) -> None:
        with patch.dict(ka.os.environ, {"KLAVIYO_REDIRECT_URI": "https://example.com/cb"}):
            self.assertEqual(ka._redirect_uri(), "https://example.com/cb")

    def test_scopes_default_to_the_read_only_set(self) -> None:
        with patch.dict(ka.os.environ, {}, clear=False):
            ka.os.environ.pop("KLAVIYO_OAUTH_SCOPES", None)
            self.assertEqual(ka._scopes(), ka.DEFAULT_SCOPES)

    def test_scopes_can_be_overridden(self) -> None:
        with patch.dict(ka.os.environ, {"KLAVIYO_OAUTH_SCOPES": "metrics:read"}):
            self.assertEqual(ka._scopes(), "metrics:read")


class NeedHelperTests(unittest.TestCase):
    def test_missing_var_raises_klaviyo_auth_error(self) -> None:
        with patch.dict(ka.os.environ, {}, clear=False):
            ka.os.environ.pop("KLAVIYO_DOES_NOT_EXIST", None)
            with self.assertRaises(ka.KlaviyoAuthError):
                ka._need("KLAVIYO_DOES_NOT_EXIST")

    def test_present_var_is_returned(self) -> None:
        with patch.dict(ka.os.environ, {"KLAVIYO_CLIENT_ID": "abc123"}):
            self.assertEqual(ka._need("KLAVIYO_CLIENT_ID"), "abc123")


class AuthUrlTests(unittest.TestCase):
    @patch.object(ka, "set_key")
    def test_missing_client_id_raises(self, mock_set_key) -> None:
        # the PKCE verifier is stashed unconditionally before client_id is
        # looked up, so set_key DOES get called here - only the client_id
        # lookup itself should raise.
        with patch.dict(ka.os.environ, {}, clear=False):
            ka.os.environ.pop("KLAVIYO_CLIENT_ID", None)
            with self.assertRaises(ka.KlaviyoAuthError):
                ka.auth_url()

    @patch.object(ka, "set_key")
    def test_builds_expected_query_params(self, mock_set_key) -> None:
        with patch.dict(ka.os.environ, {"KLAVIYO_CLIENT_ID": "abc123"}, clear=False):
            ka.os.environ.pop("KLAVIYO_REDIRECT_URI", None)
            ka.os.environ.pop("KLAVIYO_OAUTH_SCOPES", None)
            url = ka.auth_url()
        self.assertTrue(url.startswith(ka.AUTHORIZE_URL + "?"))
        self.assertIn("client_id=abc123", url)
        self.assertIn("response_type=code", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("code_challenge=", url)

    @patch.object(ka, "set_key")
    def test_stashes_pkce_verifier_via_set_key(self, mock_set_key) -> None:
        with patch.dict(ka.os.environ, {"KLAVIYO_CLIENT_ID": "abc123"}, clear=False):
            ka.auth_url()
        mock_set_key.assert_called_once()
        path, key, _value = mock_set_key.call_args[0]
        self.assertEqual(path, ka.ENV_PATH)
        self.assertEqual(key, "KLAVIYO_OAUTH_VERIFIER")


class ExchangeTests(unittest.TestCase):
    @patch.object(ka.requests, "post")
    def test_missing_verifier_raises_before_any_request(self, mock_post) -> None:
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
        }, clear=False):
            ka.os.environ.pop("KLAVIYO_OAUTH_VERIFIER", None)
            with self.assertRaises(ka.KlaviyoAuthError):
                ka.exchange("some-code")
        mock_post.assert_not_called()

    @patch.object(ka.requests, "post")
    def test_success_sends_verifier_and_unquoted_code(self, mock_post) -> None:
        resp = MagicMock(ok=True)
        resp.json.return_value = {"access_token": "at", "refresh_token": "rt",
                                   "scope": "metrics:read"}
        mock_post.return_value = resp
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
            "KLAVIYO_OAUTH_VERIFIER": "stashed-verifier",
        }, clear=False):
            data = ka.exchange("PASTE%20CODE")
        self.assertEqual(data["refresh_token"], "rt")
        sent = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent["code"], "PASTE CODE")
        self.assertEqual(sent["code_verifier"], "stashed-verifier")
        self.assertEqual(sent["grant_type"], "authorization_code")

    @patch.object(ka.requests, "post")
    def test_response_missing_refresh_token_raises(self, mock_post) -> None:
        resp = MagicMock(ok=True)
        resp.json.return_value = {"access_token": "at"}
        mock_post.return_value = resp
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
            "KLAVIYO_OAUTH_VERIFIER": "v",
        }, clear=False):
            with self.assertRaises(ka.KlaviyoAuthError):
                ka.exchange("code")

    @patch.object(ka.requests, "post")
    def test_token_endpoint_error_surfaces_response_body(self, mock_post) -> None:
        mock_post.return_value = MagicMock(ok=False, status_code=400, text="invalid_grant")
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
            "KLAVIYO_OAUTH_VERIFIER": "v",
        }, clear=False):
            with self.assertRaises(ka.KlaviyoAuthError) as cm:
                ka.exchange("code")
        self.assertIn("invalid_grant", str(cm.exception))


class AccessTokenTests(unittest.TestCase):
    def test_missing_refresh_token_raises(self) -> None:
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
        }, clear=False):
            ka.os.environ.pop("KLAVIYO_REFRESH_TOKEN", None)
            with self.assertRaises(ka.KlaviyoAuthError):
                ka.access_token()

    @patch.object(ka, "set_key")
    @patch.object(ka.requests, "post")
    def test_mints_access_token_without_rotating_refresh_token(self, mock_post, mock_set_key) -> None:
        resp = MagicMock(ok=True)
        resp.json.return_value = {"access_token": "fresh-at"}
        mock_post.return_value = resp
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
            "KLAVIYO_REFRESH_TOKEN": "rt-original",
        }, clear=False):
            tok = ka.access_token()
            self.assertEqual(ka.os.environ["KLAVIYO_REFRESH_TOKEN"], "rt-original")
        self.assertEqual(tok, "fresh-at")
        mock_set_key.assert_not_called()

    @patch.object(ka, "set_key")
    @patch.object(ka.requests, "post")
    def test_persists_rotated_refresh_token_when_returned(self, mock_post, mock_set_key) -> None:
        resp = MagicMock(ok=True)
        resp.json.return_value = {"access_token": "fresh-at", "refresh_token": "rt-new"}
        mock_post.return_value = resp
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
            "KLAVIYO_REFRESH_TOKEN": "rt-original",
        }, clear=False):
            ka.access_token()
            self.assertEqual(ka.os.environ["KLAVIYO_REFRESH_TOKEN"], "rt-new")
        mock_set_key.assert_called_once_with(ka.ENV_PATH, "KLAVIYO_REFRESH_TOKEN", "rt-new")

    @patch.object(ka.requests, "post")
    def test_token_endpoint_error_surfaces_on_refresh(self, mock_post) -> None:
        mock_post.return_value = MagicMock(ok=False, status_code=401, text="invalid_client")
        with patch.dict(ka.os.environ, {
            "KLAVIYO_CLIENT_ID": "id", "KLAVIYO_CLIENT_SECRET": "secret",
            "KLAVIYO_REFRESH_TOKEN": "rt",
        }, clear=False):
            with self.assertRaises(ka.KlaviyoAuthError) as cm:
                ka.access_token()
        self.assertIn("invalid_client", str(cm.exception))


class DispatchTests(unittest.TestCase):
    """CLI dispatch routes to the right function - patched throughout so no
    test ever performs a real network call or writes the real .env."""

    @patch.object(ka, "auth_url", return_value="https://example.com/authorize?x=1")
    def test_url_flag_prints_the_consent_url(self, mock_auth_url) -> None:
        with patch("builtins.print") as mock_print:
            ka._dispatch("--url")
        mock_auth_url.assert_called_once()
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("https://example.com/authorize?x=1", printed)

    @patch.object(ka, "access_token", return_value="tok1234")
    def test_refresh_flag_mints_and_reports_success(self, mock_access_token) -> None:
        with patch("builtins.print") as mock_print:
            ka._dispatch("--refresh")
        mock_access_token.assert_called_once()
        printed = "\n".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("OK", printed)

    @patch.object(ka, "set_key")
    @patch.object(ka, "exchange", return_value={"refresh_token": "rt-abc", "scope": "metrics:read"})
    def test_bare_code_arg_exchanges_and_saves_refresh_token(self, mock_exchange, mock_set_key) -> None:
        with patch("builtins.print"):
            ka._dispatch("SOME-CODE")
        mock_exchange.assert_called_once_with("SOME-CODE")
        mock_set_key.assert_any_call(ka.ENV_PATH, "KLAVIYO_REFRESH_TOKEN", "rt-abc")
        mock_set_key.assert_any_call(ka.ENV_PATH, "KLAVIYO_OAUTH_VERIFIER", "")

    def test_main_with_no_args_exits_with_docstring(self) -> None:
        with patch.object(ka.sys, "argv", ["klaviyo_auth.py"]):
            with self.assertRaises(SystemExit) as cm:
                ka.main()
        self.assertEqual(cm.exception.code, ka.__doc__)

    @patch.object(ka, "_dispatch", side_effect=ka.KlaviyoAuthError("boom"))
    def test_main_turns_auth_error_into_clean_exit(self, mock_dispatch) -> None:
        with patch.object(ka.sys, "argv", ["klaviyo_auth.py", "somecode"]):
            with self.assertRaises(SystemExit) as cm:
                ka.main()
        self.assertEqual(cm.exception.code, "boom")


if __name__ == "__main__":
    unittest.main()
