"""Hermetic tests for catalog_sync.py -- no network, temp/in-memory SQLite.

Covers the Shopify incremental / targeted query shapes, the image-gallery cap
and whole-URL join, per-variant row shaping (image fallback, gallery
exclusion, tags, metafields, market pricing, >100-variant pagination), the
price change-log ordering, the TikTok window walk (10k-cap splitting,
token refresh through the shared connector), the Amazon listings parser,
rebuild_via_swap, and main()'s status / exit-code semantics.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import catalog_sync
from warehouse import db
from warehouse.connectors import tiktok_shop


def _empty_page():
    return {"products": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def _product(pid="1000000000001", variants=None, **kw):
    node = {
        "id": f"gid://shopify/Product/{pid}", "title": "Hearts Mini Dress",
        "status": "ACTIVE", "vendor": "Example Vendor", "tags": ["summer", "new-in"],
        "handle": "hearts-mini-dress", "description": "A dress.",
        "featuredImage": {"url": "https://cdn.example.com/hero.jpg"},
        "images": {"nodes": [{"url": "https://cdn.example.com/hero.jpg"},
                             {"url": "https://cdn.example.com/back.jpg"},
                             {"url": "https://cdn.example.com/red.jpg"}]},
        "variants": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                     "nodes": variants if variants is not None else [_variant()]},
    }
    node.update(kw)
    return node


def _variant(vid="2000000000001", sku="SKU-1001", image=None, **kw):
    v = {"id": f"gid://shopify/ProductVariant/{vid}", "sku": sku, "title": "S",
         "price": "40.00", "compareAtPrice": "60.00", "image": image,
         "inventoryItem": {"unitCost": {"amount": "12.50"}}}
    v.update(kw)
    return v


class _Env(unittest.TestCase):
    """Clears the optional catalog env knobs so a developer's .env cannot leak in."""
    KNOBS = ("SHOPIFY_CATALOG_MARKETS", "SHOPIFY_CATALOG_METAFIELDS",
             "CATALOG_SKU_STRIP_SUFFIXES", "CATALOG_TITLE_MARKERS",
             "CATALOG_VARIANT_DELIMITER")

    def setUp(self) -> None:
        saved = {k: os.environ.pop(k) for k in self.KNOBS if k in os.environ}
        self.addCleanup(os.environ.update, saved)
        for k in self.KNOBS:
            self.addCleanup(os.environ.pop, k, None)


