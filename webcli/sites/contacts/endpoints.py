"""Every URL and rpcid webcli contacts touches. Fix this file first when a capture disagrees.

contacts.google.com is a Google "boq" web app (ContactsUi). Its data comes from one endpoint:

  batchexecute  POST /_/ContactsUi/data/batchexecute?rpcids=<id>&source-path=...&bl=...&f.sid=...
                form body f.req=[[[rpcid, "<payload json>", null, "generic"]]] & at=<XSRF token>
                answer: )]}' then length-prefixed frames of ["wrb.fr", rpcid, "<payload json>", ...]

fetch.py calls it from inside the page (COOKIE): the `at` token (WIZ_global_data.SNlM0e) and session id (FdrFJe) stay
in the page, and Python gets only the answer. The guard is the only Python that sees an outgoing body; it reads the
rpcids (and, for a confirmed label write, the payload) in memory and never stores or prints it.

The page does not call people-pa.clients6.google.com (the public People API host). Besides batchexecute it POSTs to
peoplestack-pa.clients6.google.com (autocomplete, ranking), play.google.com/log, /_/ContactsUi/jserror and
/browserinfo. webcli contacts needs none of them; the guard aborts them all.

Status: every rpcid below comes from gcontacts (google-contacts-cli), which observed each read live on 2026-10-18
(build boq_contactsuiserver_20261010) and captured the two write payloads from the UI's own requests with its guard
aborting them. Service names come from the app's JS bundle ({Ab: "<rpcid>", Db: "/<Service>.<Method>"}). Not yet
run live with webcli: `python -m webcli.core.capture contacts` after `webcli identity login google:default` shows what
the guard blocked.
"""

from __future__ import annotations

APP_HOST = "contacts.google.com"
APP_URL = "https://contacts.google.com/"
HOSTS = (APP_HOST,)
RPC_PREFIX = "/_/ContactsUi/data/"
BATCH_PATH = RPC_PREFIX + "batchexecute"

# The reads, by what they do. Payload -> answer, as gcontacts saw them. `bl` is the page's build label
# (WIZ_global_data.cfb2h); rows are Person messages as JSPB arrays (parse.py).
RPC = {
    # ContactsDataService.ContactsList: [page_token|null, null, null, 1, 0] -> [[row, ...], null, next_token, ...];
    # 50 rows on the first page, then up to 1000.
    "contacts": "YQqBqc",
    # ContactsDataService.ContactsArchivedList ("Other contacts"): [page_token|null, null, 1, 0] -> [[row, ...],
    # next_token]; "" (or nothing) at the end.
    "other": "tAERjb",
    # CustardService.ListBucketedSearchResults: [[[1, 3, bl]], query, 1, 1, 1, 1] -> [[contact row, ...] | null,
    # [other-contact row, ...] | null, ...]
    "search": "fcwrWc",
    # ContactsDataService.ContactsGetPeople: [[id, ...], null, 1, 0] -> [[row, ...]]; an unknown id answers with an
    # empty shell.
    "get": "rptSGc",
    # CustardService.ListUserContactGroups: [[[1, 3, bl]]] -> [[[group_id, name, member_count, is_system], ...]]
    "labels": "GKlgB",
}
# Fail closed: a batchexecute POST passes only when every rpcid in it is one of these (or, for `label add|remove
# --confirm` alone, a write its WriteScope allows).
READ_RPCIDS = frozenset(RPC.values())

# The only writes: label membership, sent only by `webcli contacts label add|remove ... --confirm`.
WRITE_RPC = {
    # ContactsDataService.ContactsEditGroupMembership:
    # [[contact key, ...], null, [group id to add] | null, [group id to remove] | null, null, null, 1, 0]
    # A contact key is the contact id's number in lowercase hex (c123 -> "7b"). The UI sends up to 1000 per call.
    "membership": "ls96Jc",
    # ContactsDataService.ContactsCreateGroup: [name, null, null, null, 1, 0] (field 3 would add members; unused)
    "create_label": "ouU0xd",
}
WRITE_RPCIDS = frozenset(WRITE_RPC.values())
# Known writes webcli never sends (from the same bundle). Blocked like every unlisted rpcid; listed so tests can
# assert none of them ever passes, even with a write scope.
OTHER_WRITES = {
    "Loo3me": "ContactsDeleteGroup (delete a label; its field 2 would delete the contacts too)",
    "YupeGb": "ContactsRenameGroup",
    "BzamJc": "ContactsUpdatePerson",
    "sh7VMd": "UpdateContact",
    "Mrk0Id": "CreateContacts",
    "E9cpuf": "DeleteContacts",
    "mQrNLe": "ContactsDeleteContacts",
    "jaNuCb": "ContactsMergePeople",
    "cqJZIe": "ContactsStarPerson",
    "iDnZUe": "StarContact",
    "mbvsbc": "ContactsAddToContacts",
    "LQU64d": "AddToMyContacts",
    "Dps7ke": "ContactsImportFile",
    "wyg0ef": "ContactsUpdatePrefs (the page sends it on every load: blocked)",
    "H0kBkc": "UpdateUserSettings",
    "axvkKb": "ContactsRevertMutation",
}
MEMBERSHIP_BATCH = 200  # contacts per ls96Jc call (the UI allows 1000; smaller keeps a failure's blast radius small)
BATCH_PAUSE = (1.0, 2.5)  # seconds between membership calls (random in range)
MAX_KEYS = 1000  # the most contact keys one ls96Jc call may carry

# System groups GKlgB doesn't list but memberships use.
SYSTEM_GROUP_NAMES = {"starred": "Starred", "myContacts": "My Contacts"}
HIDDEN_GROUPS = frozenset({"myContacts"})  # every saved contact has it; rows leave it out of `labels`

MAX_PAGES = 200  # 50 + 199 * 1000 contacts is far past Google's 25,000-contact cap
TOKEN_SECONDS = 20  # how long the app gets to put its `at` token on the page after load
GET_CHUNK = 200  # ids per rptSGc read
