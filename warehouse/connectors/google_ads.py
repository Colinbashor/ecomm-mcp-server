"""
Google Ads connector. Uses the official google-ads library and a GAQL query
to pull daily campaign metrics.

Docs: https://developers.google.com/google-ads/api/docs/reporting/overview
"""
from __future__ import annotations

import os

PLATFORM = "google"


def _client():
    # Imported lazily so the rest of the project runs even if google-ads
    # isn't installed yet.
    from google.ads.googleads.client import GoogleAdsClient

    return GoogleAdsClient.load_from_dict(
        {
            "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
            "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            "use_proto_plus": True,
        }
    )


def sync(start_date: str, end_date: str) -> list[dict]:
    client = _client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"]

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.advertising_channel_type,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.all_conversions,
            metrics.search_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_rank_lost_impression_share,
            metrics.search_click_share,
            metrics.absolute_top_impression_percentage,
            metrics.top_impression_percentage
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """

    rows: list[dict] = []
    stream = ga_service.search_stream(customer_id=customer_id, query=query)
    for batch in stream:
        for r in batch.results:
            rows.append(
                {
                    "platform": PLATFORM,
                    "account_id": customer_id,
                    "campaign_id": str(r.campaign.id),
                    "campaign_name": r.campaign.name,
                    "date": r.segments.date,
                    "impressions": int(r.metrics.impressions),
                    "clicks": int(r.metrics.clicks),
                    "spend": r.metrics.cost_micros / 1_000_000,  # micros -> currency
                    "conversions": float(r.metrics.conversions),
                    "revenue": float(r.metrics.conversions_value),
                    "currency": None,
                    "campaign_type": r.campaign.advertising_channel_type.name,
                    "all_conversions": float(r.metrics.all_conversions),
                    # ratio metric; 0 means "not applicable" (non-search campaigns)
                    "search_impression_share": float(r.metrics.search_impression_share) or None,
                    # Auction diagnostics — ALL RATIOS (0-1), so AVG never SUM.
                    # WHY lost-IS matters: impression share says we're missing
                    # impressions; only the budget/rank split says whether the fix
                    # is money or bids/quality. They sum with IS to ~1.0.
                    # Google buckets low IS as "<10%", so a stored 0.1 is a FLOOR,
                    # and it buckets high values as "> 90%" likewise — treat the
                    # extremes as bounds, not point estimates.
                    # 0 -> None throughout: Google returns 0 both for "truly zero"
                    # and "not applicable to this campaign type" (PMax/Display have
                    # no search auction), and conflating those with a real 0% would
                    # drag every AVG down. Non-applicable must stay NULL.
                    "search_budget_lost_impression_share":
                        float(r.metrics.search_budget_lost_impression_share) or None,
                    "search_rank_lost_impression_share":
                        float(r.metrics.search_rank_lost_impression_share) or None,
                    "search_click_share": float(r.metrics.search_click_share) or None,
                    "absolute_top_impression_pct":
                        float(r.metrics.absolute_top_impression_percentage) or None,
                    "top_impression_pct":
                        float(r.metrics.top_impression_percentage) or None,
                }
            )
    return rows


def _svc():
    """Shared (service, customer_id) for the attribute/structure fetchers."""
    client = _client()
    return client.get_service("GoogleAdsService"), os.environ["GOOGLE_ADS_CUSTOMER_ID"]


def _reasons(repeated) -> str | None:
    """Repeated enum -> comma-joined names. Read by humans, never joined on."""
    return ",".join(x.name for x in repeated) or None


# ---------------------------------------------------------------------------
# STRUCTURE / SETTINGS fetchers (current state, not date-keyed).
# Consumed by google_ads_structure_sync.py — see that module's docstring for why
# these exist. FIELD SETS ARE PROBED, NOT GUESSED (v24, 2026-08-05):
# `campaign.start_date` / `campaign.end_date` do NOT exist in v24, so don't
# re-add them from an older sample.
# ---------------------------------------------------------------------------
def fetch_campaign_settings() -> list[dict]:
    """Campaign config: status + WHY, bid strategy + its target, budget.

    The bid target lives on a different field per strategy, so all four are
    selected and coalesced — a tROAS campaign leaves the tCPA fields at 0 and
    vice versa, and 0 is stored as NULL so it can't be read as "target of zero".
    """
    svc, cust = _svc()
    query = """
        SELECT
            campaign.id, campaign.name, campaign.status, campaign.serving_status,
            campaign.primary_status, campaign.primary_status_reasons,
            campaign.advertising_channel_type, campaign.advertising_channel_sub_type,
            campaign.bidding_strategy_type,
            campaign.maximize_conversion_value.target_roas,
            campaign.target_roas.target_roas,
            campaign.target_cpa.target_cpa_micros,
            campaign.maximize_conversions.target_cpa_micros,
            campaign_budget.amount_micros,
            campaign_budget.delivery_method,
            campaign_budget.explicitly_shared
        FROM campaign
        WHERE campaign.status != 'REMOVED'
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            c = r.campaign
            troas = c.maximize_conversion_value.target_roas or c.target_roas.target_roas
            tcpa_micros = (c.target_cpa.target_cpa_micros
                           or c.maximize_conversions.target_cpa_micros)
            rows.append({
                "account_id": cust,
                "campaign_id": str(c.id),
                "campaign_name": c.name,
                "status": c.status.name,
                "serving_status": c.serving_status.name,
                "primary_status": c.primary_status.name,
                "primary_status_reasons": _reasons(c.primary_status_reasons),
                "channel_type": c.advertising_channel_type.name,
                "channel_sub_type": c.advertising_channel_sub_type.name,
                "bidding_strategy": c.bidding_strategy_type.name,
                "target_roas": float(troas) or None,
                "target_cpa": (tcpa_micros / 1_000_000) or None,
                "budget_amount": (r.campaign_budget.amount_micros / 1_000_000) or None,
                "budget_delivery": r.campaign_budget.delivery_method.name,
                "budget_shared": int(bool(r.campaign_budget.explicitly_shared)),
            })
    return rows