class ShopifyQueryShapeTests(_Env):
    @patch("catalog_sync.shopify._post", return_value=_empty_page())
    def test_days_adds_updated_at_filter(self, post) -> None:
        catalog_sync.fetch_shopify_catalog(days=3)
        variables = post.call_args.args[0]
        self.assertTrue(variables["query"].startswith("updated_at:>='"))
        self.assertTrue(variables["query"].endswith("'"))

    @patch("catalog_sync.shopify._post", return_value=_empty_page())
    def test_full_refresh_has_no_updated_filter(self, post) -> None:
        catalog_sync.fetch_shopify_catalog()
        self.assertIsNone(post.call_args.args[0]["query"])

    @patch("catalog_sync.shopify._post", return_value=_empty_page())
    def test_product_ids_builds_an_id_query_not_a_date_filter(self, post) -> None:
        catalog_sync.fetch_shopify_catalog(product_ids=["1000000000001", "1000000000002"])
        self.assertEqual(post.call_args.args[0]["query"],
                         "id:1000000000001 OR id:1000000000002")

    @patch("catalog_sync.shopify._post", return_value=_empty_page())
    def test_product_ids_are_chunked_and_accept_gids(self, post) -> None:
        ids = [f"gid://shopify/Product/{1000000000000 + i}" for i in range(120)]
        catalog_sync.fetch_shopify_catalog(product_ids=ids)
        queries = [c.args[0]["query"] for c in post.call_args_list]
        self.assertEqual(len(queries), 3)            # 50 + 50 + 20
        self.assertEqual(queries[2].count("id:"), 20)
        self.assertNotIn("gid://", queries[0])

    def test_non_numeric_product_ids_are_rejected(self) -> None:
        # The ids are spliced into Shopify search syntax; never pass text through.
        with self.assertRaises(ValueError):
            catalog_sync.fetch_shopify_catalog(product_ids=["123 OR status:active"])

    def test_days_and_product_ids_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            catalog_sync.fetch_shopify_catalog(days=3, product_ids=["1"])

    @patch("catalog_sync.shopify._post", return_value=_empty_page())
    def test_image_gallery_uses_our_own_cap(self, post) -> None:
        """images(first: 10) once silently dropped every gallery image past
        the 10th. The variable must carry IMAGE_GALLERY_CAP (25)."""
        catalog_sync.fetch_shopify_catalog(days=3)
        self.assertEqual(post.call_args.args[0]["imageCap"], catalog_sync.IMAGE_GALLERY_CAP)
        self.assertEqual(catalog_sync.IMAGE_GALLERY_CAP, 25)
        self.assertIn("images(first: $imageCap)", post.call_args.args[1])

    def test_markets_and_metafields_extend_the_query(self) -> None:
        q = catalog_sync.build_catalog_query(
            ("CA", "GB"), {"material": ("custom", "material")})
        self.assertIn("market_CA: contextualPricing(context: {country: CA})", q)
        self.assertIn("market_GB:", q)
        self.assertIn('mf_material: metafield(namespace: "custom", key: "material")', q)
        plain = catalog_sync.build_catalog_query()
        self.assertNotIn("contextualPricing", plain)
        self.assertNotIn("metafield(", plain)

    def test_bad_market_and_metafield_config_is_rejected(self) -> None:
        os.environ["SHOPIFY_CATALOG_MARKETS"] = "CA,Canada"
        with self.assertRaises(ValueError):
            catalog_sync.market_countries()
        os.environ["SHOPIFY_CATALOG_METAFIELDS"] = 'material=custom.mat") { x } #'
        with self.assertRaises(ValueError):
            catalog_sync.metafield_map()
        os.environ["SHOPIFY_CATALOG_METAFIELDS"] = "colour=custom.colour"   # not a column
        with self.assertRaises(ValueError):
            catalog_sync.metafield_map()


class GalleryJoinTests(unittest.TestCase):
    """A flat `[:N]` slice of the joined string cut the last URL in half.
    _join_gallery must only ever drop whole URLs."""

    def test_short_gallery_is_untouched(self) -> None:
        urls = ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
        self.assertEqual(catalog_sync._join_gallery(urls), ",".join(urls))

    def test_empty_gallery_is_none_not_empty_string(self) -> None:
        self.assertIsNone(catalog_sync._join_gallery([]))

    def test_oversized_gallery_drops_whole_urls_never_a_partial_one(self) -> None:
        long_url = "https://cdn.example.com/" + ("x" * 172) + ".jpg"
        self.assertEqual(len(long_url), 200)
        result = catalog_sync._join_gallery([long_url] * 60)   # 12,000 chars
        self.assertLessEqual(len(result), catalog_sync._ADDITIONAL_IMAGES_MAX_CHARS)
        parts = result.split(",")
        for part in parts:
            self.assertEqual(part, long_url)
        self.assertLess(len(parts), 60)


