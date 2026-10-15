-- ============================================================
--  Warehouse schema.
--
--  One normalized "fact" table for daily ad performance across every
--  platform, plus an orders/sales table for commerce platforms. Keeping
--  one shared shape is what makes cross-platform questions like "total
--  spend yesterday across all channels" a single query instead of a join
--  per vendor.
--
--  Migrations apply automatically in warehouse/db.py init_db(), so adding
--  a column here is safe on an existing database.
-- ============================================================

-- Daily performance metrics, one row per platform/account/campaign/day.
CREATE TABLE IF NOT EXISTS ad_metrics (
    platform      TEXT    NOT NULL,   -- 'google', 'meta', 'amazon', 'tiktok'
    account_id    TEXT    NOT NULL,
    campaign_id   TEXT,
    campaign_name TEXT,
    date          TEXT    NOT NULL,    -- ISO date 'YYYY-MM-DD'
    impressions   INTEGER DEFAULT 0,
    clicks        INTEGER DEFAULT 0,
    spend         REAL    DEFAULT 0,   -- in account currency
    conversions   REAL    DEFAULT 0,
    revenue       REAL    DEFAULT 0,   -- conversion value / sales attributed
    currency      TEXT,
    synced_at     TEXT    NOT NULL,    -- when this row was last written
    -- Extended columns: each platform fills what it offers, NULL elsewhere.
    campaign_type TEXT,                -- google: channel type (SEARCH/PMAX/...); meta: objective
    reach         INTEGER,             -- meta only
    link_clicks   INTEGER,             -- meta inline link clicks
    add_to_carts  REAL,                -- meta pixel add-to-cart count
    checkouts     REAL,                -- meta pixel initiate-checkout count
    all_conversions REAL,              -- google all_conversions (incl. cross-device etc.)
    search_impression_share REAL,      -- google search/shopping only; RATIO - average it, never SUM
    PRIMARY KEY (platform, account_id, campaign_id, date)
);

CREATE INDEX IF NOT EXISTS idx_ad_metrics_date     ON ad_metrics(date);
CREATE INDEX IF NOT EXISTS idx_ad_metrics_platform ON ad_metrics(platform);

