"""
Shopify customer dimension -> shopify_customers + shopify_customer_metafields
                             + shopify_customer_flag_history

Pulls the CURRENT state of every customer account on your Shopify store via
the Bulk Operations API: account state, created/updated timestamps, marketing
consent STATE (subscribed / not-subscribed — a flag, not contact info), the
raw tag string, and whatever custom metafields your store has attached to
customers. This is the customer DIMENSION (attributes you'd segment by). The
customer-to-ORDER join key (which order belongs to which customer) is a
separate concern that lives in `shopify_order_customers`, written by
warehouse/connectors/shopify.py — both need the same `read_customers` scope,
but that one is what actually unlocks LTV / repeat-purchase / cohort queries
once you join it against `orders`.

NO PII, BY DESIGN — read this before you're tempted to add email/name/phone.
Only the bare customer id and non-contact attributes are requested here; the
GraphQL query below never selects email, name, phone, or any address field,
even though the Admin API would happily return them. This is deliberate, not
an oversight, for two reasons:
  1. Scope discipline. A bare, pseudonymous customer id is enough to answer
     the analytics questions this table exists for (repeat-purchase rate,
     cohort/retention analysis, segmenting by tag or consent state) WITHOUT
     ever identifying a person by the row alone. Adding email/name would
     require joining this table back to something that already knows who
     the id refers to — and if you never store that mapping here, you never
     create a new place where a leak or a bug can expose it.
  2. Compliance blast radius. Shopify treats email/name/phone/address as
     "protected customer data" with its own handling obligations (secure
     storage, deletion-on-request propagation, audit trail, etc). A database
     that never stores those fields in the first place is simply out of
     scope for those obligations — you're not relying on access controls or
     a denylist to keep contact info from leaking downstream, you never had
     it. If you need real identity (to send an email, personalize a message),
     do that in your ESP/CRM, which is already built and audited for it, and
     keep this warehouse table as the anonymous analytics side of the
     relationship. When porting this connector to your own store, do NOT
     "improve" it by adding contact fields back in — that quietly moves your
     whole warehouse into a stricter compliance category.

WHY THE CHANGE-LOG TABLE EXISTS (the interesting generic technique here).
Shopify's API is CURRENT-STATE ONLY: it has no endpoint that returns "what
were this customer's tags/state on 2024-03-01". If you want to answer "when
did this customer's state change" or "how many customers moved from tag X to
tag Y last month", you have to have been RECORDING every change yourself, as
it happens — there is nothing to backfill later. `shopify_customer_flag_history`
solves this generically: `record_flag_changes()` compares each incoming
customer against the last snapshot of that SAME customer already sitting in
`shopify_customers`, and only appends a change-log row when tags or state
actually differ from what was there before. Ordering matters: this diff MUST
run before `store_customers()` overwrites the snapshot, or the "before" side
of every comparison would just be the "after" side of itself. This same
before-you-overwrite-it diff pattern generalizes to ANY current-state-only API
(price history, feature flags, subscription/membership status, inventory
snapshots) — whatever attribute you actually care about, add it to the SELECT
and the comparison tuple in `record_flag_changes()`, following this shape.

BULK OPERATIONS API MECHANICS (the gotchas that will bite you otherwise):
  * Only ONE bulk query op runs per app at a time. If a stale one is already
    running (e.g. left over from a killed process), Shopify returns a
    "bulk query already in progress" userError instead of accepting yours —
    `_submit()` detects that message, waits for the existing op to finish,
    then resubmits automatically rather than failing the run.
  * Bulk queries CANNOT use pagination arguments (`first`/`after`) on ANY
    connection, including nested ones — you query the whole connection and
    Shopify paginates it server-side into one JSONL file. That's why neither
    `customers` nor the nested `metafields` connection below take a `first`.
  * Results come back as JSON Lines, one object per line. A CONNECTION field
    (like `metafields`) is NOT inlined onto its parent's line — each child
    object arrives as its OWN line, carrying `__parentId` pointing at the
    parent's `id`. That means the customer node's `id` field must always be
    selected (nothing else lets you regroup children back to their parent),
    and a naive "read the whole file into one dict" approach works fine for
    small stores but does not scale — `iter_customer_batches()` streams the
    file and only cuts a batch at a root (non-child) line, so a customer can
    never be split from their own metafields across two batches.
  * `currentBulkOperation(type: QUERY)` returns the APP's most recent bulk
    op — not necessarily yours — so always match on the id you got back from
    the submit call before trusting a COMPLETED status.

Usage:
    .venv\\Scripts\\python.exe shopify_customers_sync.py --probe      # scope + metafield-namespace check, no writes
    .venv\\Scripts\\python.exe shopify_customers_sync.py --dry-run    # crawl + parse, print a sample, write nothing
    .venv\\Scripts\\python.exe shopify_customers_sync.py              # full crawl, current snapshot of every customer
    .venv\\Scripts\\python.exe shopify_customers_sync.py --since 2026-01-01   # only customers updated since this date

Requires the same Shopify credentials as warehouse/connectors/shopify.py
(SHOPIFY_SHOP + SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET, or legacy
SHOPIFY_ADMIN_TOKEN) PLUS the `read_customers` Admin API scope on that app,
which the orders connector does not need. Getting that scope onto a
single-store custom app needs no Shopify review: add read_customers to the
app's Admin API scopes in the Dev Dashboard, RELEASE A NEW APP VERSION (scope
edits do nothing until a new version is released), then reinstall the app so
the token picks up the new grant. `--probe` tells you whether the scope has
landed yet without doing anything else.

Docs: https://shopify.dev/docs/api/admin-graphql/latest/queries/customers
      https://shopify.dev/docs/api/usage/bulk-operations/queries
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date

import requests
from dotenv import load_dotenv

from warehouse import db
from warehouse.connectors import shopify

PLATFORM = "shopify_customers"

# Bulk ops take seconds-to-minutes server-side; poll rather than assume.
POLL_SECONDS = 15
POLL_MAX_MIN = 120

# Customers flushed per write batch. Bounds peak memory on a large store: the
# full customer list with a couple of metafields each, accumulated in one
# Python list, can run into the GB range. `iter_customer_batches` only ever
# cuts a batch at a root customer line, so a customer's metafields can never
# land in a different batch than the customer itself.
BATCH_CUSTOMERS = 50_000


# ---------------------------------------------------------------------------
# env / scope checks
# ---------------------------------------------------------------------------

def _check_env() -> None:
    """Fail with a clear, actionable message instead of a bare KeyError/crash
    when the required Shopify credentials are missing."""
    missing = [v for v in ("SHOPIFY_SHOP",) if not os.environ.get(v)]
    have_client_creds = bool(os.environ.get("SHOPIFY_CLIENT_ID")) and bool(
        os.environ.get("SHOPIFY_CLIENT_SECRET"))
    have_static_token = bool(os.environ.get("SHOPIFY_ADMIN_TOKEN"))
    if not have_client_creds and not have_static_token:
        missing.append("SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (or legacy SHOPIFY_ADMIN_TOKEN)")
    if missing:
        raise SystemExit(
            "shopify_customers_sync: missing required configuration: "
            + ", ".join(missing)
            + "\nSet these in .env — see warehouse/connectors/shopify.py's module "
              "docstring for how to obtain them, then re-run.")


def require_scope() -> None:
    if not shopify.customer_capture_enabled():
        raise SystemExit(
            "read_customers is not granted, so this connector cannot run.\n"
            "Fix (no Shopify review needed for a single-store custom app):\n"
            "  1. add read_customers to the app's Admin API scopes\n"
            "  2. RELEASE A NEW APP VERSION (scope edits are inert until you do)\n"
            "  3. reinstall the app so the token picks up the grant\n"
            "Then re-run. Verify with: shopify_customers_sync.py --probe")


# ---------------------------------------------------------------------------
# query building
# ---------------------------------------------------------------------------

def _query_doc(since: str | None = None) -> str:
    """Bulk query doc. No pagination args anywhere — bulk forbids them, which is
    also why `metafields` comes back as its own JSONL line per row rather than
    inline on the customer."""
    filt = f"(query: \"updated_at:>='{since}'\")" if since else ""
    return (
        "{ customers" + filt + " { edges { node { "
        "id state createdAt updatedAt tags numberOfOrders "
        "amountSpent { amount currencyCode } "
        "emailMarketingConsent { marketingState consentUpdatedAt } "
        "smsMarketingConsent { marketingState consentUpdatedAt } "
        "metafields { edges { node { namespace key value type updatedAt } } } "
        "} } } }"
    )


# ---------------------------------------------------------------------------
# parsing (pure — unit-tested against fixtures)
# ---------------------------------------------------------------------------

def parse_customer(node: dict) -> dict:
    """One shopify_customers row from a bulk JSONL customer line. No PII field
    is ever read here — see the module docstring."""
    spent = node.get("amountSpent") or {}
    email = node.get("emailMarketingConsent") or {}
    sms = node.get("smsMarketingConsent") or {}
    tags = node.get("tags")
    # `tags` is a list of strings on the Customer type; store the raw joined
    # form so no assumption is baked in about how many tags matter or which.
    if isinstance(tags, list):
        tags = ", ".join(str(t) for t in tags)
    return {
        "customer_id": shopify.numeric_id(node.get("id")),
        "state": node.get("state"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "tags": tags,
        "email_consent": email.get("marketingState"),
        "email_consent_at": email.get("consentUpdatedAt"),
        "sms_consent": sms.get("marketingState"),
        "sms_consent_at": sms.get("consentUpdatedAt"),
        "number_of_orders": node.get("numberOfOrders"),
        "amount_spent": float(spent["amount"]) if spent.get("amount") is not None else None,
        "currency": spent.get("currencyCode"),
    }


def parse_metafield(node: dict, customer_gid: str) -> dict:
    """One shopify_customer_metafields row. Deliberately UNFILTERED — every
    namespace/key your store uses is captured, not just ones this script
    happens to know about. Figure out which ones matter to your analysis with
    `--probe`, which prints every namespace.key it observes."""
    return {
        "customer_id": shopify.numeric_id(customer_gid),
        "namespace": node.get("namespace") or "",
        "key": node.get("key") or "",
        "value": node.get("value"),
        "type": node.get("type"),
        "updated_at": node.get("updatedAt"),
    }


def iter_customer_batches(url: str, batch_size: int = BATCH_CUSTOMERS):
    """Stream the bulk JSONL, yielding (customers, metafields) in bounded batches.

    `metafields` is a CONNECTION, so each metafield arrives as its own line
    carrying `__parentId` = the customer's gid (a single-object field, by
    contrast, would stay inline on the parent's line). Children immediately
    follow their parent in the stream, so a batch is only ever cut at a ROOT
    customer line — that is what guarantees a customer is never split from
    its own metafields across two batches."""
    customers: list[dict] = []
    metafields: list[dict] = []
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            obj = json.loads(raw)
            parent = obj.get("__parentId")
            gid = str(obj.get("id", ""))
            if parent:
                # a metafield (or any other child) hanging off a customer
                if "namespace" in obj or gid.startswith("gid://shopify/Metafield/"):
                    metafields.append(parse_metafield(obj, parent))
                continue
            if not gid.startswith("gid://shopify/Customer/"):
                continue
            if len(customers) >= batch_size:      # safe cut point: a root line
                yield customers, metafields
                customers, metafields = [], []
            customers.append(parse_customer(obj))
            # defensive: if Shopify ever inlines the connection instead
            inline = (obj.get("metafields") or {})
            for edge in (inline.get("edges") or []) if isinstance(inline, dict) else []:
                node = (edge or {}).get("node") or {}
                if node:
                    metafields.append(parse_metafield(node, gid))
    if customers or metafields:
        yield customers, metafields


def parse_jsonl(url: str) -> tuple[list[dict], list[dict]]:
    """Whole-response convenience wrapper over iter_customer_batches. Fine for
    a bounded --since slice; the full crawl uses the iterator so peak memory
    stays flat on a large store."""
    customers: list[dict] = []
    metafields: list[dict] = []
    for cs, ms in iter_customer_batches(url):
        customers.extend(cs)
        metafields.extend(ms)
    return customers, metafields


# ---------------------------------------------------------------------------
# schema (self-contained — this script owns these tables, not schema.sql)
# ---------------------------------------------------------------------------

def ensure_schema(conn) -> None:
    """Create this connector's tables if they don't exist yet. Safe to call
    repeatedly. Deliberately separate from warehouse/schema.sql / db.init_db()
    so this script stays a self-contained, optional add-on: dropping this one
    file (and not running it) leaves the rest of the warehouse untouched."""
    conn.executescript(
        """
        -- Current snapshot, one row per customer. NO email/name/phone/address
        -- column exists here — see the module docstring for why that's a
        -- deliberate design decision, not an omission to "fix" later.
        CREATE TABLE IF NOT EXISTS shopify_customers (
            customer_id      TEXT PRIMARY KEY,  -- bare numeric Shopify customer id
            state            TEXT,              -- ENABLED / DISABLED / INVITED / DECLINED
            created_at       TEXT,
            updated_at       TEXT,
            tags             TEXT,               -- raw comma-joined tag list
            email_consent    TEXT,               -- SUBSCRIBED / NOT_SUBSCRIBED / ... (a state, not an address)
            email_consent_at TEXT,
            sms_consent      TEXT,
            sms_consent_at   TEXT,
            number_of_orders INTEGER,
            amount_spent     REAL,
            currency         TEXT,
            synced_at        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shopify_customers_updated ON shopify_customers(updated_at);

        -- Flexible namespace+key+value capture of WHATEVER custom metafields
        -- your store has attached to customers (loyalty flags, external ids,
        -- preference fields, ...). Nothing here assumes a specific namespace
        -- or key — run `--probe` to see what your store actually has, then
        -- filter/query on the namespace(s) that matter to you.
        CREATE TABLE IF NOT EXISTS shopify_customer_metafields (
            customer_id TEXT NOT NULL,
            namespace   TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT,
            type        TEXT,
            updated_at  TEXT,
            synced_at   TEXT NOT NULL,
            PRIMARY KEY (customer_id, namespace, key)
        );
        CREATE INDEX IF NOT EXISTS idx_shopify_cust_meta_ns ON shopify_customer_metafields(namespace, key);

        -- Change-log: a row is appended ONLY when a customer's tags or state
        -- actually changed since the last run (see record_flag_changes below).
        -- Shopify keeps no history for either field, so this is the only way
        -- to ever answer "when did this customer's state/tags change" -
        -- and only from the day you started running this script onward.
        CREATE TABLE IF NOT EXISTS shopify_customer_flag_history (
            customer_id   TEXT NOT NULL,
            observed_date TEXT NOT NULL,  -- the run's date, not Shopify's updated_at
            tags          TEXT,
            state         TEXT,
            synced_at     TEXT NOT NULL,
            PRIMARY KEY (customer_id, observed_date)
        );
        CREATE INDEX IF NOT EXISTS idx_shopify_cust_flag_cust ON shopify_customer_flag_history(customer_id);
        """
    )


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------

_CUSTOMER_COLUMNS = ("customer_id", "state", "created_at", "updated_at", "tags",
                     "email_consent", "email_consent_at", "sms_consent",
                     "sms_consent_at", "number_of_orders", "amount_spent",
                     "currency")


def record_flag_changes(conn, customers: list[dict], observed: str) -> int:
    """Append change-log rows for customers whose tags/state MOVED since the
    last snapshot.

    MUST run BEFORE store_customers(): the comparison is against the PREVIOUS
    row in `shopify_customers`, and store_customers() overwrites it. Run this
    after, and every comparison degenerates into "before == after", and no
    change is ever recorded again.

    Generalize this the same way for any other attribute your store cares
    about (e.g. a specific loyalty/membership metafield): fetch its previous
    value the same way, add it to the comparison tuple below, and add it to
    the rows appended — the shape of the diff-before-overwrite pattern does
    not change.
    """
    prev = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT customer_id, tags, state FROM shopify_customers")}
    rows = []
    for c in customers:
        cid = c["customer_id"]
        if not cid:
            continue
        before = prev.get(cid)
        # A customer never seen before is logged too: that's the baseline row
        # the first real change will be measured against.
        if before is None or before[0] != c.get("tags") or before[1] != c.get("state"):
            rows.append({"customer_id": cid, "observed_date": observed,
                         "tags": c.get("tags"), "state": c.get("state")})
    if rows:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO shopify_customer_flag_history "
                "(customer_id, observed_date, tags, state, synced_at) "
                "VALUES (:customer_id, :observed_date, :tags, :state, :synced_at)",
                [{**r, "synced_at": db.now()} for r in rows])
    return len(rows)


def store_customers(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    stamp = db.now()
    cols = ", ".join(_CUSTOMER_COLUMNS) + ", synced_at"
    binds = ", ".join(f":{c}" for c in _CUSTOMER_COLUMNS) + ", :synced_at"
    with conn:
        # Columns NAMED explicitly, never positional — a positional
        # VALUES(...) silently maps onto the wrong columns after a future
        # ALTER TABLE changes the on-disk column order.
        conn.executemany(
            f"INSERT OR REPLACE INTO shopify_customers ({cols}) VALUES ({binds})",
            [{**r, "synced_at": stamp} for r in rows])
    return len(rows)


def store_metafields(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    stamp = db.now()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO shopify_customer_metafields "
            "(customer_id, namespace, key, value, type, updated_at, synced_at) "
            "VALUES (:customer_id, :namespace, :key, :value, :type, :updated_at, :synced_at)",
            [{**r, "synced_at": stamp} for r in rows])
    return len(rows)


# ---------------------------------------------------------------------------
# Bulk Operations API driver
# ---------------------------------------------------------------------------

def _gql(query: str) -> dict:
    """One non-streaming Admin GraphQL call, used for the bulk submit/poll
    mutations. Reuses the Shopify connector's token + throttle/retry
    conventions rather than a second, incompatible transport."""
    shop = os.environ["SHOPIFY_SHOP"]
    url = f"https://{shop}/admin/api/{shopify.API_VERSION}/graphql.json"
    last_err = None
    for attempt in range(20):
        try:
            headers = {"X-Shopify-Access-Token": shopify._access_token(shop),
                       "Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json={"query": query}, timeout=90)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            time.sleep(min(60, 3 * (attempt + 1)))
            continue
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", 2)))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Shopify API {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        errors = payload.get("errors") or []
        if any((e.get("extensions", {}) or {}).get("code") == "THROTTLED" for e in errors):
            time.sleep(2)
            continue
        if errors:
            raise RuntimeError(f"Shopify GraphQL error: {errors[:2]}")
        return payload["data"]
    raise RuntimeError(f"Shopify API failed after retries: {last_err}" if last_err
                       else "Shopify API kept throttling after retries.")


def _current_bulk() -> dict | None:
    """The APP's most recent bulk op — not necessarily ours. Callers must
    match on the id returned at submit time before trusting the status."""
    return _gql("{ currentBulkOperation(type: QUERY) "
                "{ id status errorCode objectCount url } }")["currentBulkOperation"]


def _wait_for(op_id: str) -> dict:
    """Poll currentBulkOperation until it terminates."""
    deadline = POLL_MAX_MIN * 60 / POLL_SECONDS
    tries = 0
    while True:
        time.sleep(POLL_SECONDS)
        cur = _current_bulk()
        if cur and cur.get("id") == op_id:
            st = cur.get("status")
            if st == "COMPLETED":
                return cur
            if st in ("FAILED", "CANCELED"):
                raise RuntimeError(f"Bulk op {st}: {cur.get('errorCode')}")
        tries += 1
        if tries > deadline:
            raise RuntimeError(f"Bulk op {op_id} did not finish within {POLL_MAX_MIN} min")


def _submit(doc: str) -> tuple[str | None, int]:
    """Submit the bulk query and wait for it. If another op is already
    running (a prior crashed run, most likely), drain it first instead of
    failing — bulk allows only one op per app at a time."""
    mutation = ("mutation { bulkOperationRunQuery(query: %s) "
                "{ bulkOperation { id status } userErrors { field message } } }"
                % json.dumps(doc))
    data = _gql(mutation)["bulkOperationRunQuery"]
    errs = data.get("userErrors") or []
    if errs:
        msg = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errs)
        if "already in progress" in msg.lower():
            existing = _current_bulk()
            if existing and existing.get("status") not in ("COMPLETED", "FAILED", "CANCELED"):
                _wait_for(existing["id"])
            return _submit(doc)
        raise RuntimeError(f"bulkOperationRunQuery userErrors: {msg}")
    done = _wait_for(data["bulkOperation"]["id"])
    return done.get("url"), int(done.get("objectCount") or 0)


def _probe() -> None:
    """Report what the app can actually see, without writing anything: scope
    status, plus a small live sample of tags/state/metafield namespaces so you
    can decide which of your store's own metafields matter to your analysis."""
    granted = shopify.customer_capture_enabled()
    print(f"read_customers granted: {granted}")
    if not granted:
        print("  -> nothing else can be probed; see the module docstring for how to grant it.")
        return
    doc = ("{ customers(first: 3) { edges { node { id tags state "
           "metafields(first: 25) { edges { node { namespace key type } } } } } } }")
    data = shopify._post({}, doc)
    seen: set[str] = set()
    for edge in ((data.get("customers") or {}).get("edges") or []):
        node = edge.get("node") or {}
        print(f"  customer {shopify.numeric_id(node.get('id'))} state={node.get('state')} "
              f"tags={node.get('tags')}")
        for me in ((node.get("metafields") or {}).get("edges") or []):
            n = me.get("node") or {}
            seen.add(f"{n.get('namespace')}.{n.get('key')} ({n.get('type')})")
    print("  metafield namespaces observed (decide which matter to your analysis):")
    for s in sorted(seen):
        print(f"    {s}")


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="scope + metafield-namespace reconnaissance, no writes")
    ap.add_argument("--dry-run", action="store_true",
                    help="crawl and parse but write nothing; prints a sample")
    ap.add_argument("--since", help="only customers updated on/after this date (YYYY-MM-DD)")
    args = ap.parse_args()

    _check_env()

    if args.probe:
        _probe()
        return 0

    require_scope()
    started = db.now()
    db.init_db()
    conn = db.connect()
    ensure_schema(conn)
    try:
        url, objcount = _submit(_query_doc(args.since))
        if not url:
            print("no customers returned (empty window)")
            db.log_sync(PLATFORM, started, 0, "ok", "empty")
            return 0
        observed = date.today().isoformat()
        n_cust = n_meta = changed = 0
        namespaces: set[str] = set()
        samples: list[dict] = []
        for customers, metafields in iter_customer_batches(url):
            namespaces.update(mf["namespace"] for mf in metafields)
            if len(samples) < 3:
                samples.extend(customers[: 3 - len(samples)])

            if args.dry_run:
                n_cust += len(customers)
                n_meta += len(metafields)
                continue

            # Flag history BEFORE the snapshot upsert overwrites the
            # comparison basis — see record_flag_changes' docstring.
            changed += record_flag_changes(conn, customers, observed)
            n_cust += store_customers(conn, customers)
            n_meta += store_metafields(conn, metafields)
            print(f"  ... {n_cust:,} customers written", flush=True)

        print(f"parsed {n_cust:,} customers, {n_meta:,} metafields (objectCount {objcount:,})")
        if args.dry_run:
            for c in samples:
                print("  sample:", {k: c[k] for k in ("customer_id", "state", "tags",
                                                      "number_of_orders")})
            print("  namespaces:", sorted(namespaces)[:20])
            print("DRY RUN — nothing written.")
            return 0

        print(f"wrote {n_cust:,} customers, {n_meta:,} metafields, {changed:,} flag-history rows")
        db.log_sync(PLATFORM, started, n_cust + n_meta, "ok",
                    f"{n_cust} customers, {n_meta} metafields, {changed} changes")
    except Exception as exc:                     # noqa: BLE001 - log then re-raise
        db.log_sync(PLATFORM, started, 0, "error", str(exc))
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
