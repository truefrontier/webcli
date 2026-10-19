"""Every URL webcli gmail touches. Fix this file first when a capture disagrees.

Gmail's filters have no API a browser session can use: gmail.googleapis.com's settings.filters rejects cookie auth
(google-gmail-cli probed it, 2026-09-10: HTTP 400 from every origin). So, as google-gmail-cli did, webcli gmail reads
the Settings > Filters table (table.cf, one tr.r7 per filter) and creates or deletes a filter by driving that page's
own dialogs (UI).

Legacy Gmail sends its actions as POST /mail/u/0/?...&act=<action>&at=<XSRF token>. Status of what follows:

  VERIFIED (by google-gmail-cli, live, 2026-09-10): the filters table and its selectors; the create and delete
    dialogs' labels; one filter created with these dialogs (the POST that carried act and at).
  GUESS (UNVERIFIED): the act values in WRITE_ACTS. google-gmail-cli's capture kept parameter names only, never
    values. Every guess fails closed: when Gmail uses another act, the guard blocks the write, nothing changes, and
    the error names the blocked act. Add the real value here only after a capture shows it is that filter write.
  UNKNOWN: whether the filters table renders with every POST blocked (reads allow GET/HEAD only). If `webcli gmail
    filters list` times out, run `python -m webcli.core.capture gmail --url https://mail.google.com/mail/u/0/#settings/filters`
    and allow a request here only once you know it is a read.
"""

from __future__ import annotations

APP_HOST = "mail.google.com"
APP_URL = "https://mail.google.com/mail/u/0/"
FILTERS_URL = APP_URL + "#settings/filters"
HOSTS = (APP_HOST,)
ACTION_PATH = "/mail/u/0/"  # where legacy Gmail POSTs its actions
# Query or form parameters that mark a legacy Gmail action (act), its XSRF token (at), or a sign-out. A GET carrying
# one is refused; a POST carrying one passes only as the confirmed command's one write.
ACTION_PARAMS = frozenset({"act", "at", "logout"})
WRITE_ACTS = {"create": frozenset({"cf"}), "delete": frozenset({"df"})}  # GUESS: see above

LOAD_TRIES = 60  # seconds the filters table gets to render
SETTLE_MS = 1_500  # after "Create a new filter" shows with no rows yet (an empty list)
DIALOG_TRIES = 30  # half-seconds the create dialog gets to open
SAVE_MS = 3_000  # after a create or delete, before the table is read again

# The filters dialogs' visible text (VERIFIED, google-gmail-cli).
CREATE_LINK = "Create a new filter"
CREATE_BUTTON = "Create filter"
CRITERIA_LABELS = {"from": "From", "to": "To", "subject": "Subject", "query": "Has the words",
                   "negatedQuery": "Doesn't have"}
HAS_ATTACHMENT = "Has attachment"
ACTION_LABELS = {"skipInbox": "Skip the Inbox (Archive it)", "markAsRead": "Mark as read", "star": "Star it",
                 "delete": "Delete it", "neverSpam": "Never send it to Spam"}
CONFIRM_BUTTONS = ("OK", "Delete", "Yes")
