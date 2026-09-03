"""
warehouse.connectors.google_ads._impression_share_fields() hermetic tests.

Pins the fix for a bug where a blanket `float(x) or None` treated any zero as
"not applicable," silently discarding real 0.0 impression-share days on
Search/Shopping campaigns (which DO run a search auction) rather than just the
meaningless 0 on Performance Max/Display (which don't). A second, distinct
defect is also guarded here: some accounts' Google Ads API responses can
return a hard 0.0/0.0/0.0 triple as a "not computed yet" placeholder on a day
the campaign genuinely served impressions — detectable because
impression_share + budget_lost + rank_lost sums to ~1.0 on every real day, and
a campaign that served impressions cannot, by definition, have won a real 0%
of them.
"""
from __future__ import annotations

from types import SimpleNamespace

from warehouse.connectors.google_ads import _impression_share_fields


def _row(channel_type, impressions, impr_share, budget_lost, rank_lost, click_share=0.0):
    return SimpleNamespace(
        campaign=SimpleNamespace(
            advertising_channel_type=SimpleNamespace(name=channel_type)
        ),
        metrics=SimpleNamespace(
            impressions=impressions,
            search_impression_share=impr_share,
            search_budget_lost_impression_share=budget_lost,
            search_rank_lost_impression_share=rank_lost,
            search_click_share=click_share,
        ),
    )


def test_non_search_auction_channel_type_is_always_none():
    # Performance Max has no search auction at all — every value must be
    # None regardless of what Google put in the metric slots.
    r = _row("PERFORMANCE_MAX", 500_000, 0.42, 0.10, 0.48)
    out = _impression_share_fields(r)
    assert out == {
        "search_impression_share": None,
        "search_budget_lost_impression_share": None,
        "search_rank_lost_impression_share": None,
        "search_click_share": None,
    }


def test_real_nonzero_impression_share_is_kept():
    r = _row("SHOPPING", 197_055, 0.9337712096332786, 0.0, 0.0662287903667214)
    out = _impression_share_fields(r)
    assert out["search_impression_share"] == 0.9337712096332786
    assert out["search_budget_lost_impression_share"] == 0.0
    assert out["search_rank_lost_impression_share"] == 0.0662287903667214


def test_real_low_impression_share_with_real_budget_lost_is_kept():
    # A genuinely real day: IS is low but budget_lost + rank_lost still sum
    # to ~1.0 with it — not all three are zero, so this is not the placeholder.
    r = _row("SHOPPING", 939, 0.0999, 0.9001, 0.006557928367243989)
    out = _impression_share_fields(r)
    assert out["search_impression_share"] == 0.0999
    assert out["search_budget_lost_impression_share"] == 0.9001


def test_search_campaign_real_zero_budget_lost_is_kept_not_nulled():
    # The ORIGINAL bug: IS=0.89 is truthy and survived the old `or None`, but
    # budget_lost=0.0 is falsy and was silently nulled even though it's a
    # real, meaningful "we didn't lose any share to budget" result.
    r = _row("SEARCH", 5077, 0.8905, 0.0, 0.1095)
    out = _impression_share_fields(r)
    assert out["search_budget_lost_impression_share"] == 0.0
    assert out["search_impression_share"] == 0.8905


def test_all_zero_triple_with_impressions_is_a_placeholder_not_a_real_zero():
    # A campaign that served 224,580 impressions cannot, by definition, have
    # won a real 0% of the auction. This is exactly the shape of Google's own
    # "not computed yet" sentinel for this metric family.
    r = _row("SHOPPING", 224_580, 0.0, 0.0, 0.0)
    out = _impression_share_fields(r)
    assert out == {
        "search_impression_share": None,
        "search_budget_lost_impression_share": None,
        "search_rank_lost_impression_share": None,
        "search_click_share": None,
    }


def test_all_zero_triple_with_zero_impressions_is_a_genuine_non_event():
    # No impressions at all that day means no auction was entered — a
    # trivial, genuinely-real 0/0/0, not a placeholder. Gate on impressions>0,
    # not on the triple alone, or a campaign with a real dark day would lose
    # its (correct) zero.
    r = _row("SEARCH", 0, 0.0, 0.0, 0.0)
    out = _impression_share_fields(r)
    assert out["search_impression_share"] == 0.0
    assert out["search_budget_lost_impression_share"] == 0.0
    assert out["search_rank_lost_impression_share"] == 0.0


def test_click_share_travels_with_the_same_placeholder_detection():
    r = _row("SEARCH", 5000, 0.0, 0.0, 0.0, click_share=0.0)
    out = _impression_share_fields(r)
    assert out["search_click_share"] is None

    r2 = _row("SEARCH", 5000, 0.30, 0.20, 0.50, click_share=0.15)
    out2 = _impression_share_fields(r2)
    assert out2["search_click_share"] == 0.15
