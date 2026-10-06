"""Hermetic tests for meta_funnel.classify_targeting (no network)."""
from __future__ import annotations

import unittest
from unittest import mock

from meta_funnel import classify_targeting


def aud(*names):
    return {"custom_audiences": [{"name": n} for n in names]}


class ClassifyTargetingTests(unittest.TestCase):
    def test_no_includes_is_cold_even_with_exclusions(self):
        t = {"excluded_custom_audiences": [{"name": "Website Visitors 180"}]}
        self.assertEqual(classify_targeting(t), "cold")

    def test_stacked_site_and_engagers_is_retargeting(self):
        self.assertEqual(classify_targeting(aud("Website Visitors 180", "IG Engagers 365")),
                         "retargeting")

    def test_pdp_and_video_viewers_is_warm_product(self):
        self.assertEqual(classify_targeting(aud("Red Boots PDP Visitors")), "warm_product")
        self.assertEqual(classify_targeting(aud("Red Boots Video Viewers 25%")), "warm_product")

    def test_customer_list_is_retention(self):
        self.assertEqual(classify_targeting(aud("Existing Customers Plus")), "retention")

    def test_retention_outranks_retargeting(self):
        self.assertEqual(classify_targeting(aud("Website Visitors", "Customers")), "retention")

    def test_unrecognised_included_audience_is_unknown_not_guessed(self):
        self.assertEqual(classify_targeting(aud("Lookalike 1% Purchasers")), "unknown")

    def test_missing_targeting_is_unknown(self):
        self.assertEqual(classify_targeting(None), "unknown")

    def test_env_override_changes_the_pattern(self):
        with mock.patch.dict("os.environ", {"META_FUNNEL_RETENTION_PATTERN": "vip"}):
            self.assertEqual(classify_targeting(aud("VIP list")), "retention")
            self.assertEqual(classify_targeting(aud("Existing Customers")), "unknown")


if __name__ == "__main__":
    unittest.main()