def fetch_asset_groups() -> list[dict]:
    """PMax asset groups. primary_status + REASONS is the "why isn't this
    serving" field; ad_strength PENDING means Google hasn't scored it yet."""
    svc, cust = _svc()
    query = """
        SELECT campaign.id, asset_group.id, asset_group.name, asset_group.status,
               asset_group.primary_status, asset_group.primary_status_reasons,
               asset_group.ad_strength
        FROM asset_group
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            g = r.asset_group
            rows.append({
                "account_id": cust,
                "campaign_id": str(r.campaign.id),
                "asset_group_id": str(g.id),
                "asset_group_name": g.name,
                "status": g.status.name,
                "primary_status": g.primary_status.name,
                "primary_status_reasons": _reasons(g.primary_status_reasons),
                "ad_strength": g.ad_strength.name,
            })
    return rows


def fetch_asset_group_assets() -> list[dict]:
    """Asset COUNTS per asset_group x field_type x status.

    Aggregated here rather than in SQL because the only question this answers is
    whether an asset group clears PMax's per-type minimums (business name, 1:1
    logo, >=1 landscape + >=1 square image, 3+ headlines, 2+ descriptions), and
    storing every individual asset daily would be ~830k rows/yr to answer it.
    A field_type with every asset REMOVED still yields a row (n_assets under
    status=REMOVED), which is what makes "zero ENABLED logos" visible rather
    than just absent.
    """
    svc, cust = _svc()
    query = """
        SELECT campaign.id, asset_group.id,
               asset_group_asset.field_type, asset_group_asset.status
        FROM asset_group_asset
    """
    counts: dict[tuple, int] = {}
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            key = (str(r.campaign.id), str(r.asset_group.id),
                   r.asset_group_asset.field_type.name,
                   r.asset_group_asset.status.name)
            counts[key] = counts.get(key, 0) + 1
    return [{"account_id": cust, "campaign_id": c, "asset_group_id": g,
             "field_type": f, "status": s, "n_assets": n}
            for (c, g, f, s), n in counts.items()]


# Listing-group case dimensions, in the order they're checked. Each node splits
# on exactly one; whichever is populated names the dimension.
_CASE_DIMENSIONS = (
    ("product_brand", lambda cv: cv.product_brand.value),
    ("product_item_id", lambda cv: cv.product_item_id.value),
    ("product_type", lambda cv: cv.product_type.value),
    ("product_custom_attribute", lambda cv: cv.product_custom_attribute.value),
)


def fetch_asset_group_listing_filters() -> list[dict]:
    """The PMax listing-group tree — what product segment each asset group points
    at. A UNIT_INCLUDED node whose value matches nothing in the Merchant Center
    feed is a silent zero-delivery campaign: impressions collapse to zero with
    no error surfaced anywhere. Google matches these values EXACTLY, so a feed
    that spells a brand differently - punctuation and apostrophes count - will
    never match.
    """
    svc, cust = _svc()
    query = """
        SELECT campaign.id, asset_group.id,
               asset_group_listing_group_filter.id,
               asset_group_listing_group_filter.type,
               asset_group_listing_group_filter.parent_listing_group_filter,
               asset_group_listing_group_filter.case_value.product_brand.value,
               asset_group_listing_group_filter.case_value.product_item_id.value,
               asset_group_listing_group_filter.case_value.product_type.value,
               asset_group_listing_group_filter.case_value.product_custom_attribute.value
        FROM asset_group_listing_group_filter
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            f = r.asset_group_listing_group_filter
            dimension = value = None
            for name, getter in _CASE_DIMENSIONS:
                v = getter(f.case_value)
                if v:
                    dimension, value = name, v
                    break
            # parent is a resource name ".../assetGroupListingGroupFilters/<ag>~<id>"
            parent = str(f.parent_listing_group_filter or "")
            rows.append({
                "account_id": cust,
                "campaign_id": str(r.campaign.id),
                "asset_group_id": str(r.asset_group.id),
                "filter_id": str(f.id),
                "parent_filter_id": parent.split("~")[-1] or None,
                "filter_type": f.type_.name,
                "case_dimension": dimension,
                "case_value": value,
            })
    return rows


