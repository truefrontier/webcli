"""The in-page readers webcli amazon runs (page.evaluate). Each returns plain text and attributes, never a decision:
parse.py turns them into records. Every reader starts with `gate` ({captcha, signin, path}), so a sign-in or a
bot-check page is never read as "no orders".

They only read the DOM. NEXT_PAGE is the one that clicks, and only the transactions pager's own Next button (the
guard allows that form's POST and nothing else).
"""

from __future__ import annotations

COMMON = r"""
const txt = el => ((el && (el.innerText || el.textContent)) || '').replace(/[ \t\u00a0]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
const line = el => txt(el).replace(/\s+/g, ' ').trim();
const abs = h => { try { return new URL(h, location.href).href; } catch (e) { return ''; } };
const gate = () => {
  const p = location.pathname.toLowerCase();
  const head = ((document.body && document.body.innerText) || '').slice(0, 3000);
  const captcha = p.startsWith('/errors/validatecaptcha')
    || !!document.querySelector('form[action*="validateCaptcha"], #captchacharacters, #aa-challenge-whole-page-iframe, iframe[src*="captcha"]')
    || /not a robot|Enter the characters you see below|solve this puzzle/i.test(head);
  const signin = /^\/(ap\/(signin|mfa|cvf|challenge|register)|ax\/claim)/.test(p)
    || !!document.querySelector('form[name="signIn"], #ap_password, #auth-mfa-otpcode');
  return {captcha, signin, host: location.hostname, path: location.pathname};
};
const asinOf = h => { const m = (h || '').match(/\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})/); return m ? m[1] : null; };
const NOISE = '.a-carousel, #rhf, [data-component*="ecommendation"], [cel_widget_id*="recommendation"], .a-popover';
const products = root => {
  const byAsin = new Map();
  for (const a of root.querySelectorAll('a[href*="/dp/"], a[href*="/gp/product/"], a[href*="/gp/aw/d/"]')) {
    if (a.closest(NOISE)) continue;
    const asin = asinOf(a.getAttribute('href'));
    if (!asin) continue;
    const box = a.closest('.yohtmlc-item, .item-box, [class*="item-box"], .od-item-view-box, .a-fixed-left-grid, [data-component="purchasedItems"] > div')
      || (a.parentElement && a.parentElement.parentElement) || a;
    const cur = byAsin.get(asin) || {asin, title: '', alt: '', qty: '', price: '', url: abs(a.getAttribute('href'))};
    const t = line(a);
    if (t.length > cur.title.length) cur.title = t;
    const img = a.querySelector('img');
    if (img && img.alt && !cur.alt) cur.alt = img.alt.trim();
    if (!cur.qty) cur.qty = line(box.querySelector('.product-image__qty, .item-view-qty, [data-component="quantity"]'));
    if (!cur.price) cur.price = line(box.querySelector('[data-component="unitPrice"] .a-offscreen, .a-price .a-offscreen, .a-color-price, [data-component="unitPrice"]'));
    byAsin.set(asin, cur);
  }
  return [...byAsin.values()];
};
const topLevel = (root, selector) => {
  const all = [...root.querySelectorAll(selector)];
  return all.filter(el => !all.some(other => other !== el && other.contains(el)));
};
"""

