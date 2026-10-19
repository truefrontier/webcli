"""`webcli aa`: Artificial Analysis LLM and TTS leaderboards from the Free Data API (aanalysis parity). Plain HTTPS
with an API key: no browser, no identity."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import click

from webcli import __version__
from webcli.core import config, output
from webcli.core.errors import ConfigError, RateLimited
from webcli.core.output import (
    EXIT_API,
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    agent_output_options,
    die,
    emit_json,
    emit_md,
    emit_rows,
    note,
    resolve_format,
    rows_table,
)
from webcli.sites.aa import api, digest

ATTRIBUTION = "Data provided by Artificial Analysis (artificialanalysis.ai)"
FIELDS = ["rank", "id", "slug", "name", "creator", "intelligence_index", "coding_index", "agentic_index",
          "tokens_per_second", "time_to_first_token_s", "input_price_per_1m", "output_price_per_1m",
          "cost_per_intel_task", "open_guess", "pareto_intel_speed"]
COMPACT = ("rank", "name", "creator", "intelligence_index")
COLUMNS = ["rank", "name", "creator", "intelligence_index", "coding_index", "tokens_per_second",
           "time_to_first_token_s", "input_price_per_1m", "output_price_per_1m", "cost_per_intel_task"]
TTS_FIELDS = ["rank", "id", "slug", "name", "creator", "elo", "ci95", "price_per_1m_chars", "chars_per_second"]
TTS_COMPACT = ("rank", "name", "creator", "elo")
TTS_COLUMNS = ["rank", "name", "creator", "elo", "ci95"]
NOTES = [
    "Artificial Analysis's Free Data API over plain HTTPS: no browser, no identity, no daemon.",
    f"API key: ${api.KEY_ENV}, else ~/.webcli/sites/aa/api_key (`webcli aa config set-key`). No key, or a rejected "
    "one, exits 7. The key is sent only in the x-api-key header and never printed.",
    "Responses are cached 6h in ~/.webcli/sites/aa/cache; --refresh fetches fresh.",
    "Model rows: rank, id, slug, name, creator, intelligence_index, coding_index, agentic_index, tokens_per_second, "
    "time_to_first_token_s, input_price_per_1m, output_price_per_1m, cost_per_intel_task, open_guess, "
    "pareto_intel_speed (null when the free tier lacks a value).",
    "`digest open`'s open_guess is a name/creator heuristic: the free tier has no license field.",
    "`digest pick` and `digest all` picks print the raw API model objects ({category, recommendation, runners_up}).",
    "TTS speed (chars_per_second) and price (price_per_1m_chars) are Pro-tier fields: without them "
    "`tts digest smart-fast` / `smart-cheap` exit 3.",
    "HTTP 429 exits 6 (error \"rate_limited\", resets_at from Retry-After or null); other HTTP or network errors "
    "exit 5.",
    f"Attribution: {ATTRIBUTION}.",
]
WRITES = [
    "Nothing on artificialanalysis.ai: every API call is a read-only GET.",
    "Locally: `config set-key` writes ~/.webcli/sites/aa/api_key (0600), and responses are cached under "
    "~/.webcli/sites/aa/cache.",
]


def _env() -> dict[str, str]:
    return {
        output.FORMAT_ENV: "json | table | text | md. Default output when --format is not given.",
        api.KEY_ENV: "The Artificial Analysis API key (wins over ~/.webcli/sites/aa/api_key).",
        config.HOME_ENV: "The data home (default ~/.webcli).",
    }


@click.group(cls=output.AgentGroup, name="aa")
@click.version_option(__version__, prog_name="webcli aa")
def group() -> None:
    """Artificial Analysis LLM and TTS leaderboards (Free Data API; an API key, no browser)."""
    output.env_format()


def _fetch(load: Callable[..., list[dict]], refresh: bool) -> list[dict]:
    """The API's rows, normalized; API failures exit with their code."""
    try:
        return [digest.normalize(m) for m in load(refresh=refresh)]
    except ConfigError as exc:
        die(str(exc), EXIT_CONFIG, hint=exc.hint)
    except RateLimited as exc:
        die(exc.message, EXIT_RATE_LIMITED, error="rate_limited", resets_at=exc.resets_at,
            hint="Don't retry before resets_at (when null, wait a few minutes). Cached results still work.")
    except api.APIError as exc:
        die(str(exc), EXIT_API, hint="Check the network and retry.")


