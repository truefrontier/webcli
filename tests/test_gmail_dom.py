"""webcli gmail's FIELD_JS (fetch.py) on a stand-in create dialog in headless Chromium: two label rows 25 px apart.
Skips without a headless Chromium. Kept out of test_gmail.py: the sync Playwright fixture breaks asyncio.run in tests
that share its module. Nothing reaches Google: the stand-in is set as the page's content."""

from __future__ import annotations

from test_messages_dom import chromium  # noqa: F401  (fixture)

from webcli.sites.gmail import fetch

ROWS_25PX = """<div style="position:relative">
  <label style="position:absolute;top:105px;left:10px">Has the words</label>
  <input id="words" type="text" style="position:absolute;top:105px;left:100px;width:200px;height:20px">
  <label style="position:absolute;top:130px;left:10px">Doesn't have</label>
  <input id="not" type="text" style="position:absolute;top:130px;left:100px;width:200px;height:20px">
</div>"""


def test_field_js_takes_the_nearest_box(chromium):
    page = chromium.new_page()
    page.set_content(ROWS_25PX)
    page.evaluate(fetch.FIELD_JS, "Doesn't have")
    assert page.evaluate("document.activeElement.id") == "not"  # not "words", the box 25 px above
    page.close()