# The order history page: one record per order card, plus the next page's link.
ORDERS = "() => {" + COMMON + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g, orders: [], next: null, ready: false};
  const cards = topLevel(document, '.order-card, .js-order-card, #ordersContainer .order, #orderCard, .a-box-group.order');
  const orders = cards.map(card => {
    const header = card.querySelector('.order-header, .a-box.order-info, .order-info, .a-box') || card;
    const detail = card.querySelector('a[href*="order-details"]');
    return {
      header: txt(header).slice(0, 1500),
      order_id: line(card.querySelector('.yohtmlc-order-id span[dir="ltr"], .yohtmlc-order-id .value, [data-component="orderId"]')),
      ship_to: line(card.querySelector('.yohtmlc-recipient .a-popover-trigger, .yohtmlc-recipient, .recipient .a-popover-trigger')),
      status: line(card.querySelector('.delivery-box__primary-text, .yohtmlc-shipment-status-primaryText, .shipment-top-row .a-text-bold, .js-shipment-info-container .a-text-bold')),
      items: products(card),
      details_url: detail ? abs(detail.getAttribute('href')) : '',
    };
  });
  const nextA = document.querySelector('ul.a-pagination li.a-last:not(.a-disabled) a');
  const ready = !!document.querySelector('#ordersContainer, .your-orders-content-container, #yourOrders, .js-yo-main-content, #yourOrdersContent, .order-card, .a-pagination')
    || /Your Orders/i.test(document.title || '');
  return {gate: g, orders, next: nextA ? abs(nextA.getAttribute('href')) : null, ready};
}"""

# One order's details page: its text, items, shipments (with each Track package link), payment, ship-to.
ORDER = "() => {" + COMMON + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g};
  const root = document.querySelector('#orderDetails, [data-component="orderCard"], .od-container, #a-page') || document.body;
  const trackSel = 'a[href*="ship-track"], a[href*="progress-tracker"]';
  const shipments = topLevel(root, '[data-component="shipments"] .a-box, .a-box.shipment, .shipment, [data-component="shipmentsLeftGrid"]').map(box => {
    const track = box.querySelector(trackSel);
    return {
      status: line(box.querySelector('[data-component="shipmentStatus"], .od-status-message, .shipment-top-row .a-text-bold, .a-size-medium.a-text-bold, .a-text-bold')),
      text: txt(box).slice(0, 1500),
      items: products(box).map(p => p.title || p.alt),
      track_url: track ? abs(track.getAttribute('href')) : '',
    };
  });
  const pay = root.querySelector('[data-component="viewPaymentPlanSummaryWidget"], [data-component="paymentMethod"], .pmts-payments-instrument-details, .pmts-payment-instrument-billing-address, #od-payment-method');
  let payText = txt(pay), payAlts = pay ? [...pay.querySelectorAll('img')].map(i => i.alt || '').filter(Boolean) : [];
  if (!payText) {
    const head = [...root.querySelectorAll('h5, h4, h3, .a-text-bold, span')].find(el => /^payment method/i.test(line(el)));
    const box = head && (head.closest('.a-column, [data-component], .a-section') || head.parentElement);
    payText = txt(box);
    payAlts = box ? [...box.querySelectorAll('img')].map(i => i.alt || '').filter(Boolean) : [];
  }
  const ship = root.querySelector('[data-component="shippingAddress"] li, .displayAddressFullName, #od-shipping-address-container .a-list-item, .od-shipping-address-container li');
  return {
    gate: g,
    text: txt(root).slice(0, 30000),
    items: products(root),
    shipments,
    track_urls: [...root.querySelectorAll(trackSel)].map(a => abs(a.getAttribute('href'))),
    payment_text: payText.slice(0, 1000),
    payment_alts: payAlts,
    ship_to: line(ship),
  };
}"""

# Any page as text (the printable invoice).
PAGE_TEXT = "() => {" + COMMON + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g, text: ''};
  return {gate: g, text: txt(document.body).slice(0, 60000)};
}"""

# A tracking page (ship-track or progress-tracker).
TRACK = "() => {" + COMMON + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g};
  const primary = line(document.querySelector('#primaryStatus, .pt-promise-main-slot, .pt-delivery-card-primary-status, [data-testid="primary-status"], .milestone-primaryMessage'));
  const secondary = line(document.querySelector('#secondaryStatus, .pt-promise-details-slot, .pt-delivery-card-secondary-status, [data-testid="secondary-status"]'));
  const carrier = line(document.querySelector('.carrierRelatedInfo-mfn-providerTitle, .pt-delivery-card-carrier, [data-testid="carrier-name"]'));
  const tracking = line(document.querySelector('.pt-delivery-card-trackingId, .carrierRelatedInfo-trackingId-text, [data-testid="tracking-id"]'));
  const root = document.querySelector('#a-page, main') || document.body;
  return {gate: g, primary, secondary, carrier, tracking, text: txt(root).slice(0, 20000)};
}"""