def _models(refresh: bool) -> list[dict]:
    return _fetch(api.llm_models, refresh)


def _tts(refresh: bool) -> list[dict]:
    return _fetch(api.tts_models, refresh)


def _emit(rows: list[dict], title: str, opts: dict, columns: list[str] = COLUMNS) -> None:
    """LLM rows in the chosen format, then the attribution (on a terminal). `opts`: agent_output_options' values."""
    emit_rows(rows, title=title, fields=FIELDS, compact_fields=COMPACT, columns=columns, noun="models", **opts)
    note(ATTRIBUTION, quiet=opts["quiet"])


def _emit_tts(models: list[dict], title: str, opts: dict, *always: str) -> None:
    """TTS rows; the Pro-tier price and speed columns show when a row has them (or `always`)."""
    rows = digest.tts_rows(models)
    columns = [*TTS_COLUMNS, *(c for c in ("price_per_1m_chars", "chars_per_second")
                               if c in always or any(r[c] for r in rows))]
    emit_rows(rows, title=title, fields=TTS_FIELDS, compact_fields=TTS_COMPACT, columns=columns, noun="models", **opts)
    note(ATTRIBUTION, quiet=opts["quiet"])


limit_option = click.option("--limit", type=click.IntRange(min=1), default=None, help="At most this many models.")
refresh_option = click.option("--refresh", is_flag=True, help="Skip the 6h cache and fetch fresh.")


def _digest_limit(default: int) -> Callable:
    return click.option("--limit", type=click.IntRange(min=1), default=default, show_default=True,
                        help="At most this many models.")


@group.command("models")
@limit_option
@click.option("--creator", default=None, help="Only models whose creator name contains this (case-insensitive).")
@click.option("--min-intel", type=float, default=None, help="Minimum intelligence index.")
@refresh_option
@agent_output_options()
def models_cmd(limit, creator, min_intel, refresh, **opts) -> None:
    """LLM models, by intelligence index (models without one are left out)."""
    models = digest.filter_models(_models(refresh), creator=creator, min_intelligence=min_intel)
    _emit(digest.model_rows(digest.rank_by_intelligence(models, limit)), "LLM Models", opts)
    raise SystemExit(EXIT_OK)


@group.group("digest")
def digest_group() -> None:
    """Ranked LLM digests: smartest, smart-fast, smart-cheap, open, coding, pick, all."""


@digest_group.command("smartest")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def digest_smartest(limit, refresh, **opts) -> None:
    """Top models by intelligence index."""
    rows = digest.model_rows(digest.rank_by_intelligence(_models(refresh), limit))
    _emit(rows, f"Top {limit} Smartest Models", opts)
    raise SystemExit(EXIT_OK)


@digest_group.command("smart-fast")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def digest_smart_fast(limit, refresh, **opts) -> None:
    """Top models by intelligence + speed (z-scores, at or above median intelligence); pareto_intel_speed marks the
    frontier."""
    rows = digest.model_rows(digest.rank_by_smart_fast(_models(refresh), limit))
    _emit(rows, f"Top {limit} Smart & Fast Models", opts, columns=[*COLUMNS, "pareto_intel_speed"])
    raise SystemExit(EXIT_OK)