def fetch_conversion_actions() -> list[dict]:
    """Every conversion action + primary_for_goal.

    Snapshotted so a change to what the Conversions column counts is DETECTABLE.
    Note that ENABLED actions commonly carry primary_for_goal=true,
    including PAGE_VIEW / ENGAGEMENT / STORE_VISIT ones, yet the Conversions
    column measures purchases only — that depends on account-level GOAL config,
    not on this flag, so it can change with no error surfacing anywhere.
    """
    svc, cust = _svc()
    query = """
        SELECT conversion_action.id, conversion_action.name, conversion_action.status,
               conversion_action.category, conversion_action.type,
               conversion_action.primary_for_goal,
               conversion_action.attribution_model_settings.attribution_model
        FROM conversion_action
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            a = r.conversion_action
            rows.append({
                "account_id": cust,
                "conversion_action_id": str(a.id),
                "name": a.name,
                "status": a.status.name,
                "category": a.category.name,
                "action_type": a.type_.name,
                "primary_for_goal": int(bool(a.primary_for_goal)),
                "attribution_model":
                    a.attribution_model_settings.attribution_model.name,
            })
    return rows


def sync_search_terms(start_date: str, end_date: str) -> list[dict]:
    """Daily search-term report (search_term_view) — customer queries that
    triggered our ads, with spend/conv. Powers negative-keyword + waste audit."""
    client = _client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"]

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            search_term_view.search_term,
            search_term_view.status,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    rows: list[dict] = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for r in batch.results:
            rows.append({
                "account_id": customer_id,
                "date": r.segments.date,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "ad_group_id": str(r.ad_group.id),
                "ad_group_name": r.ad_group.name,
                "search_term": r.search_term_view.search_term,
                "status": r.search_term_view.status.name,
                "impressions": int(r.metrics.impressions),
                "clicks": int(r.metrics.clicks),
                "spend": r.metrics.cost_micros / 1_000_000,
                "conversions": float(r.metrics.conversions),
                "revenue": float(r.metrics.conversions_value),
            })
    return rows


def sync_shopping_products(start_date: str, end_date: str) -> list[dict]:
    """Daily Shopping product performance (shopping_performance_view) keyed by
    Merchant Center product item id. Carries the product title, so reorder /
    momentum reporting names products without a cross-channel id join. Ad-driven
    demand only (Shopping + PMax-shopping surfaces), not total site sales."""
    client = _client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"]

    query = f"""
        SELECT
            segments.product_item_id,
            segments.product_title,
            campaign.id,
            campaign.name,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM shopping_performance_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    rows: list[dict] = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for r in batch.results:
            rows.append({
                "account_id": customer_id,
                "date": r.segments.date,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "product_item_id": r.segments.product_item_id,
                "product_title": r.segments.product_title,
                "impressions": int(r.metrics.impressions),
                "clicks": int(r.metrics.clicks),
                "spend": r.metrics.cost_micros / 1_000_000,
                "conversions": float(r.metrics.conversions),
                "revenue": float(r.metrics.conversions_value),
            })
    return rows


