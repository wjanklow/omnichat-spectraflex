"""
Light-weight helper that creates a **Shopify Storefront-API** checkout
containing a single variant and returns the checkout URL.
"""

import json, certifi, requests
from settings import settings

_URL = (
    f"https://{settings.shop_url.host}/api/2024-04/graphql.json"
)  # → shop_url is already a pydantic AnyHttpUrl
_HEADERS = {
    "X-Shopify-Storefront-Access-Token": settings.storefront_token.get_secret_value(),
    "Content-Type": "application/json",
}

def _gql(query: str, variables: dict[str, object]):
    """Fire a Storefront GraphQL call and return `data`."""
    r = requests.post(
        _URL,
        headers=_HEADERS,
        json={"query": query, "variables": variables},
        timeout=20,
        verify=certifi.where(),        # ← important inside Render
    )
    r.raise_for_status()
    return r.json()["data"]

def create_checkout(variant_id: str, qty: int = 1) -> str:
    """Return the hosted‐checkout URL that contains `qty` × `variant_id`."""
    q = """
    mutation ($variant: ID!, $qty: Int!) {
      checkoutCreate(input:{
        lineItems:[{variantId:$variant, quantity:$qty}]
      }) { checkout { webUrl } }
    }
    """
    data = _gql(q, {"variant": variant_id, "qty": qty})
    return data["checkoutCreate"]["checkout"]["webUrl"]
