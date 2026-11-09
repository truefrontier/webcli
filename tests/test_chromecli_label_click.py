"""chromecli click on a <label>, in headless Chromium on tests/fixtures/chromecli/label.html. HTML's label activation
makes a click on a label (or on plain content inside one) also click the label's control, so a styled
<label for=pay> over a hidden submit submits the form. The element description carries that control as
`activates`, and the gate counts it: "always gated" holds for the submit the click really triggers."""

from __future__ import annotations

import pytest

from test_chromecli_dom import gate
from test_chromecli_svg_click import browser_page  # noqa: F401  (the headless Chromium and the fixture server)


@pytest.fixture
def page(browser_page):  # noqa: F811
    pg, url, src = browser_page
    pg.goto(url.replace("/svg.html", "/label.html"))
    pg.evaluate(src)
    return pg


def act(pg, **spec):
    return pg.evaluate("s => window.__pa(s)", spec)


def describe(pg, selector):
    d = act(pg, mode="describe", op="click", target={"selector": selector})
    assert d["ok"], d
    return d


@pytest.mark.parametrize("selector, control, reason", [
    ("#place-label", "place", "submit_input"),  # <label for> a hidden <input type=submit>
    ("#label-icon", "place", "submit_input"),  # an SVG icon inside that kind of label (it resolves to the label)
    ("#wrap-span", "wrapped", "submit_button"),  # plain text inside a label that wraps a hidden submit button
    ("#outside-label", "outside-pay", "submit_button"),  # a label for a button that joins its form by form=
])
def test_a_click_that_reaches_a_submit_through_a_label_is_gated_as_that_submit(page, selector, control, reason):
    d = describe(page, selector)
    assert d["element"]["activates"]["id"] == control
    assert reason in gate("click", d["element"])
    done = act(page, mode="do", op="click", target={"selector": selector}, fingerprint=d["fingerprint"])
    assert done["ok"] and done["dispatched"], done
    assert page.evaluate("window.log") == [f"submit:{control}"]  # the click did submit, through the label


def test_a_link_inside_a_label_and_a_checkbox_label_add_nothing(page):
    link = describe(page, "#tips-link")
    assert "activates" not in link["element"]  # a link inside a label keeps its own click
    assert gate("click", link["element"]) == []
    tips = describe(page, "#tips-label")
    assert tips["element"]["activates"]["id"] == "tips"  # a checkbox: described, but it submits nothing
    assert gate("click", tips["element"]) == []
    plain = describe(page, "#plain-label")
    assert plain["element"]["activates"]["id"] == "plain-btn" and gate("click", plain["element"]) == []


def test_pointing_the_label_at_another_control_after_the_ok_changes_the_fingerprint(page):
    d = describe(page, "#plain-label")
    page.evaluate("document.getElementById('plain-label').htmlFor = 'place'")
    done = act(page, mode="do", op="click", target={"selector": "#plain-label"}, fingerprint=d["fingerprint"])
    assert not done["ok"] and done["code"] == "element_not_found", done
    assert page.evaluate("window.log") == []
