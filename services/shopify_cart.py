"""
storefront.py – thin Shopify Storefront-API helper
──────────────────────────────────────────────────
• _gql()            – fire GraphQL POSTs with retry & TLS-cert fix
• create_checkout() – return a ready-to-use checkout URL
"""

from __future__ import annotations

import os
import requests, backoff, certifi

from settings import settings  # holds shop_url + storefront_token


# ────────────────────────────────────────────────────────────────────────────
# Point Requests / urllib3 / OpenSSL to the Certifi CA bundle.
# This single step fixes the SSL_CERTIFICATE_VERIFY_FAILED error on Render.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

_URL = f"https://{settings.shop_url.host}/api/2024-04/graphql.json"
_HEADERS = {
    "X-Shopify-Storefront-Access-Token": settings.storefront_token.get_secret_value(),
    "Content-Type": "application/json",
}

# ────────────────────────────────────────────────────────────────────────────
@backoff.on_exception(backoff.expo, requests.RequestException, max_time=40)
def _gql(query: str, variables: dict) -> dict:
    """Fire a GraphQL POST, raise on HTTP or GraphQL errors."""
    resp = requests.post(
        _URL,
        headers=_HEADERS,
        json={"query": query, "variables": variables},
        timeout=20,
        verify=certifi.where(),   # harmless duplicate but explicit
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:                          # GraphQL-level errors
        raise RuntimeError(data["errors"])
    return data["data"]


# ────────────────────────────────────────────────────────────────────────────
def create_checkout(variant_id: str, qty: int = 1) -> str:
    """
    Create a one-item checkout and return the ready webUrl.
    """
    q = """
    mutation ($variant: ID!, $qty: Int!) {
      checkoutCreate(input:{
        lineItems:[{ variantId:$variant, quantity:$qty }]
      }) {
        checkout { webUrl }
      }
    }"""
    data = _gql(q, {"variant": variant_id, "qty": qty})
    return data["checkoutCreate"]["checkout"]["webUrl"]
