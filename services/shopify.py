"""
omnichat.services.shopify
~~~~~~~~~~~~~~~~~~~~~~~~~
Thin wrapper over Shopify Admin & Storefront APIs.

Key features
------------
* Auto-retries on 429 / 5xx with exponential back-off.
* JSON-safe logging (no secrets).
* Typed helpers for common actions: add / prune ScriptTags, iterate products.
* Webhook HMAC verification (for /webhooks endpoints).

Usage
-----
>>> from omnichat.services.shopify import add_script_tag, iter_products
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Generator, Iterable, Literal

import backoff
import httpx

from omnichat_log import logger
from settings import settings

# ── 1.  HTTPX client shared per-process ───────────────────────────────────────
_ADMIN_URL = f"https://{settings.shop_url.host}/admin/api/2025-04"
_HEADERS = {
    "X-Shopify-Access-Token": settings.shop_admin_token.get_secret_value(),
    "Content-Type": "application/json",
    "User-Agent": "Omnichat/1.0 (+github.com/your-org/omnichat)",
}
_async_client = httpx.AsyncClient(base_url=_ADMIN_URL, headers=_HEADERS, timeout=30)
_sync_client = httpx.Client(base_url=_ADMIN_URL, headers=_HEADERS, timeout=30)


# ── 2.  Back-off helper (shared) ─────────────────────────────────────────────
def _backoff_hdl(details):
    logger.warning(
        f"Shopify call retry {details['tries']} after {details['wait']:0.1f}s "
        f"(function={details['target'].__name__})"
    )


def _giveup(exc: Exception):
    # give up on 4xx except 429
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429


# ── 3.  GraphQL call (sync) ---------------------------------------------------
@backoff.on_exception(
    backoff.expo,
    (httpx.HTTPStatusError, httpx.ReadTimeout),
    giveup=_giveup,
    on_backoff=_backoff_hdl,
    max_time=60,
)
def graphql(query: str, variables: dict | None = None) -> dict:
    payload = {"query": query, "variables": variables or {}}
    r = _sync_client.post("/graphql.json", json=payload)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


# ── 4.  Product iterator (for ingestion script) ------------------------------
_PRODUCT_Q = """
query($cursor:String){
  products(first:100, after:$cursor){
    pageInfo{hasNextPage}
    edges{
      cursor
      node{
        id title handle tags descriptionHtml
        variants(first:5){nodes{id title sku price{amount currencyCode}}}
        images(first:1){nodes{url}}
      }
    }
  }
}
"""


def iter_products() -> Generator[dict, None, None]:
    cursor = None
    while True:
        data = graphql(_PRODUCT_Q, {"cursor": cursor})
        p = data["products"]
        for edge in p["edges"]:
            yield edge["node"]
            cursor = edge["cursor"]
        if not p["pageInfo"]["hasNextPage"]:
            break


# ── 5.  ScriptTag helpers -----------------------------------------------------
_SCOPES: set[Literal["ONLINE_STORE", "ORDER_STATUS"]] = {"ONLINE_STORE", "ORDER_STATUS"}


def add_script_tag(src: str, display_scope: Literal["ONLINE_STORE", "ORDER_STATUS"] = "ONLINE_STORE") -> str:
    if display_scope not in _SCOPES:
        raise ValueError(f"display_scope must be one of {_SCOPES}")

    m = """
    mutation($input: ScriptTagInput!){
      scriptTagCreate(input:$input){
        scriptTag{ id src displayScope }
        userErrors{ field message }
      }
    }
    """
    resp = graphql(
        m,
        {"input": {"src": src, "displayScope": display_scope}},
    )["scriptTagCreate"]

    if resp["userErrors"]:
        raise RuntimeError(resp["userErrors"])

    tag = resp["scriptTag"]
    logger.info(f"Created ScriptTag {tag['id']} src={tag['src']} scope={tag['displayScope']}")
    return tag["id"]


def prune_script_tags(keep_src_substring: str) -> None:
    q = """
    {
      scriptTags(first:250){
        edges{ node{ id src } }
      }
    }
    """
    tags = graphql(q)["scriptTags"]["edges"]
    for edge in tags:
        tag = edge["node"]
        if keep_src_substring not in tag["src"]:
            _sync_client.delete(f"/script_tags/{tag['id']}.json")
            logger.info(f"Deleted old ScriptTag {tag['id']} src={tag['src']}")


# ── 6.  Webhook HMAC verification --------------------------------------------
def verify_webhook(x_shopify_hmac256: str, body: bytes) -> bool:
    secret = settings.shop_admin_token.get_secret_value().encode()
    digest = hmac.new(secret, body, hashlib.sha256).digest()
    calc = base64.b64encode(digest).decode()
    valid = hmac.compare_digest(calc, x_shopify_hmac256)
    if not valid:
        logger.error("Shopify webhook HMAC verification failed")
    return valid
