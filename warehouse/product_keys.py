"""Product identity keys shared by catalog_sync.py (which writes product_xref)
and server.py's product_lookup tool (which reads it).

Two keys, two different jobs:

  sku_key    norm_sku(): the channel's seller SKU, upper-cased, with any
             configured channel suffix stripped ('SKU-1001-FBA' -> 'SKU-1001').
             EXACT identity. This is what product_lookup joins on.
  match_key  norm_style(): the product title reduced to its style name
             (configured store markers removed, the ' - <variant>' segment
             dropped, punctuation collapsed). FUZZY, title-derived. Useful for
             channels that carry no SKU, never a substitute for sku_key when
             one exists. Useless for Amazon, whose titles are long SEO strings.

Both are configurable per deployment, read from the environment AT CALL TIME
(not import time) so a `.env` loaded after import still applies:

  CATALOG_SKU_STRIP_SUFFIXES  comma-separated suffixes stripped after a '-'
                              (default 'FBA', a common Amazon MSKU convention;
                              set to an empty value to strip nothing)
  CATALOG_TITLE_MARKERS       comma-separated parenthesised markers removed
                              from titles before keying, e.g. 'Outlet,Clearance'
                              turns 'Tote Bag (Outlet)' into 'tote bag'
                              (default: none)
  CATALOG_VARIANT_DELIMITER   separator between style and variant in titles
                              (default ' - ', as in 'Mini Dress - Black')

Changing any of these changes every key: rebuild product_xref afterwards
(`python catalog_sync.py --xref-only`), or old and new keys will not join.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

_DEFAULT_SUFFIXES = "FBA"
_DEFAULT_DELIMITER = " - "


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


@lru_cache(maxsize=8)
def _suffix_re(raw: str) -> re.Pattern | None:
    parts = _split_csv(raw)
    if not parts:
        return None
    return re.compile(r"-(" + "|".join(re.escape(s.upper()) for s in parts) + r")$")


@lru_cache(maxsize=8)
def _marker_re(raw: str) -> re.Pattern | None:
    parts = _split_csv(raw)
    if not parts:
        return None
    return re.compile(r"\s*\(\s*(" + "|".join(re.escape(m) for m in parts) + r")\s*\)\s*", re.I)


def norm_sku(sku: str | None) -> str:
    """'sku-1001-fba ' -> 'SKU-1001' (with the default suffix list)."""
    if not sku:
        return ""
    s = sku.strip().upper()
    rx = _suffix_re(os.environ.get("CATALOG_SKU_STRIP_SUFFIXES", _DEFAULT_SUFFIXES))
    return rx.sub("", s) if rx else s


def norm_style(title: str | None) -> str:
    """'Hearts+Stars Mini Dress - Black' -> 'hearts stars mini dress'.

    The marker strip runs BEFORE the variant split, so it is position-
    independent: 'Tote Bag (Outlet)' and 'Halter Top - White (Outlet)' both
    collapse onto their clean style. Stripping after the split would only
    catch the second."""
    if not title:
        return ""
    t = title.replace("+", " ")
    rx = _marker_re(os.environ.get("CATALOG_TITLE_MARKERS", ""))
    if rx:
        t = rx.sub(" ", t)
    delim = os.environ.get("CATALOG_VARIANT_DELIMITER") or _DEFAULT_DELIMITER
    t = t.split(delim)[0]
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
