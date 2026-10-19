from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import env_check

EXAMPLE = """\
# header
# ---------- Shopify (orders) ----------
SHOPIFY_SHOP=
SHOPIFY_CLIENT_SECRET=
# ---------- Meta Ads ----------
META_ACCESS_TOKEN=
META_AD_ACCOUNT_ID=
"""

SECRET = "super-secret-value-123"


class EnvCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.example = self.dir / ".env.example"
        self.example.write_text(EXAMPLE, encoding="utf-8")
        self.env = self.dir / ".env"

    def _report(self, env_text: str | None, words=(), set_only=False) -> str:
        if env_text is not None:
            self.env.write_text(env_text, encoding="utf-8")
        return "\n".join(env_check.report(self.example, self.env, list(words), set_only))

    def test_never_prints_a_value(self) -> None:
        out = self._report(f"SHOPIFY_SHOP=my-store\nSHOPIFY_CLIENT_SECRET={SECRET}\n"
                           f"CUSTOM_THING={SECRET}\n")
        self.assertNotIn(SECRET, out)
        self.assertNotIn("my-store", out)

    def test_states(self) -> None:
        out = self._report("SHOPIFY_SHOP=x\nSHOPIFY_CLIENT_SECRET=\n"
                           "META_ACCESS_TOKEN='# token goes here'\n")
        self.assertIn("set      SHOPIFY_SHOP", out)
        self.assertIn("empty    SHOPIFY_CLIENT_SECRET", out)
        self.assertIn("suspect  META_ACCESS_TOKEN", out)
        self.assertIn("missing  META_AD_ACCOUNT_ID", out)

    def test_block_filter_and_set_only(self) -> None:
        out = self._report("SHOPIFY_SHOP=x\n", words=["meta"])
        self.assertIn("Meta Ads", out)
        self.assertNotIn("Shopify", out)
        out = self._report("SHOPIFY_SHOP=x\n", set_only=True)
        self.assertIn("Shopify", out)
        self.assertNotIn("Meta Ads", out)

    def test_unknown_keys_are_listed_by_name_only(self) -> None:
        out = self._report("SHOPFY_SHOP=zq-unique-value\n")
        self.assertIn("SHOPFY_SHOP", out)
        self.assertNotIn("zq-unique-value", out)

    def test_no_env_file(self) -> None:
        out = self._report(None)
        self.assertIn("copy .env.example to .env", out)
        self.assertIn("missing  SHOPIFY_SHOP", out)

    def test_real_example_parses_into_blocks(self) -> None:
        blocks = env_check.example_blocks(env_check.HERE / ".env.example")
        titles = [t for t, _ in blocks]
        self.assertTrue(any("Shopify" in t for t in titles))
        self.assertIn("SHOPIFY_SHOP", dict(blocks)[next(t for t in titles if "Shopify" in t)])
        # A header whose closing dashes wrap onto the next line still opens a block.
        amazon = next(t for t in titles if "Amazon Seller extras" in t)
        self.assertIn("DATAKIOSK_TIMEOUT_MIN", dict(blocks)[amazon])


if __name__ == "__main__":
    unittest.main()
