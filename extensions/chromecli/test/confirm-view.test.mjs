import { test } from "node:test";
import assert from "node:assert/strict";
import { countdown, detailRows, headline, reasonItems } from "../confirm-view.js";

const place = { op: "click", origin: "http://127.0.0.1:8765", title: "Form", reasons: ["submit_button", "submit_word:order"],
  element: { tag: "button", role: "button", name: "Place order", outerHTML: "<button>Place order</button>" } };

test("headlines", () => {
  assert.equal(headline(place), "Click “Place order”");
  assert.equal(headline({ op: "open", origin: "https://example.org" }), "Open example.org");
  assert.equal(headline({ op: "navigate", origin: "https://example.net" }), "Go to example.net");
  assert.equal(headline({ op: "press", key: "Enter", element: { tag: "input", name: "Email" } }), "Press Enter in “Email”");
  assert.equal(headline({ op: "fill", element: { tag: "input", name: "" } }), "Fill this input");
});

test("details: site, tab, element, masked value", () => {
  const rows = detailRows({ ...place, op: "fill", value: { masked: true, text: "••••••••", chars: 9 },
    element: { tag: "input", role: "textbox", type: "password", name: "Password" } });
  const map = Object.fromEntries(rows.map(([k, v, kind]) => [k, { v, kind }]));
  assert.equal(map.Origin.v, "http://127.0.0.1:8765");
  assert.equal(map.Element.v, "<input> · textbox · type=password");
  assert.equal(map.Value.kind, "masked");
  assert.match(map.Value.v, /9 chars, hidden/);
});

test("reasons and countdown", () => {
  assert.equal(reasonItems(place.reasons).length, 2);
  assert.deepEqual(countdown(10_000, 0), { seconds: 10, label: "10s", urgent: true });
  assert.equal(countdown(0, 5000).seconds, 0);
});