def sync_paid_organic(start_date: str, end_date: str) -> list[dict]:
    """paid_organic_search_term_view — the SAME query's paid clicks next to its
    ORGANIC clicks. The only Google surface that measures cannibalisation
    directly: how much traffic a query already delivers for free before we pay.

    !! THIS VIEW REJECTS ALL MONEY METRICS !! Probed 2026-08-05: cost_micros,
    conversions and conversions_value all error with "metric is incompatible
    with the resource in the FROM clause". Clicks/impressions only — so cost per
    query has to come from google_search_terms, joined on the term.
    """
    svc, cust = _svc()
    query = f"""
        SELECT
            campaign.id, campaign.name, ad_group.id,
            paid_organic_search_term_view.search_term,
            segments.date,
            metrics.impressions, metrics.clicks,
            metrics.organic_impressions, metrics.organic_clicks,
            metrics.organic_impressions_per_query, metrics.organic_clicks_per_query
        FROM paid_organic_search_term_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            rows.append({
                "account_id": cust,
                "date": r.segments.date,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "ad_group_id": str(r.ad_group.id),
                "search_term": r.paid_organic_search_term_view.search_term,
                "paid_impressions": int(r.metrics.impressions),
                "paid_clicks": int(r.metrics.clicks),
                "organic_impressions": int(r.metrics.organic_impressions),
                "organic_clicks": int(r.metrics.organic_clicks),
                "organic_impressions_per_query": float(r.metrics.organic_impressions_per_query),
                "organic_clicks_per_query": float(r.metrics.organic_clicks_per_query),
            })
    return rows


def sync_conversion_action_daily(start_date: str, end_date: str) -> list[dict]:
    """campaign x date x conversion_action_name.

    This is what makes the Conversions column auditable. `conversions` is the
    number every ROAS in the project divides by, and which ACTIONS feed it is an
    account-level goal setting that can change with no error anywhere. Splitting
    by action name makes a change visible the next day.
    Also explains all_conversions: on a single campaign-day, page-view style
    actions can post hundreds of all_conversions while conversions stays 0 -
    all_conversions is dominated by page-view micro-actions and is NOT a
    business metric.
    """
    svc, cust = _svc()
    query = f"""
        SELECT
            campaign.id, campaign.name, segments.date,
            segments.conversion_action_name, segments.conversion_action_category,
            metrics.conversions, metrics.conversions_value, metrics.all_conversions,
            metrics.all_conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            rows.append({
                "account_id": cust,
                "date": r.segments.date,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "conversion_action": r.segments.conversion_action_name,
                "conversion_category": r.segments.conversion_action_category.name,
                "conversions": float(r.metrics.conversions),
                "conversions_value": float(r.metrics.conversions_value),
                "all_conversions": float(r.metrics.all_conversions),
                "all_conversions_value": float(r.metrics.all_conversions_value),
            })
    return rows


