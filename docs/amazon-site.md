# webcli amazon

`webcli amazon` reads your Amazon.com orders, payment transactions, package tracking, and wish lists, through its
own identity's hidden Chrome. It exists so an agent can tie a card charge to the order behind it: "what was the
$33.90 Amazon charge on the 21st?" is `webcli amazon match 33.90 --date 2026-10-21`. It is hard read-only.

## Sign in (a person, once)

```sh
webcli amazon login          # same as: webcli identity login amazon:default
webcli amazon status         # {"signedIn": true}
```

The profile is `~/.webcli/identities/amazon-default/chrome-profile`. Login opens a visible Chrome on Amazon's
sign-in. Enter your password and any one-time code Amazon sends, and keep **Keep me signed in** checked. It finishes
once Your Orders shows and a fresh hidden Chrome on the profile agrees. The session stays in that profile on this
computer. Agents never sign in for you.

When Amazon signs the profile out, or shows a captcha or bot check to a hidden run, every command exits 4 and says
which. webcli never solves, skips, or retries around a captcha: a person runs `webcli amazon login` again.

## Commands

All take `--json` (same as `--format json`) and webcli's usual `--format/--quiet/--compact/--select/--csv`. An
`ORDER_ID` is `111-2223334-5556667` (or a digital `D01-...` id) or an order URL. Dates are `YYYY-MM-DD` or relative
(`30d`, `6w`, `3m`).

| Command | What it does |
|---|---|
| `orders [--year Y \| --since DATE] [--limit N] [--details]` | Orders, newest first: id, date, total, ship-to name, status, items (title, ASIN, qty, price when shown). Default: the last 3 months. `--details` also reads each order's invoice (item prices, tax, total, payment) |
| `order ORDER_ID` | One order: items with qty and price, subtotal, shipping, discounts, total before tax, tax, gift card, refund, total, shipments (status, items, tracking link), payment |
| `transactions [--since DATE] [--limit N]` | Your Payments > Transactions: date (pending ones have none), amount, `charge` or `refund`, card, order ids, merchant, `pending` or `completed`. Default `--since 30d` |
| `match AMOUNT [--date DATE] [--window DAYS] [--no-orders]` | Transactions of exactly AMOUNT within DAYS of DATE (default 7; with no date, the last 60 days), then order totals of that amount (unless `--no-orders`), each with its orders' date, total, and items. `matched` is true or false |
| `track ORDER_ID` | Each shipment: status, delivered, carrier, tracking number, delivery date, items |
| `wishlists` | Your lists: id, name, default, privacy |
| `wishlist LIST [--limit N]` | One list (id or name): items with title, ASIN, price (null when unavailable), added date, availability |
| `status` / `login` | Sign-in check / sign in (a person) |

Payments are always **card brand + last 4** (`{"brand": "Visa", "last4": "4242", "display": "Visa ****4242"}`),
plus gift card or points entries. As a backstop every field except ids, tracking numbers, ASINs, and URLs is scrubbed:
any run of digits shaped like a card number becomes `****` and its last 4. Addresses are never reported; `ship_to`
is the recipient's name only.

Example: matching a charge.

```sh
$ webcli amazon match 33.90 --date 2026-10-21 --json
{"amount": 33.9, "date": "2026-10-21", "start": "2026-10-14", "end": "2026-10-28", "window_days": 7, "matched": true,
 "matches": [{"source": "transaction", "date": "2026-10-21", "amount": 33.9, "type": "charge",
              "card": {"brand": "Visa", "last4": "4242", "display": "Visa ****4242"},
              "order_ids": ["111-2223334-5556667"],
              "orders": [{"order_id": "111-2223334-5556667", "date": "2026-10-20", "total": 33.9,
                          "items": [{"title": "The First Book: A Novel", "qty": 2, ...}, ...]}]}],
 "searched": {"transactions": 2, "orders": 1}}
```

(Made-up data. One charge can cover several orders, and one order can be charged in several shipments: a
transaction's `order_ids` and each order's own totals tell them apart.)

## Hard read-only: the guard

Every request the hidden tab makes passes a fail-closed policy (`webcli/sites/amazon/guard.py`):

- **GET/HEAD** only over https, port 443, to `www.amazon.com`/`amazon.com` on these paths (or below them): Your
  Orders (`/your-orders/orders`, `/gp/your-account/order-history`, `/gp/css/order-history`), order details
  (`/your-orders/order-details`, `/gp/your-account/order-details`), the invoice (`/gp/css/summary/print.html`),
  tracking (`/gp/your-account/ship-track`, `/progress-tracker/package`), transactions
  (`/cpe/yourpayments/transactions`), and wish lists (`/hz/wishlist/ls`, `/hz/wishlist/slv/items`,
  `/hz/wishlist/your-lists`, `/gp/registry/wishlist`); and Amazon's image and static hosts. A path containing cart,
  buy, checkout, return, cancel, subscribe, delete, remove, edit, move, add-to, purchase, or sign-out is refused
  even under an allowed prefix.
- **POST** only to the transactions page (or its `payments-portal ... continueWidget` endpoint), and only a
  page-navigation form: every field is a `ppw-` field and every event is `DefaultNextPageNavigationEvent`,
  `NextPageNavigationEvent`, `PreviousPageNavigationEvent`, or `DefaultPageNavigationEvent`.
- Everything else is blocked: product pages (they carry Buy Now), the cart and `aws/cart/add` links, checkout,
  returns, cancellations, Subscribe & Save, payment-method and address edits, wish list edits, sign-out, telemetry.

A blocked request is logged by method, host, and path only (no query string or body). The in-page readers only read
the DOM; the one click is the transactions page's Next page button.

## How it reads

Each command loads Amazon's own pages in the hidden tab and reads them with small in-page scripts
(`webcli/sites/amazon/scripts.py`) that return text; Python (`parse.py`) turns that text into records. Orders page
10 at a time (`startIndex`); a year uses `timeFilter=year-Y`, 30 days `last30`, 90 days `months-3`. Transactions page
with the page's Next button, up to 60 pages. Wish lists follow the list's "show more" link.

Amazon changes its markup often. When a command comes back empty or exits 3 on a page that has data, the readers'
selectors are the first suspect; the tests (`tests/test_amazon.py`, fixtures in `tests/fixtures/amazon/`, made-up
data) run the real readers in Chrome over Amazon-shaped pages via `tests/fixtures/amazon/make.py`.

## Exit codes

0 ok; 2 usage (bad order id, amount, or date); 3 not found (no such order or list); 4 signed out, or a captcha (a
person runs `webcli amazon login`); 5 other failures; 7 config.