@digest_group.command("smart-cheap")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def digest_smart_cheap(limit, refresh, **opts) -> None:
    """Top models by intelligence per dollar (cost per task, else blended price), at or above median
    intelligence."""
    rows = digest.model_rows(digest.rank_by_smart_cheap(_models(refresh), limit))
    _emit(rows, f"Top {limit} Smart & Cheap Models", opts)
    raise SystemExit(EXIT_OK)


@digest_group.command("open")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def digest_open(limit, refresh, **opts) -> None:
    """Top open-weight models (a heuristic guess). On a terminal, also the top 5 proprietary and the gap."""
    result = digest.open_digest(_models(refresh), limit)
    columns = [*COLUMNS, "open_guess"]
    _emit(digest.model_rows(result["open_models"]), f"Top {limit} Open-Weight Models (Heuristic)",
          opts, columns=columns)
    if not opts["as_csv"] and resolve_format(opts["fmt"]) == "table":
        rows_table("Top Proprietary Models for Comparison", digest.model_rows(result["proprietary_comparison"]),
                   columns)
        if result["intelligence_gap"] is not None:
            click.echo(f"Intelligence gap (proprietary - open): {result['intelligence_gap']:.1f}")
    raise SystemExit(EXIT_OK)


@digest_group.command("coding")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def digest_coding(limit, refresh, **opts) -> None:
    """Top models for coding (coding index; else intelligence for code-named models)."""
    rows = digest.model_rows(digest.rank_by_coding(_models(refresh), limit))
    _emit(rows, f"Top {limit} Coding Models", opts)
    raise SystemExit(EXIT_OK)


@digest_group.command("pick")
@click.argument("category", type=click.Choice(digest.PICK_CATEGORIES))
@refresh_option
@agent_output_options(rows=False)
def digest_pick(category, refresh, fmt, quiet) -> None:
    """One recommendation and runners-up for CATEGORY. JSON: {category, recommendation, runners_up}."""
    result = digest.pick(_models(refresh), category)
    out = resolve_format(fmt)
    if out == "json":
        emit_json(result)
    else:
        ranked = [result["recommendation"], *result["runners_up"]] if result["recommendation"] else []
        rows = digest.model_rows(ranked)
        if out == "md":
            emit_md([{k: r[k] for k in COLUMNS} for r in rows])
        else:
            rows_table(f"Pick for {category} (1 = recommendation)", rows, COLUMNS)
        note(ATTRIBUTION if ranked else f"No recommendation for {category}.", quiet=quiet)
    raise SystemExit(EXIT_OK)


@digest_group.command("all")
@_digest_limit(5)
@refresh_option
@agent_output_options(formats=("json",), rows=False)
def digest_all(limit, refresh, fmt, quiet) -> None:
    """Every LLM digest in one JSON object (--limit per digest), with all four picks."""
    models = _models(refresh)
    emit_json({
        "smartest": digest.model_rows(digest.rank_by_intelligence(models, limit)),
        "smart_fast": digest.model_rows(digest.rank_by_smart_fast(models, limit)),
        "smart_cheap": digest.model_rows(digest.rank_by_smart_cheap(models, limit)),
        "coding": digest.model_rows(digest.rank_by_coding(models, limit)),
        "open": digest.model_rows(digest.open_digest(models, limit)["open_models"]),
        "picks": {category.replace("-", "_"): digest.pick(models, category) for category in digest.PICK_CATEGORIES},
    })
    raise SystemExit(EXIT_OK)


@group.group("tts")
def tts_group() -> None:
    """Text-to-speech models (the Speech Arena): models, digest."""


@tts_group.command("models")
@limit_option
@click.option("--creator", default=None, help="Only models whose creator name contains this (case-insensitive).")
@click.option("--min-elo", type=float, default=None, help="Minimum Elo rating.")
@refresh_option
@agent_output_options()
def tts_models_cmd(limit, creator, min_elo, refresh, **opts) -> None:
    """TTS models, by Elo."""
    ranked = digest.rank_by_elo(digest.filter_tts(_tts(refresh), creator=creator, min_elo=min_elo), limit)
    _emit_tts(ranked, "TTS Models", opts)
    raise SystemExit(EXIT_OK)


