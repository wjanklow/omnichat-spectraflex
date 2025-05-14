"""
Thin Storefront-API helper
──────────────────────────
• _gql()             – fire GraphQL POSTs with back-off & cert fix
• create_checkout()  – return a ready-to-use checkout URL
"""

from __future__ import annotations

import json, time, os                                    # ← added os
import requests, backoff, certifi

# --- force Requests / urllib3 / OpenSSL to use certifi everywhere ---------
os.environ["SSL_CERT_FILE"]      = certifi.where()       # 🔑 1-liner fix
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()       # 🔑

from settings import settings

_URL = f"https://{settings.shop_url.host}/api/2024-04/graphql.json"  # host is safe (no scheme)
_HEADERS = {
    "X-Shopify-Storefront-Access-Token": settings.storefront_token.get_secret_value(),
    "Content-Type": "application/json",
}

# ── resilient POST with exponential back-off ──────────────────────────────
@backoff.on_exception(backoff.expo, requests.RequestException, max_time=40)
def _gql(query: str, variables: dict):
    r = requests.post(
        _URL,
        headers=_HEADERS,
        json={"query": query, "variables": variables},
        timeout=20,
        # verify points to the same bundle, but is harmless to keep
        verify=certifi.where(),
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


# ── public helper ─────────────────────────────────────────────────────────
def create_checkout(variant_id: str, qty: int = 1) -> str:
    """
    Return a Checkout URL that already contains one line-item.
    """
    q = """
    mutation ($variant: ID!, $qty: Int!) {
      checkoutCreate(input:{
        lineItems:[{variantId:$variant, quantity:$qty}]
      }) {
        checkout { webUrl }
      }
    }"""
    data = _gql(q, {"variant": variant_id, "qty": qty})
    return data["checkoutCreate"]["checkout"]["webUrl"]
