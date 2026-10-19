"""`webcli photos`: your Google Photos library, read-only, from your own signed-in, hidden Chrome
(google-photos-cli parity). `download` saves the original file."""

from __future__ import annotations

import click

from webcli.core.output import EXIT_AUTH, EXIT_OK, agent_output_options, die, emit_csv, emit_rows, select_fields
from webcli.sites import data
from webcli.sites.photos import ADAPTER as SITE
from webcli.sites.photos import parse

PHOTO_FIELDS = ["id", "kind", "taken_at", "label", "url"]
PHOTO_COMPACT = ("id", "kind", "taken_at")
PHOTO_COLUMNS = ["id", "kind", "taken_at"]
ALBUM_FIELDS = ["id", "title", "url"]
ALBUM_COMPACT = ("id", "title")
INFO_COMPACT = ("id", "kind", "taken_at", "url")
NOTES = [
    "Identity google:default (the profile `webcli identity login google:default` signs in, through gemini). Commands "
    "run one at a time in that identity's daemon, a hidden headless Chrome: no window, no Dock icon, no focus change.",
    "Everything is read off photos.google.com's rendered page, as google-photos-cli did: list, search, and albums "
    "scroll the grid and collect the links (--limit stops early; the end of the grid stops it too).",
    "PHOTO is a media key (AF1Qip...) or a photos.google.com/photo/<key> URL; ALBUM likewise (/album/<key>).",
    "kind and taken_at come from the photo's label (\"Photo - Portrait - Nov 10, 2016, 11:22:34 AM\"), as Photos "
    "words it.",
    "`download` saves the ORIGINAL (a video's =dv original, else the photo's =d original) into -o DIR (default: the "
    "current directory, created if needed) under Google's file name, else <id>.<ext>. It replaces a file of that "
    "name; the file is mode 0600.",
    "No photos, albums, or results exits 3; when the page should have had some, the error names what the request "
    "guard blocked.",
    "Not signed in exits 4: a person runs `webcli identity login google:default`.",
]
WRITES = ["Nothing on Google Photos: no upload, edit, album change, favorite, archive, trash, or share. The guard "
          "aborts every batchexecute rpc that isn't a known read. `download` writes only the local file you asked "
          "for."]

group = data.site_group(SITE, "Google Photos, read-only (unofficial). Identity google:default.")


def _key(parser):
    def callback(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return parser(value)
        except ValueError as exc:
            raise click.BadParameter(str(exc)) from None

    return callback


def _emit_photos(rows: list[dict], title: str, id_only: bool, opts: dict) -> None:
    if id_only:
        for row in rows:
            click.echo(row["id"])
        return
    emit_rows(rows, title=title, fields=PHOTO_FIELDS, compact_fields=PHOTO_COMPACT, columns=PHOTO_COLUMNS,
              noun="photos", **opts)


id_only_option = click.option("--id-only", is_flag=True, help="Print only ids, one per line.")
limit_option = click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True,
                            help="At most this many photos.")


@group.command("status")
@agent_output_options(rows=False)
def status_cmd(fmt, quiet) -> None:
    """Whether google:default is signed in to Google Photos (exit 4 if not)."""
    info = data.call(SITE, "status")
    data.emit_record("Google Photos", info, fmt)
    if not info["signedIn"]:
        die("Not signed in to Google Photos.", EXIT_AUTH, hint=data.login_hint())
    raise SystemExit(EXIT_OK)


@group.command("albums")
@id_only_option
@agent_output_options()
def albums_cmd(id_only, **opts) -> None:
    """Your albums and collections."""
    rows = data.call(SITE, "albums")
    if id_only:
        for row in rows:
            click.echo(row["id"])
    else:
        emit_rows(rows, title="Albums", fields=ALBUM_FIELDS, compact_fields=ALBUM_COMPACT, columns=["id", "title"],
                  noun="albums", **opts)
    raise SystemExit(EXIT_OK)


@group.command("list")
@click.option("--album", default=None, callback=_key(parse.album_id), help="An album id or URL.")
@limit_option
@id_only_option
@agent_output_options()
def list_cmd(album, limit, id_only, **opts) -> None:
    """Photos in an album, or your library's most recent without --album."""
    rows = data.call(SITE, "photos", album=album, limit=limit)
    _emit_photos(rows, "Photos", id_only, opts)
    raise SystemExit(EXIT_OK)


@group.command("search")
@click.argument("query")
@limit_option
@id_only_option
@agent_output_options()
def search_cmd(query, limit, id_only, **opts) -> None:
    """Google Photos' own search over your library (people, places, things, text). Exit 3 when nothing matches."""
    rows = data.call(SITE, "photos", query=query, limit=limit)
    _emit_photos(rows, f"Search: {query}", id_only, opts)
    raise SystemExit(EXIT_OK)


@group.command("info")
@click.argument("photo", callback=_key(parse.photo_id))
@agent_output_options()
def info_cmd(photo, fmt, compact, select, quiet, as_csv) -> None:
    """One photo or video: kind, taken_at, label, url. Exit 3 if Photos doesn't show it."""
    found = data.call(SITE, "info", photo)
    payload = select_fields({k: found[k] for k in INFO_COMPACT} if compact else found, select)
    if as_csv:
        emit_csv([payload])
    else:
        data.emit_record(found["label"] or photo, payload, fmt)
    raise SystemExit(EXIT_OK)


@group.command("download")
@click.argument("photo", callback=_key(parse.photo_id))
@click.option("-o", "--output", "out", default=".", show_default=True, help="Directory to save the original in.")
@agent_output_options(rows=False)
def download_cmd(photo, out, fmt, quiet) -> None:
    """Save the ORIGINAL photo or video file. Prints {id, path, bytes, mime}."""
    saved = data.call(SITE, "download", photo, data.out_dir(out))
    data.emit_record("Downloaded", saved, fmt)
    raise SystemExit(EXIT_OK)


data.add_agent_help(group, notes=NOTES, writes=WRITES)