# The transactions page: each line item with its date header and section ("In Progress", "Completed"), plus the
# page's text as a fallback reader, and whether a Next page button exists.
TRANSACTIONS = "() => {" + COMMON + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g, rows: [], text: '', has_next: false, ready: false};
  const sel = '.apx-transactions-sleeve-header-container, .apx-transaction-date-container, .apx-transactions-line-item-component-container';
  let date = '', section = '';
  const rows = [];
  for (const el of document.querySelectorAll(sel)) {
    if (el.matches('.apx-transactions-sleeve-header-container')) section = line(el);
    else if (el.matches('.apx-transaction-date-container')) date = line(el);
    else rows.push({date, section, text: txt(el).slice(0, 1500),
                    order_links: [...el.querySelectorAll('a[href]')].map(a => a.getAttribute('href') || '').filter(h => /orderI[dD]=/.test(h))});
  }
  const root = document.querySelector('.pmts-portal-root, [id^="pmts-portal"], .apx-transactions-container, main') || document.body;
  const next = [...document.querySelectorAll('input[name*="NextPageNavigationEvent"], span.a-button input, button')].find(
    el => /NextPageNavigationEvent/.test(el.getAttribute('name') || '') || /^next page$/i.test(line(el.closest('.a-button') || el)));
  const disabled = next && (next.disabled || !!next.closest('.a-button-disabled'));
  const ready = rows.length > 0 || !!document.querySelector('.apx-transactions-container, .pmts-portal-root, [id^="pmts-portal"]');
  return {gate: g, rows, text: rows.length ? '' : txt(root).slice(0, 60000), has_next: !!next && !disabled,
          first: rows.length ? rows[0].text.slice(0, 200) + '|' + rows[0].date : '', ready};
}"""

# Clicks the transactions pager's Next button (only that). True when there was one to click.
NEXT_PAGE = "() => {" + COMMON + r"""
  const next = [...document.querySelectorAll('input[name*="NextPageNavigationEvent"], span.a-button input, button')].find(
    el => /NextPageNavigationEvent/.test(el.getAttribute('name') || '') || /^next page$/i.test(line(el.closest('.a-button') || el)));
  if (!next || next.disabled || next.closest('.a-button-disabled')) return false;
  next.click();
  return true;
}"""

# The wish list index: every list in the left nav (id, name, privacy), and the list shown.
WISHLISTS = "() => {" + COMMON + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g, lists: []};
  const seen = new Map();
  for (const a of document.querySelectorAll('#your-lists-nav a[href*="/hz/wishlist/ls/"], #left-nav a[href*="/hz/wishlist/ls/"], .wl-list a[href*="/hz/wishlist/ls/"], a[id^="wl-list-link-"]')) {
    const m = (a.getAttribute('href') || '').match(/\/hz\/wishlist\/ls\/([A-Z0-9]+)/);
    if (!m || seen.has(m[1])) continue;
    const title = line(a.querySelector('[id^="wl-list-entry-title-"], .wl-list-entry-title')) || txt(a).split('\n')[0];
    const privacy = line(a.querySelector('[id^="wl-list-privacy-"], .wl-list-entry-privacy'));
    const isDefault = /default list/i.test(txt(a));
    seen.set(m[1], {id: m[1], name: title, privacy, is_default: isDefault});
  }
  const shown = (location.pathname.match(/\/hz\/wishlist\/ls\/([A-Z0-9]+)/) || [])[1] || null;
  return {gate: g, lists: [...seen.values()], shown, shown_name: line(document.querySelector('#profile-list-name, #listTitle'))};
}"""

_ITEMS = r"""
const readItems = doc => [...doc.querySelectorAll('li[data-itemid]')].map(li => {
  const a = li.querySelector('a[id^="itemName_"]');
  let asin = null;
  try { const p = JSON.parse(li.getAttribute('data-reposition-action-params') || '{}'); asin = ((p.itemExternalId || '').match(/ASIN:([A-Z0-9]{10})/) || [])[1] || null; } catch (e) {}
  if (!asin && a) asin = asinOf(a.getAttribute('href'));
  return {
    item_id: li.getAttribute('data-itemid'),
    asin,
    title: a ? ((a.getAttribute('title') || '').trim() || line(a)) : '',
    url: a ? abs(a.getAttribute('href')) : '',
    price_attr: li.getAttribute('data-price') || '',
    price_text: line(li.querySelector('.a-price .a-offscreen, [id^="itemPrice_"] .a-offscreen, [id^="itemPrice_"]')),
    byline: line(li.querySelector('[id^="item-byline-"]')),
    added: line(li.querySelector('[id^="itemAddedDate_"]')),
    requested: line(li.querySelector('[id^="itemRequested_"]')),
    purchased: line(li.querySelector('[id^="itemPurchased_"]')),
    available: !/unavailable|no longer available/i.test(line(li.querySelector('[id^="availability-msg_"], .itemAvailMessage'))),
  };
});
const moreUrl = doc => {
  if (doc.querySelector('#endOfListMarker')) return null;
  const el = doc.querySelector('input[name="showMoreUrl"], .showMoreUrl');
  const v = el ? (el.value || el.getAttribute('value') || '') : '';
  return v ? abs(v) : null;
};
"""

# One wish list page: its items and the "show more" URL.
WISHLIST = "() => {" + COMMON + _ITEMS + r"""
  const g = gate();
  if (g.captcha || g.signin) return {gate: g, items: [], next: null};
  return {gate: g, name: line(document.querySelector('#profile-list-name, #listTitle')), items: readItems(document),
          next: moreUrl(document), ready: !!document.querySelector('#g-items, #wishlist-page, #my-lists-tab')};
}"""

# The next wish list fragment: a GET of the page's own showMoreUrl (www.amazon.com/hz/wishlist/slv/items, the
# guard checks it), parsed off-page.
WISHLIST_MORE = "async (url) => {" + COMMON + _ITEMS + r"""
  const res = await fetch(url, {credentials: 'include', headers: {'accept': 'text/html'}});
  if (!res.ok) return {status: res.status, items: [], next: null};
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  return {status: res.status, items: readItems(doc), next: moreUrl(doc)};
}"""