class ShapeProductTests(_Env):
    def test_variant_image_falls_back_and_gallery_excludes_the_rows_own_image(self) -> None:
        p = _product(variants=[
            _variant("2000000000001", image=None),
            _variant("2000000000002", sku="SKU-1002",
                     image={"url": "https://cdn.example.com/red.jpg"}),
        ])
        rows, _ = catalog_sync.shape_product(p, p["variants"]["nodes"])
        a, b = rows
        self.assertEqual(a["image_url"], "https://cdn.example.com/hero.jpg")
        self.assertNotIn("hero.jpg", a["additional_images"])
        self.assertIn("red.jpg", a["additional_images"])
        # per VARIANT: b's primary is red.jpg, so hero.jpg is back in its gallery
        self.assertEqual(b["image_url"], "https://cdn.example.com/red.jpg")
        self.assertNotIn("red.jpg", b["additional_images"])
        self.assertIn("hero.jpg", b["additional_images"])

    def test_ids_prices_and_tags(self) -> None:
        p = _product()
        (row,), _ = catalog_sync.shape_product(p, p["variants"]["nodes"])
        self.assertEqual(row["variant_id"], "2000000000001")
        self.assertEqual(row["product_id"], "1000000000001")
        self.assertEqual((row["price"], row["compare_at_price"], row["unit_cost"]),
                         (40.0, 60.0, 12.5))
        self.assertEqual(row["tags"], "summer, new-in")
        self.assertEqual(set(row), set(catalog_sync.CATALOG_SHOPIFY_COLS))

    def test_no_tags_stores_null(self) -> None:
        p = _product(tags=[])
        (row,), _ = catalog_sync.shape_product(p, p["variants"]["nodes"])
        self.assertIsNone(row["tags"])

    def test_metafields_and_defensive_launch_date(self) -> None:
        mfs = {"material": ("custom", "material"), "launch_date_epoch": ("custom", "launch")}
        p = _product(mf_material={"value": "100% Cotton"},
                     mf_launch_date_epoch={"value": "1767225600"})
        (row,), _ = catalog_sync.shape_product(p, p["variants"]["nodes"], (), mfs)
        self.assertEqual(row["material"], "100% Cotton")
        self.assertEqual(row["launch_date_epoch"], 1767225600)
        bad = _product(mf_launch_date_epoch={"value": "next tuesday"})
        (row,), _ = catalog_sync.shape_product(bad, bad["variants"]["nodes"], (), mfs)
        self.assertIsNone(row["launch_date_epoch"])

    def test_market_pricing_rows_carry_currency_and_skip_unobserved(self) -> None:
        v = _variant(market_CA={"price": {"amount": "56.00", "currencyCode": "CAD"},
                                "compareAtPrice": {"amount": "84.00", "currencyCode": "CAD"}},
                     market_GB=None)
        p = _product(variants=[v])
        _, mkt = catalog_sync.shape_product(p, [v], ("CA", "GB"))
        self.assertEqual(mkt, [{"variant_id": "2000000000001", "country": "CA",
                                "currency": "CAD", "price": 56.0,
                                "compare_at_price": 84.0}])

    @patch("catalog_sync.shopify._post")
    def test_more_than_one_page_of_variants_is_paginated_not_truncated(self, post) -> None:
        first = _product(variants=[_variant("2000000000001")])
        first["variants"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c1"}
        post.side_effect = [
            {"products": {"nodes": [first],
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}},
            {"product": {"variants": {
                "nodes": [_variant("2000000000002", sku="SKU-1002")],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ]
        rows, _ = catalog_sync.fetch_shopify_catalog()
        self.assertEqual([r["variant_id"] for r in rows], ["2000000000001", "2000000000002"])
        self.assertEqual(post.call_args_list[1].args[0],
                         {"id": "gid://shopify/Product/1000000000001", "cursor": "c1"})


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    catalog_sync.ensure_schema(conn)
    return conn


def _row(vid="2000000000001", price=40.0, cap=60.0, **kw):
    r = {c: None for c in catalog_sync.CATALOG_SHOPIFY_COLS}
    r.update(variant_id=vid, product_id="1000000000001", sku="SKU-1001",
             price=price, compare_at_price=cap, status="ACTIVE")
    r.update(kw)
    return r


class PriceHistoryTests(unittest.TestCase):
    def test_new_changed_and_unchanged_variants(self) -> None:
        conn = _mem_conn()
        catalog_sync.store_shopify_page(conn, [_row()], [], "2026-01-01T00:00:00+00:00")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shopify_price_history")
                         .fetchone()[0], 1)            # new -> logged
        catalog_sync.store_shopify_page(conn, [_row()], [], "2026-01-02T00:00:00+00:00")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shopify_price_history")
                         .fetchone()[0], 1)            # unchanged -> not logged
        catalog_sync.store_shopify_page(conn, [_row(price=30.0)], [],
                                        "2026-01-03T00:00:00+00:00")
        self.assertEqual(conn.execute(
            "SELECT observed_date, price FROM shopify_price_history ORDER BY observed_date")
            .fetchall(), [("2026-01-01", 40.0), ("2026-01-03", 30.0)])
        # and the snapshot itself moved
        self.assertEqual(conn.execute("SELECT price FROM catalog_shopify").fetchone()[0], 30.0)

    def test_no_price_observed_is_not_a_baseline(self) -> None:
        conn = _mem_conn()
        catalog_sync.store_shopify_page(conn, [_row(price=None, cap=None)], [], "2026-01-01T00")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shopify_price_history")
                         .fetchone()[0], 0)

    def test_insert_is_named_so_a_migrated_column_order_cannot_shift_values(self) -> None:
        """A table created by an older version (fewer columns) gets the rest via
        ALTER TABLE at its physical END. A positional insert would misalign."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE catalog_shopify (variant_id TEXT NOT NULL PRIMARY KEY, "
                     "product_id TEXT NOT NULL, sku TEXT, synced_at TEXT NOT NULL, "
                     "title TEXT)")
        catalog_sync.ensure_schema(conn)
        catalog_sync.store_shopify_page(conn, [_row(title="Hearts Mini Dress",
                                                    tags="summer")], [], "2026-01-01T00")
        self.assertEqual(conn.execute(
            "SELECT title, tags, price, sku FROM catalog_shopify").fetchone(),
            ("Hearts Mini Dress", "summer", 40.0, "SKU-1001"))


class TikTokWindowTests(unittest.TestCase):
    ENV = {"TIKTOK_APP_KEY": "k", "TIKTOK_APP_SECRET": "s",
           "TIKTOK_SHOP_CIPHER": "c", "TIKTOK_ACCESS_TOKEN": "old"}

    @staticmethod
    def _resp(payload):
        m = Mock()
        m.json.return_value = payload
        m.status_code = 200
        return m

    GOOD = {"code": 0, "data": {"products": [
        {"id": 3000000000001, "title": "T", "status": "ACTIVATE",
         "skus": [{"id": 9, "seller_sku": "SKU-1001",
                   "price": {"sale_price": "5", "currency": "USD"}}]}]}}

    def test_expired_token_is_refreshed_and_the_request_rebuilt(self) -> None:
        """The token expires about weekly; the shared connector refreshes ONCE
        and rebuilds + re-signs the request (fresh token, fresh timestamp)."""
        expired = {"code": 105002, "message": "Expired credentials"}

        def _rotate():
            os.environ["TIKTOK_ACCESS_TOKEN"] = "new"

        with patch.dict(os.environ, self.ENV), \
             patch.object(tiktok_shop.requests, "post",
                          side_effect=[self._resp(expired), self._resp(self.GOOD)]) as post, \
             patch.object(tiktok_shop, "_refresh_access_token", side_effect=_rotate) as refresh, \
             patch.object(tiktok_shop.time, "time", side_effect=[1000.0, 2000.0]):
            rows = catalog_sync._fetch_tiktok_window(0, 1)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(rows, [("3000000000001", "9", "SKU-1001", "T", "ACTIVATE", 5.0, "USD")])
        first, second = post.call_args_list
        self.assertEqual(first.kwargs["headers"]["x-tts-access-token"], "old")
        self.assertEqual(second.kwargs["headers"]["x-tts-access-token"], "new")
        self.assertEqual((first.kwargs["params"]["timestamp"],
                          second.kwargs["params"]["timestamp"]), ("1000", "2000"))
        self.assertNotEqual(first.kwargs["params"]["sign"], second.kwargs["params"]["sign"])

    def test_still_expired_after_refresh_raises(self) -> None:
        expired = {"code": 105002, "message": "Expired credentials"}
        with patch.dict(os.environ, self.ENV), \
             patch.object(tiktok_shop.requests, "post",
                          side_effect=[self._resp(expired), self._resp(expired)]), \
             patch.object(tiktok_shop, "_refresh_access_token"):
            with self.assertRaises(RuntimeError) as ctx:
                catalog_sync._fetch_tiktok_window(0, 1)
        self.assertIn("105002", str(ctx.exception))

    def test_a_non_token_error_is_not_retried(self) -> None:
        boom = {"code": 12345, "message": "something else"}
        with patch.dict(os.environ, self.ENV), \
             patch.object(tiktok_shop.requests, "post",
                          side_effect=[self._resp(boom)]) as post, \
             patch.object(tiktok_shop, "_refresh_access_token") as refresh:
            with self.assertRaises(RuntimeError):
                catalog_sync._fetch_tiktok_window(0, 1)
        self.assertEqual(post.call_count, 1)
        refresh.assert_not_called()

    def test_over_cap_window_splits_and_a_1h_overflow_is_reported(self) -> None:
        calls = []

        def fake(a, b, field):
            calls.append((a, b))
            # Over the cap until windows are under an hour, and the very first
            # slice never fits at all (a bulk-listing hour).
            if b - a > 3600 or a == 0:
                raise RuntimeError(f"TikTok API error {catalog_sync.TIKTOK_OVER_CAP}: too many")
            return [("p", str(a), "s", "t", "ACTIVATE", 1.0, "USD")]

        with patch.object(catalog_sync, "_fetch_tiktok_window", side_effect=fake):
            rows, skipped = catalog_sync.fetch_tiktok_catalog(days=1, now=86400)
        self.assertEqual(calls[0], (0, 86400))
        # 86400 halves five times to 2700s windows: 32 of them, one unlistable.
        self.assertEqual(skipped, [(0, 2700)])
        self.assertEqual(len(rows), 31)

    def test_incremental_uses_update_time_and_full_uses_create_time(self) -> None:
        with patch.object(catalog_sync, "_fetch_tiktok_window", return_value=[]) as f:
            catalog_sync.fetch_tiktok_catalog(days=2, now=10 * 86400)
            self.assertEqual(f.call_args.args, (8 * 86400, 10 * 86400, "update_time"))
            catalog_sync.fetch_tiktok_catalog(now=10 ** 10)
            self.assertEqual(f.call_args.args[2], "create_time")


class AmazonListingsParserTests(unittest.TestCase):
    def test_parses_by_header_name_and_skips_blank_skus(self) -> None:
        text = ("item-name\tseller-sku\tprice\tquantity\tasin1\tstatus\n"
                "Hearts Mini Dress\tSKU-1001-FBA\t40.00\t3\tB000000001\tActive\n"
                "No SKU\t\t1\t1\tB000000002\tActive\n"
                "Tote\tSKU-1002\t\t\tB000000003\tInactive\n")
        self.assertEqual(catalog_sync.parse_listings_report(text), [
            ("SKU-1001-FBA", "B000000001", "Hearts Mini Dress", 40.0, 3, "Active"),
            ("SKU-1002", "B000000003", "Tote", None, None, "Inactive"),
        ])

    def test_decode_falls_back_to_cp1252(self) -> None:
        self.assertEqual(catalog_sync._decode_report("caf\xe9".encode("cp1252")), "caf\xe9")
        self.assertEqual(catalog_sync._decode_report("caf\xe9".encode("utf-8")), "caf\xe9")


class RebuildViaSwapTests(unittest.TestCase):
    COLS = catalog_sync.XREF_COLS

    def _conn(self, factory=sqlite3.Connection) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", factory=factory)
        catalog_sync.ensure_schema(conn)
        conn.execute("INSERT INTO product_xref VALUES "
                     "('old','OLD','amazon','A1','S1','Old','t0')")
        conn.commit()
        return conn

    def _rows(self, n: int) -> list[tuple]:
        return [(f"k{i}", f"S{i}", "amazon", f"A{i}", f"SKU{i}", f"T{i}", "t1")
                for i in range(n)]

    def test_replaces_contents_and_drops_scaffolding(self) -> None:
        conn = self._conn()
        written = catalog_sync.rebuild_via_swap(conn, "product_xref", self.COLS,
                                                self._rows(120), chunk=50)
        self.assertEqual(written, 120)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_xref").fetchone()[0], 120)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM product_xref WHERE match_key='old'").fetchone()[0], 0)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("product_xref__new", names)
        self.assertNotIn("product_xref__old", names)

    def test_index_names_stay_canonical(self) -> None:
        conn = self._conn()
        catalog_sync.rebuild_via_swap(conn, "product_xref", self.COLS, self._rows(10), chunk=5)
        idx = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='product_xref' AND sql IS NOT NULL"))
        self.assertEqual(idx, ["idx_xref_channel_id", "idx_xref_key", "idx_xref_skukey"])

    def test_a_view_over_the_table_follows_the_swap(self) -> None:
        """In modern ALTER mode the first RENAME would re-point the view at
        product_xref__old, which is then dropped -- breaking the view."""
        conn = self._conn()
        conn.execute("CREATE VIEW xref_amazon AS SELECT * FROM product_xref "
                     "WHERE channel='amazon'")
        catalog_sync.rebuild_via_swap(conn, "product_xref", self.COLS, self._rows(7))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM xref_amazon").fetchone()[0], 7)

    def test_visible_table_is_never_partial(self) -> None:
        class SpyConn(sqlite3.Connection):
            snapshots: list | None = None

            def executemany(self, sql, seq):  # type: ignore[override]
                result = super().executemany(sql, seq)
                if self.snapshots is not None:
                    self.snapshots.append(
                        self.execute("SELECT match_key FROM product_xref").fetchall())
                return result

        conn = self._conn(SpyConn)
        conn.snapshots = []
        catalog_sync.rebuild_via_swap(conn, "product_xref", self.COLS, self._rows(150), chunk=50)
        seen, conn.snapshots = conn.snapshots, None
        self.assertGreaterEqual(len(seen), 3)
        for snapshot in seen:
            self.assertEqual(snapshot, [("old",)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_xref").fetchone()[0], 150)

    def test_rejects_a_missing_table(self) -> None:
        with self.assertRaises(RuntimeError):
            catalog_sync.rebuild_via_swap(sqlite3.connect(":memory:"), "nope", "a", [("x",)])


class MainStatusTests(_Env):
    """Empty pull -> degraded/75, a channel error -> error/1 while the other
    channels still run, unconfigured channels skipped not failed."""

    CLEAR = ("SHOPIFY_SHOP", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "SHOPIFY_ADMIN_TOKEN",
             "TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_SHOP_CIPHER",
             "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN",
             "SPAPI_MARKETPLACE_ID")

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        orig = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "warehouse.db"
        self.addCleanup(setattr, db, "DB_PATH", orig)
        saved = {k: os.environ.pop(k) for k in self.CLEAR if k in os.environ}
        self.addCleanup(os.environ.update, saved)
        for k in self.CLEAR:
            self.addCleanup(os.environ.pop, k, None)

    def _last_log(self):
        conn = sqlite3.connect(db.DB_PATH)
        row = conn.execute("SELECT status, message FROM sync_log ORDER BY id DESC").fetchone()
        conn.close()
        return row

    def _shopify_env(self):
        os.environ.update(SHOPIFY_SHOP="example.myshopify.com", SHOPIFY_ADMIN_TOKEN="x")

    def test_nothing_configured_skips_every_channel_and_reports_degraded(self) -> None:
        self.assertEqual(catalog_sync.main([]), catalog_sync.EXIT_PAUSED)
        status, msg = self._last_log()
        self.assertEqual(status, "degraded")
        self.assertIn("shopify skipped (not configured)", msg)
        self.assertIn("0 identifier sightings", msg)

    def test_empty_channel_is_degraded_never_ok(self) -> None:
        self._shopify_env()
        with patch.object(catalog_sync, "iter_shopify_catalog", return_value=iter([([], [])])):
            self.assertEqual(catalog_sync.main(["--only-shopify", "--days", "3"]),
                             catalog_sync.EXIT_PAUSED)
        status, msg = self._last_log()
        self.assertEqual(status, "degraded")
        self.assertIn("0 variants returned", msg)

    def test_successful_pull_is_ok_and_builds_the_xref(self) -> None:
        self._shopify_env()
        with patch.object(catalog_sync, "iter_shopify_catalog",
                          return_value=iter([([_row(title="Hearts Mini Dress")], [])])):
            self.assertEqual(catalog_sync.main(["--only-shopify"]), 0)
        status, msg = self._last_log()
        self.assertEqual(status, "ok")
        conn = sqlite3.connect(db.DB_PATH)
        self.assertEqual(conn.execute(
            "SELECT channel, channel_id, sku_key FROM product_xref").fetchall(),
            [("shopify", "2000000000001", "SKU-1001")])
        conn.close()

    def test_one_channel_error_does_not_stop_the_others(self) -> None:
        self._shopify_env()
        os.environ.update(TIKTOK_APP_KEY="k", TIKTOK_APP_SECRET="s",
                          TIKTOK_ACCESS_TOKEN="t", TIKTOK_SHOP_CIPHER="c")
        with patch.object(catalog_sync, "fetch_tiktok_catalog",
                          side_effect=RuntimeError("TikTok API error 1: boom")), \
             patch.object(catalog_sync, "iter_shopify_catalog",
                          return_value=iter([([_row()], [])])):
            self.assertEqual(catalog_sync.main([]), 1)
        status, msg = self._last_log()
        self.assertEqual(status, "error")
        self.assertIn("tiktok error", msg)
        self.assertIn("shopify ok", msg)
        self.assertIn("amazon skipped", msg)

    def test_amazon_report_that_never_finishes_is_a_pause_not_an_error(self) -> None:
        os.environ.update(SPAPI_CLIENT_ID="a", SPAPI_CLIENT_SECRET="b",
                          SPAPI_REFRESH_TOKEN="c", SPAPI_MARKETPLACE_ID="d")
        with patch.object(catalog_sync, "fetch_amazon_catalog",
                          side_effect=catalog_sync.UpstreamPause("did not finish in time")):
            self.assertEqual(catalog_sync.main(["--only-amazon"]), catalog_sync.EXIT_PAUSED)
        self.assertEqual(self._last_log()[0], "degraded")

    def test_targeted_resync_reports_ids_that_did_not_come_back(self) -> None:
        self._shopify_env()
        with patch.object(catalog_sync, "iter_shopify_catalog",
                          return_value=iter([([_row()], [])])):
            code = catalog_sync.main(["--only-shopify", "--product-ids",
                                      "1000000000001,1000000000009"])
        self.assertEqual(code, catalog_sync.EXIT_PAUSED)
        status, msg = self._last_log()
        self.assertEqual(status, "degraded")
        self.assertIn("1/2 product ids returned", msg)
        self.assertIn("1000000000009", msg)

    def test_product_ids_requires_only_shopify(self) -> None:
        with self.assertRaises(SystemExit):
            catalog_sync.main(["--product-ids", "1000000000001"])
        with self.assertRaises(SystemExit):
            catalog_sync.main(["--only-shopify", "--only-tiktok", "--product-ids", "1"])
        self._shopify_env()
        with self.assertRaises(SystemExit):
            catalog_sync.main(["--only-shopify", "--days", "3", "--product-ids", "1"])

    def test_explicit_channel_without_credentials_exits_naming_the_vars(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            catalog_sync.main(["--only-tiktok"])
        self.assertIn("TIKTOK_APP_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
