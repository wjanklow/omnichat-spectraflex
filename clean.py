# scripts/reset_script_tags.py
import httpx, json, pathlib, sys, os
sys.path.append(str(pathlib.Path(".").resolve()))

from settings import settings      # your .env is loaded here
from services.shopify import add_script_tag

ADMIN_URL = f"{settings.shop_url}/admin/api/2024-01"
HEADERS   = {
    "X-Shopify-Access-Token": settings.shop_admin_token.get_secret_value(),
    "Content-Type": "application/json",
}

def list_tags():
    q = {"query": "{ scriptTags(first:50){edges{node{id src}} } }"}
    r = httpx.post(f"{ADMIN_URL}/graphql.json", headers=HEADERS, json=q, verify=False).json()
    return [edge["node"] for edge in r["data"]["scriptTags"]["edges"]]

def delete_tag(tag_id):
    httpx.delete(f"{ADMIN_URL}/script_tags/{tag_id}.json", headers=HEADERS, verify=False)

def main():
    tags = list_tags()
    for tag in tags:
        delete_tag(tag["id"].split("/")[-1])
    print(f"🗑️  removed {len(tags)} old tags")

    SRC = "https://taupe-vacherin-f62f9e.netlify.app/widget.min.js?v=22"
    new_id = add_script_tag(SRC)
    print("✅ added fresh tag:", new_id, "\n→", SRC)

if __name__ == "__main__":
    main()