-- Commerce orders, at LINE-ITEM grain (one row per order per sku).
--
-- Shared by every commerce connector, which is why the primary key includes
-- `platform`: order ids are only unique within a platform. Note the grain -
-- summing `total` gives line revenue, which excludes shipping and tax, so it
-- will not tie exactly to a platform's own "total sales" figure.
CREATE TABLE IF NOT EXISTS orders (
    platform     TEXT NOT NULL,
    order_id     TEXT NOT NULL,
    order_date   TEXT NOT NULL,        -- ISO date
    status       TEXT,
    sku          TEXT,
    product_name TEXT,
    quantity     INTEGER DEFAULT 0,
    total        REAL    DEFAULT 0,
    currency     TEXT,
    synced_at    TEXT NOT NULL,
    PRIMARY KEY (platform, order_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_orders_date     ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_orders_platform ON orders(platform);

-- Per-line promo discount attribution for Shopify orders.
--
-- Companion to `orders`, at the same grain: `orders.total` says what was
-- charged, this says WHICH discount(s) account for the gap from list price.
-- Amounts are summed when one discount lands on several line items of a sku.
--
-- Worth knowing before you treat this as promo spend: not every row is
-- marketing. Store-credit and loyalty redemptions, and channel-funded
-- discounts, arrive through the same field. Filter on kind/code first.
--
-- An absent row means "no allocation on that line", NOT "not synced yet" -
-- how much of a merchant's discounting shows up here depends entirely on
-- whether they discount via codes or by lowering variant prices. Codes are
-- visible on the order; price changes are not.
CREATE TABLE IF NOT EXISTS shopify_order_discounts (
    order_id          TEXT NOT NULL,
    sku               TEXT NOT NULL,
    order_date        TEXT,           -- denormalized so time filters need no join
    kind              TEXT NOT NULL,  -- code | automatic | manual | script | other
    code              TEXT NOT NULL,  -- the discount code, else its title
    amount            REAL,           -- allocated to this order+sku
    allocation_method TEXT,           -- EACH | ACROSS
    target_selection  TEXT,           -- ENTITLED | ALL | EXPLICIT
    synced_at         TEXT NOT NULL,
    PRIMARY KEY (order_id, sku, kind, code)
);
CREATE INDEX IF NOT EXISTS idx_shopify_disc_date ON shopify_order_discounts(order_date);
CREATE INDEX IF NOT EXISTS idx_shopify_disc_code ON shopify_order_discounts(code);

-- The Shopify customer id per order: the join key that makes customer-grain
-- analysis (LTV, repeat rate, cohorts, retention) possible at all. Without it
-- `orders` has no customer column, so a customer dimension cannot be joined to
-- a single dollar of revenue.
--
-- Deliberately a SIDE TABLE rather than a column on `orders`, because:
--   (a) `orders` is shared by every platform and this is Shopify-only;
--   (b) `orders` is LINE grain while a customer is ORDER grain, so a column
--       would multiply the storage to say the same thing;
--   (c) ALTER TABLE appends a column at the table's PHYSICAL end, which breaks
--       any positional `INSERT ... VALUES (?, ...)` elsewhere in the codebase.
--
-- NO PII BY DESIGN: the pseudonymous customer id and nothing else - no email,
-- name, phone or address, in the query or the schema. Analytics needs none of
-- them, and not storing them keeps this database out of scope for Shopify's
-- protected-customer-data obligations rather than relying on the MCP server's
-- per-column denylist to keep them from leaving the machine.
--
-- POPULATED ONLY WHEN the app holds the read_customers scope. Until then the
-- connector omits the field entirely - requesting it unscoped returns
-- ACCESS_DENIED, which would fail the whole nightly order sync - and this table
-- simply stays empty. Its emptiness means "scope not granted", never "no
-- customers".
CREATE TABLE IF NOT EXISTS shopify_order_customers (
    order_id    TEXT PRIMARY KEY,  -- orders.order_id at platform='shopify'
    customer_id TEXT,              -- numeric Shopify customer id; NULL = guest checkout
    order_date  TEXT,              -- denormalized so cohort filters need no join
    synced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shopify_ordcust_cust ON shopify_order_customers(customer_id);
CREATE INDEX IF NOT EXISTS idx_shopify_ordcust_date ON shopify_order_customers(order_date);

-- Shopify ORDER TAGS, one row per order+tag. Tags are the merchant's own order
-- labels (gift / influencer / wholesale / test / resend / claim ...), often the
-- only way to identify orders that should be segmented or excluded from revenue.
-- A SIDE TABLE for the same reasons as shopify_order_customers above
-- (Shopify-only, ORDER grain not line grain, and no ALTER TABLE on `orders`).
-- `tag` is COLLATE NOCASE because Shopify treats tags case-insensitively:
-- WHERE tag = 'gift' also matches 'Gift'.
-- REPLACE SEMANTICS: every fetch of an order rewrites its whole tag set, so a
-- tag removed in Shopify disappears here on the next fetch. BUT an order is only
-- re-fetched while its created_at is inside a sync window (e.g. --days 3), so a
-- tag added or removed AFTER that is not seen until a re-pull. Tags that ops add
-- late (claims, resends, stock-outs) need a wider window or a re-pull; tags set
-- when the order is created are always captured.
-- Tags are merchant-entered labels, not customer data. No customAttributes /
-- note attributes are stored: they are app plumbing plus visitor and session
-- ids, which the no-PII rule keeps out.
CREATE TABLE IF NOT EXISTS shopify_order_tags (
    order_id   TEXT NOT NULL,                -- orders.order_id (the order name) at platform='shopify'
    tag        TEXT NOT NULL COLLATE NOCASE, -- as entered in Shopify, whitespace-trimmed
    order_date TEXT,                         -- denormalized so time filters need no join
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (order_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_shopify_ordtag_tag ON shopify_order_tags(tag);
CREATE INDEX IF NOT EXISTS idx_shopify_ordtag_date ON shopify_order_tags(order_date);

-- Which orders have had their tags READ, including the ones that had none.
-- An untagged order has no shopify_order_tags rows, so without this table "no
-- rows" would mean both "looked, untagged" and "never looked" (a measured zero
-- is not a blank). A row here with tag_count = 0 is a measured zero. NO row
-- means the tags are UNKNOWN: the order was last synced before tag capture was
-- deployed and has not been re-pulled since. So an exclusion such as "drop
-- orders tagged test" is only complete for orders that HAVE a coverage row.
-- Derive that floor from this table; don't hardcode a date.
CREATE TABLE IF NOT EXISTS shopify_order_tag_coverage (
    order_id   TEXT PRIMARY KEY,  -- orders.order_id at platform='shopify'
    order_date TEXT,
    tag_count  INTEGER NOT NULL,  -- tags read on the last fetch; 0 = looked, none
    synced_at  TEXT NOT NULL      -- when that fetch happened
);
CREATE INDEX IF NOT EXISTS idx_shopify_ordtagcov_date ON shopify_order_tag_coverage(order_date);

-- Bookkeeping: track each sync run so you can see when data last refreshed.
-- The MCP server exposes this as last_sync_status(), which is usually the
-- fastest way to answer "why does yesterday look empty".
CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rows_written INTEGER DEFAULT 0,
    status     TEXT,                   -- 'ok' or 'error'
    message    TEXT
);