def sync_campaign_devices(start_date: str, end_date: str) -> list[dict]:
    """campaign x date x device. Mobile/desktop/tablet split was invisible, so a
    device-specific conversion-rate collapse looked like a general decline."""
    svc, cust = _svc()
    query = f"""
        SELECT
            campaign.id, campaign.name, campaign.advertising_channel_type,
            segments.date, segments.device,
            metrics.impressions, metrics.clicks, metrics.cost_micros,
            metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    rows: list[dict] = []
    for batch in svc.search_stream(customer_id=cust, query=query):
        for r in batch.results:
            rows.append({
                "account_id": cust,
                "date": r.segments.date,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "campaign_type": r.campaign.advertising_channel_type.name,
                "device": r.segments.device.name,
                "impressions": int(r.metrics.impressions),
                "clicks": int(r.metrics.clicks),
                "spend": r.metrics.cost_micros / 1_000_000,
                "conversions": float(r.metrics.conversions),
                "revenue": float(r.metrics.conversions_value),
            })
    return rows


def sync_pmax_search_themes(start_date: str, end_date: str) -> list[dict]:
    """campaign_search_term_insight — the ONLY query-level visibility PMax has.

    search_term_view covers no PMax traffic at all (verified: 0 rows against
    a live PMax spend), so this closes a total blind spot. Google
    returns clustered SEARCH THEMES (`category_label`), not raw queries — e.g.
    'running shoes for flat feet', 'waterproof hiking boots' - which is as granular as
    PMax gets.

    TWO API CONSTRAINTS, both probed 2026-08-05:
      - the resource REQUIRES an explicit `campaign_search_term_insight.campaign_id`
        filter; a customer-wide query errors. So it is ONE REQUEST PER CAMPAIGN,
        and the PMax campaign list has to be resolved first.
      - it is WINDOW-AGGREGATED, not date-segmented, so rows are stamped with the
        window rather than a date. Never SUM two overlapping windows.
    """
    svc, cust = _svc()
    pmax = [str(r.campaign.id) for batch in svc.search_stream(customer_id=cust, query="""
                SELECT campaign.id FROM campaign
                WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
                  AND campaign.status = 'ENABLED'""")
            for r in batch.results]
    rows: list[dict] = []
    for cid in pmax:
        q = f"""
            SELECT campaign_search_term_insight.id,
                   campaign_search_term_insight.category_label,
                   campaign_search_term_insight.campaign_id,
                   metrics.impressions, metrics.clicks,
                   metrics.conversions, metrics.conversions_value
            FROM campaign_search_term_insight
            WHERE campaign_search_term_insight.campaign_id = {cid}
              AND segments.date BETWEEN '{start_date}' AND '{end_date}'
        """
        for batch in svc.search_stream(customer_id=cust, query=q):
            for r in batch.results:
                i = r.campaign_search_term_insight
                # id 0 / blank label is Google's "everything else" bucket
                rows.append({
                    "account_id": cust,
                    "window_start": start_date,
                    "window_end": end_date,
                    "campaign_id": cid,
                    "insight_id": str(i.id),
                    "search_theme": i.category_label or "(other)",
                    "impressions": int(r.metrics.impressions),
                    "clicks": int(r.metrics.clicks),
                    "conversions": float(r.metrics.conversions),
                    "revenue": float(r.metrics.conversions_value),
                })
    return rows


def sync_keywords(start_date: str, end_date: str) -> list[dict]:
    """Daily keyword report (keyword_view) with Quality Score. QS is a
    point-in-time attribute (current value stamped on each date row); 0 = not
    available, stored as NULL."""
    client = _client()
    ga_service = client.get_service("GoogleAdsService")
    customer_id = os.environ["GOOGLE_ADS_CUSTOMER_ID"]

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            ad_group_criterion.criterion_id,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.status,
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM keyword_view
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
          AND ad_group_criterion.status != 'REMOVED'
    """
    rows: list[dict] = []
    for batch in ga_service.search_stream(customer_id=customer_id, query=query):
        for r in batch.results:
            qs = int(r.ad_group_criterion.quality_info.quality_score)
            rows.append({
                "account_id": customer_id,
                "date": r.segments.date,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "ad_group_id": str(r.ad_group.id),
                "ad_group_name": r.ad_group.name,
                "criterion_id": str(r.ad_group_criterion.criterion_id),
                "keyword": r.ad_group_criterion.keyword.text,
                "match_type": r.ad_group_criterion.keyword.match_type.name,
                "quality_score": qs or None,  # 0 => not available
                "status": r.ad_group_criterion.status.name,
                "impressions": int(r.metrics.impressions),
                "clicks": int(r.metrics.clicks),
                "spend": r.metrics.cost_micros / 1_000_000,
                "conversions": float(r.metrics.conversions),
                "revenue": float(r.metrics.conversions_value),
            })
    return rows
