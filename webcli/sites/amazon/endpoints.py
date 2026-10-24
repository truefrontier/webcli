"""Everything webcli amazon knows about www.amazon.com, and the guard's allowlists. Fix this file (and scripts.py,
the in-page readers) first when Amazon moves something.

Amazon has almost no JSON for these pages: they are server-rendered HTML. So the commands load each read page in the
hidden Chrome and read the rendered DOM (scripts.py), then parse its text in Python (parse.py). The one fetch is the
wish list's own "show more" fragment (a GET the page itself makes).

Status tags:
  KNOWN - Amazon's long-standing URLs (order history, order details, the printable invoice, ship-track, the Your
          Payments transactions page, wish lists), as the web app links them.
  GUESS - UNVERIFIED. Every guess fails closed: a wrong path is blocked and named in the error.
"""

from __future__ import annotations

ORIGIN = "https://www.amazon.com"
APP_HOST = "www.amazon.com"

# KNOWN: order history. timeFilter: last30, months-3, year-YYYY. 10 orders a page; startIndex pages.
ORDERS_URL = ORIGIN + "/your-orders/orders?timeFilter={filter}&startIndex={start}"
ORDERS_PAGE_SIZE = 10
ORDER_URL = ORIGIN + "/gp/your-account/order-details?orderID={id}"  # KNOWN (redirects to /your-orders/order-details)
INVOICE_URL = ORIGIN + "/gp/css/summary/print.html?orderID={id}"  # KNOWN: the printable invoice (prices, payment)
TRANSACTIONS_URL = ORIGIN + "/cpe/yourpayments/transactions"  # KNOWN: Your Payments > Transactions
WISHLISTS_URL = ORIGIN + "/hz/wishlist/ls"  # KNOWN: redirects to your default list, with every list in the left nav
WISHLIST_URL = ORIGIN + "/hz/wishlist/ls/{id}?viewType=list"  # KNOWN
LOGIN_URL = ORDERS_URL.format(filter="last30", start=0)  # signed out, Amazon sends this to /ap/signin

# GET/HEAD on www.amazon.com (and amazon.com) only under these path prefixes: the read pages above and the GETs they
# make. Everything else on Amazon is blocked, the cart (/gp/cart, /cart, /gp/aws/cart/add.html, whose GET adds to the
# cart), checkout (/gp/buy, /checkout), returns (/returns, /spr/returns, /gp/orc), cancellations, subscriptions,
# account edits, sign-out, product pages, ads and telemetry included.
READ_PATHS = (
    "/your-orders/orders",
    "/your-orders/order-details",
    "/your-orders/invoice",  # GUESS: the new invoice popover
    "/gp/your-account/order-history",
    "/gp/your-account/order-details",
    "/gp/css/order-history",
    "/gp/css/order-details",
    "/gp/css/summary/print.html",
    "/gp/your-account/ship-track",
    "/progress-tracker/package",  # KNOWN: the newer tracking page
    "/cpe/yourpayments/transactions",
    "/hz/wishlist/ls",
    "/hz/wishlist/slv/items",  # KNOWN: the wish list's "show more" fragment (GET)
    "/hz/wishlist/your-lists",  # GUESS: the list index on some accounts
    "/gp/registry/wishlist",  # KNOWN: the legacy wish list URL, which redirects
)
# Even under a read path, a path holding one of these is refused (belt and braces: none is a read).
DENY_WORDS = ("cart", "buy", "checkout", "return", "cancel", "subscribe", "delete", "remove", "edit", "move",
              "add-to", "addto", "purchase", "signout", "sign-out", "logout")
SITE_HOSTS = ("www.amazon.com", "amazon.com")
# Static assets (images, CSS, JS): GET only, any path.
STATIC_HOSTS = ("m.media-amazon.com", "*.media-amazon.com", "images-na.ssl-images-amazon.com",
                "*.ssl-images-amazon.com", "*.images-amazon.com")

# The one POST: the transactions page's pager. It submits the page's own widget form (KNOWN: the "ppw-" Amazon
# Payments widget fields) either to the page itself or, GUESS, to the widget's continue endpoint. Allowed only when
# every widget event in the form is a page navigation.
PAGER_PATHS = ("/cpe/yourpayments/transactions",)
PAGER_PATH_RE = r"/payments-portal/data/widgets2/v1/customer/[A-Za-z0-9._-]+/continueWidget"
PAGER_EVENTS = frozenset({"DefaultNextPageNavigationEvent", "DefaultPreviousPageNavigationEvent",
                          "NextPageNavigationEvent", "PreviousPageNavigationEvent"})

# Sign-in and challenge pages (a landing path starting with one of these means signed out / challenged).
SIGNED_OUT_PATHS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge", "/ax/claim", "/ap/register")
CAPTCHA_PATHS = ("/errors/validatecaptcha",)
# Where login waits for a tab: the order history, shown signed in.
SIGNED_IN_PATHS = ("/your-orders/", "/gp/your-account/order-history", "/gp/css/order-history")

PAGER_SECONDS = 20  # how long the transactions pager may take to show the next page
MAX_PAGES = 60  # a hard stop on any pagination loop
