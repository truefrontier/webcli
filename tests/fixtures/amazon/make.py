"""Regenerate readers.json: run webcli amazon's real in-page readers (scripts.py) over the Amazon-shaped HTML pages
here, in a headless Chrome whose amazon.com requests are answered from these files (nothing reaches Amazon). Also
records the transactions pager's real form POST body (the one POST the guard allows) and the wish list's "show more"
fragment. test_amazon.py parses readers.json. Run it after changing a reader or a page:
    .venv/bin/python tests/fixtures/amazon/make.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from webcli.sites.amazon import scripts

HERE = Path(__file__).parent
PAGES = {
    "orders": ("https://www.amazon.com/your-orders/orders?timeFilter=months-3&startIndex=0", "orders.html", "ORDERS"),
    "order": ("https://www.amazon.com/your-orders/order-details?orderID=111-2223334-5556667", "order.html", "ORDER"),
    "invoice": ("https://www.amazon.com/gp/css/summary/print.html?orderID=111-2223334-5556667", "invoice.html",
                "PAGE_TEXT"),
    "track": ("https://www.amazon.com/gp/your-account/ship-track?itemId=abc&orderId=111-2223334-5556667",
              "track.html", "TRACK"),
    "transactions": ("https://www.amazon.com/cpe/yourpayments/transactions", "transactions.html", "TRANSACTIONS"),
    "wishlists": ("https://www.amazon.com/hz/wishlist/ls/AAAAAAAAAAAA", "wishlist.html", "WISHLISTS"),
    "wishlist": ("https://www.amazon.com/hz/wishlist/ls/AAAAAAAAAAAA?viewType=list", "wishlist.html", "WISHLIST"),
    "signin": ("https://www.amazon.com/ap/signin?openid.return_to=x", "signin.html", "ORDERS"),
    "captcha": ("https://www.amazon.com/errors/validateCaptcha", "captcha.html", "ORDERS"),
    "captcha_inline": ("https://www.amazon.com/your-orders/orders?timeFilter=last30&startIndex=0", "captcha.html",
                       "ORDERS"),
}


async def main() -> None:
    out: dict = {}
    posts: list = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        current = {"file": None}

        async def route(r):
            url, method = r.request.url, r.request.method
            if method == "POST":
                posts.append({"url": url, "body": r.request.post_data})
                await r.fulfill(status=200, content_type="text/html", body="<html><body>next</body></html>")
            elif "/hz/wishlist/slv/items" in url:
                await r.fulfill(status=200, content_type="text/html", body=(HERE / "wishlist_more.html").read_text())
            elif "www.amazon.com" in url and current["file"]:
                await r.fulfill(status=200, content_type="text/html", body=(HERE / current["file"]).read_text())
            else:
                await r.fulfill(status=200, body="")

        await page.route("**/*", route)
        for name, (url, file, reader) in PAGES.items():
            current["file"] = file
            await page.goto(url)
            out[name] = await page.evaluate(getattr(scripts, reader))
            if name == "wishlist":
                out["wishlist_more"] = await page.evaluate(scripts.WISHLIST_MORE, out["wishlist"]["next"])
            if name == "transactions":
                out["next_clicked"] = await page.evaluate(scripts.NEXT_PAGE)
                await page.wait_for_timeout(500)
        out["pager_posts"] = posts
        await browser.close()
    (HERE / "readers.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