@tts_group.group("digest")
def tts_digest_group() -> None:
    """Ranked TTS digests: smartest, smart-fast, smart-cheap, all."""


@tts_digest_group.command("smartest")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def tts_smartest(limit, refresh, **opts) -> None:
    """Top TTS models by Elo (quality)."""
    ranked = digest.rank_by_elo(_tts(refresh), limit)
    _emit_tts(ranked, f"Top {limit} TTS Models by Elo", opts)
    raise SystemExit(EXIT_OK)


@tts_digest_group.command("smart-fast")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def tts_smart_fast(limit, refresh, **opts) -> None:
    """Top TTS models by quality + speed. Exit 3 when no model has speed data (a Pro-tier field)."""
    ranked = digest.rank_tts_smart_fast(_tts(refresh), limit)
    if not ranked:
        die("No TTS speed data: chars_per_second isn't on the free tier.", EXIT_NOT_FOUND,
            hint="Speed metrics need an Artificial Analysis Pro key; `webcli aa tts digest smartest` ranks by Elo.")
    _emit_tts(ranked, f"Top {limit} Smart & Fast TTS Models", opts, "chars_per_second")
    raise SystemExit(EXIT_OK)


@tts_digest_group.command("smart-cheap")
@_digest_limit(10)
@refresh_option
@agent_output_options()
def tts_smart_cheap(limit, refresh, **opts) -> None:
    """Top TTS models by Elo per dollar. Exit 3 when no model has pricing (a Pro-tier field)."""
    ranked = digest.rank_tts_smart_cheap(_tts(refresh), limit)
    if not ranked:
        die("No TTS pricing data: price_per_1m_characters isn't on the free tier.", EXIT_NOT_FOUND,
            hint="Pricing needs an Artificial Analysis Pro key; `webcli aa tts digest smartest` ranks by Elo.")
    _emit_tts(ranked, f"Top {limit} Cost-Efficient TTS Models", opts, "price_per_1m_chars")
    raise SystemExit(EXIT_OK)


@tts_digest_group.command("all")
@_digest_limit(5)
@refresh_option
@agent_output_options(formats=("json",), rows=False)
def tts_all(limit, refresh, fmt, quiet) -> None:
    """Every TTS digest in one JSON object (--limit per digest; smart_fast / smart_cheap are [] on the free tier)."""
    models = _tts(refresh)
    emit_json({
        "smartest": digest.tts_rows(digest.rank_by_elo(models, limit)),
        "smart_fast": digest.tts_rows(digest.rank_tts_smart_fast(models, limit)),
        "smart_cheap": digest.tts_rows(digest.rank_tts_smart_cheap(models, limit)),
    })
    raise SystemExit(EXIT_OK)


@group.group("config")
def config_group() -> None:
    """The Artificial Analysis API key."""


@config_group.command("set-key")
@click.option("--key-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Read the key from this file (default: stdin; a hidden prompt on a terminal).")
def config_set_key(key_file: Path | None) -> None:
    """Save the API key to ~/.webcli/sites/aa/api_key (0600). Prints {saved: path}, never the key."""
    if key_file:
        key = key_file.read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        key = click.prompt("Artificial Analysis API key", hide_input=True, err=True)
    else:
        key = sys.stdin.read()
    try:
        path = api.save_key(key)
    except ConfigError as exc:
        die(str(exc), EXIT_CONFIG, hint=exc.hint)
    emit_json({"saved": str(path)})
    if os.environ.get(api.KEY_ENV, "").strip():
        output.warn(f"${api.KEY_ENV} is set and wins over the saved key.")
    raise SystemExit(EXIT_OK)


group.add_command(output.agent_help_command(group, version=__version__, env=_env, notes=NOTES, writes=WRITES))
