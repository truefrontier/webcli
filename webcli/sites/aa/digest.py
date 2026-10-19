"""Rankings and digests over the Free Data API's LLM and TTS rows (aanalysis parity), plus the flat output rows.

Rankers add underscore fields to the model dicts they rank (_smart_fast_score, _pareto_intel_speed, _open_guess,
_cost_efficiency...). The open-weight call is a name/creator heuristic: the free tier has no license field.
"""

from __future__ import annotations

import math
from typing import Any

OPEN_WEIGHT_FAMILIES = {
    "llama", "qwen", "deepseek", "mistral", "gemma", "phi", "gpt-oss", "glm", "yi", "command-r", "mixtral",
    "codellama", "openchat", "vicuna", "falcon", "mpt", "stablelm", "solar", "kimi",
}
OPEN_WEIGHT_CREATORS = {
    "meta", "alibaba", "deepseek", "mistral ai", "mistral", "google", "microsoft", "tsinghua", "01.ai", "databricks",
    "stability ai", "eleutherai", "together", "z ai", "kimi",
}
PROPRIETARY_NAMES = ("gpt-4", "gpt-5", "gpt-6", "claude", "gemini-pro", "palm")
CODING_NAMES = ("code", "coder", "codestral", "deepseek")
PICK_CATEGORIES = ("local", "cheap-api", "frontier", "coding-agent")


def _get(model: dict, section: str, key: str) -> Any:
    return (model.get(section) or {}).get(key)


def creator_name(model: dict) -> str:
    creator = model.get("model_creator")
    return (creator.get("name") or "") if isinstance(creator, dict) else (creator or "")


def normalize(model: dict) -> dict:
    """A copy with model_creator_name (the creator object's name) and ci95 (TTS rows also spell it ci_95)."""
    out = {**model, "model_creator_name": creator_name(model)}
    if "ci_95" in out and "ci95" not in out:
        out["ci95"] = out["ci_95"]
    return out


def intelligence(model: dict) -> float | None:
    return _get(model, "evaluations", "artificial_analysis_intelligence_index")


def coding(model: dict) -> float | None:
    return _get(model, "evaluations", "artificial_analysis_coding_index")


def agentic(model: dict) -> float | None:
    return _get(model, "evaluations", "artificial_analysis_agentic_index")


def cost_per_task(model: dict) -> float | None:
    """artificial_analysis_intelligence_index_cost.cost_per_task.total_cost."""
    per_task = (model.get("artificial_analysis_intelligence_index_cost") or {}).get("cost_per_task")
    return per_task.get("total_cost") if isinstance(per_task, dict) else None


def tokens_per_second(model: dict) -> float | None:
    return _get(model, "performance", "median_output_tokens_per_second")


def time_to_first_token(model: dict) -> float | None:
    return _get(model, "performance", "median_time_to_first_token_seconds")


def input_price(model: dict) -> float | None:
    return _get(model, "pricing", "price_1m_input_tokens")


def output_price(model: dict) -> float | None:
    return _get(model, "pricing", "price_1m_output_tokens")


def is_open_weight(model: dict) -> bool:
    name = (model.get("name") or "").lower()
    creator = (model.get("model_creator_name") or "").lower()
    if any(family in name for family in OPEN_WEIGHT_FAMILIES):
        return True
    if any(pattern in creator for pattern in OPEN_WEIGHT_CREATORS):
        return "gpt-oss" in name or not any(prop in name for prop in PROPRIETARY_NAMES)
    return False


def _cap(ranked: list, limit: int | None) -> list:
    return ranked[:limit] if limit else ranked


def _median(values: list[float]) -> float:
    return sorted(values)[len(values) // 2]  # the upper median, as aanalysis


def _zscores(values: list[float]) -> list[float]:
    mean = sum(values) / len(values)
    std = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 or 1
    return [(v - mean) / std for v in values]


def filter_models(models: list[dict], *, creator: str | None = None, min_intelligence: float | None = None) -> list:
    out = []
    for model in map(normalize, models):
        if creator and creator.lower() not in model["model_creator_name"].lower():
            continue
        if min_intelligence is not None and (intelligence(model) is None or intelligence(model) < min_intelligence):
            continue
        out.append(model)
    return out


def rank_by_intelligence(models: list[dict], limit: int | None = None) -> list[dict]:
    return _cap(sorted((m for m in models if intelligence(m) is not None), key=intelligence, reverse=True), limit)


def pareto(models: list[dict], x_key: str, y_key: str) -> list[bool]:
    """Whether each model is on the frontier of x_key and y_key (both higher-is-better)."""
    flags = [True] * len(models)
    for i, a in enumerate(models):
        for j, b in enumerate(models):
            if i == j or not flags[j] or None in (a.get(x_key), a.get(y_key), b.get(x_key), b.get(y_key)):
                continue
            if b[x_key] >= a[x_key] and b[y_key] >= a[y_key] and (b[x_key] > a[x_key] or b[y_key] > a[y_key]):
                flags[i] = False
                break
    return flags


def rank_by_smart_fast(models: list[dict], limit: int | None = None) -> list[dict]:
    """z(intelligence) + z(log tokens/s) among models at or above the median intelligence; Pareto frontier marked."""
    scored = [(m, intelligence(m), tokens_per_second(m)) for m in models]
    scored = [(m, intel, tps) for m, intel, tps in scored if intel is not None and tps is not None and tps > 0]
    if not scored:
        return []
    median = _median([intel for _, intel, _ in scored])
    scored = [s for s in scored if s[1] >= median]
    scores = [a + b for a, b in zip(_zscores([s[1] for s in scored]), _zscores([math.log(s[2]) for s in scored]))]
    ranked = []
    for (model, intel, tps), score in sorted(zip(scored, scores), key=lambda pair: pair[1], reverse=True):
        model.update(_smart_fast_score=score, _intelligence_index=intel, _tokens_per_second=tps)
        ranked.append(model)
    for model, flag in zip(ranked, pareto(ranked, "_intelligence_index", "_tokens_per_second")):
        model["_pareto_intel_speed"] = flag
    return _cap(ranked, limit)


def rank_by_smart_cheap(models: list[dict], limit: int | None = None) -> list[dict]:
    """Intelligence per dollar among models at or above the median intelligence: per cost_per_task when present,
    else per blended price (in + 3*out) / 4."""
    scored = []
    for model in models:
        intel = intelligence(model)
        if intel is None:
            continue
        task = cost_per_task(model)
        if task is not None and task > 0:
            scored.append((model, intel, intel / task, "task"))
            continue
        in_price, out_price = input_price(model), output_price(model)
        if in_price is not None and out_price is not None and (blended := (in_price + 3 * out_price) / 4.0) > 0:
            scored.append((model, intel, intel / blended, "blended"))
    if not scored:
        return []
    median = _median([s[1] for s in scored])
    ranked = []
    for model, _, efficiency, basis in sorted((s for s in scored if s[1] >= median), key=lambda s: s[2],
                                              reverse=True):
        model.update(_cost_efficiency=efficiency, _cost_basis=basis)
        ranked.append(model)
    return _cap(ranked, limit)


def rank_by_coding(models: list[dict], limit: int | None = None) -> list[dict]:
    """By coding index; a model without one counts by intelligence when its name says code (or deepseek)."""
    scored = []
    for model in models:
        score = coding(model)
        if score is None and any(word in (model.get("name") or "").lower() for word in CODING_NAMES):
            score = intelligence(model)
        if score is not None:
            scored.append((model, score))
    return _cap([m for m, _ in sorted(scored, key=lambda s: s[1], reverse=True)], limit)


def open_digest(models: list[dict], limit: int | None = None) -> dict:
    """The top open-weight guesses, the top 5 proprietary for comparison, and the intelligence gap between the
    two leaders."""
    for model in models:
        model["_open_guess"] = is_open_weight(model)
    open_ranked = rank_by_intelligence([m for m in models if m["_open_guess"]], limit)
    proprietary = rank_by_intelligence([m for m in models if not m["_open_guess"]], 5)
    gap = None
    if open_ranked and proprietary:
        gap = intelligence(proprietary[0]) - intelligence(open_ranked[0])
    return {"open_models": open_ranked, "proprietary_comparison": proprietary, "intelligence_gap": gap}


def pick(models: list[dict], category: str) -> dict:
    """One recommendation and up to 4 runners-up (the raw model dicts, as aanalysis)."""
    if category == "local":
        ranked = rank_by_intelligence([m for m in models if is_open_weight(m)], 5)
    elif category == "cheap-api":
        ranked = rank_by_smart_cheap(models, 5)
    elif category == "frontier":
        ranked = rank_by_intelligence(models, 5)
    elif category == "coding-agent":
        ranked = rank_by_coding(models, 5)
    else:
        raise ValueError(f"Unknown category: {category}")
    return {"category": category, "recommendation": ranked[0] if ranked else None, "runners_up": ranked[1:5]}


def model_rows(models: list[dict]) -> list[dict]:
    """The flat output row per model (aanalysis's JSON/CSV shape)."""
    return [{
        "rank": i,
        "id": m.get("id"),
        "slug": m.get("slug"),
        "name": m.get("name", "?"),
        "creator": creator_name(m) or "?",
        "intelligence_index": intelligence(m),
        "coding_index": coding(m),
        "agentic_index": agentic(m),
        "tokens_per_second": tokens_per_second(m),
        "time_to_first_token_s": time_to_first_token(m),
        "input_price_per_1m": input_price(m),
        "output_price_per_1m": output_price(m),
        "cost_per_intel_task": cost_per_task(m),
        "open_guess": m.get("_open_guess"),
        "pareto_intel_speed": m.get("_pareto_intel_speed"),
    } for i, m in enumerate(models, 1)]


# TTS (the Speech Arena): elo and ci95 on the free tier; price_per_1m_characters and chars_per_second on Pro only.

def elo(model: dict) -> float | None:
    return model.get("elo")


def chars_per_second(model: dict) -> float | None:
    return model.get("chars_per_second")


def price_per_1m_chars(model: dict) -> float | None:
    return model.get("price_per_1m_characters")


def filter_tts(models: list[dict], *, creator: str | None = None, min_elo: float | None = None) -> list[dict]:
    out = []
    for model in map(normalize, models):
        if creator and creator.lower() not in model["model_creator_name"].lower():
            continue
        if min_elo is not None and (elo(model) is None or elo(model) < min_elo):
            continue
        out.append(model)
    return out


def rank_by_elo(models: list[dict], limit: int | None = None) -> list[dict]:
    return _cap(sorted((m for m in models if elo(m) is not None), key=elo, reverse=True), limit)


def rank_tts_smart_fast(models: list[dict], limit: int | None = None) -> list[dict]:
    """z(elo) + z(log chars/s) among models at or above the median elo. Fewer than 3 scored: by elo. None scored:
    the models with any speed, by elo."""
    scored = [(m, elo(m), chars_per_second(m)) for m in models]
    scored = [(m, e, cps) for m, e, cps in scored if e is not None and cps is not None and cps > 0]
    if not scored:
        return rank_by_elo([m for m in models if chars_per_second(m) is not None], limit)
    if len(scored) < 3:
        return _cap([m for m, _, _ in sorted(scored, key=lambda s: s[1], reverse=True)], limit)
    median = _median([s[1] for s in scored])
    scored = [s for s in scored if s[1] >= median]
    scores = [a + b for a, b in zip(_zscores([s[1] for s in scored]), _zscores([math.log(s[2]) for s in scored]))]
    ranked = []
    for (model, e, cps), score in sorted(zip(scored, scores), key=lambda pair: pair[1], reverse=True):
        model.update(_smart_fast_score=score, _elo=e, _speed=cps)
        ranked.append(model)
    return _cap(ranked, limit)


def rank_tts_smart_cheap(models: list[dict], limit: int | None = None) -> list[dict]:
    """Elo per dollar (per 1M characters) among models at or above the median elo."""
    scored = [(m, elo(m), price_per_1m_chars(m)) for m in models]
    scored = [(m, e, p) for m, e, p in scored if e is not None and p is not None and p > 0]
    if not scored:
        return []
    median = _median([s[1] for s in scored])
    ranked = []
    for model, e, price in sorted((s for s in scored if s[1] >= median), key=lambda s: s[1] / s[2], reverse=True):
        model.update(_cost_efficiency=e / price, _price=price)
        ranked.append(model)
    return _cap(ranked, limit)


def tts_rows(models: list[dict]) -> list[dict]:
    return [{
        "rank": m.get("rank") or i,
        "id": m.get("id", "?"),
        "slug": m.get("slug", "?"),
        "name": m.get("name", "?"),
        "creator": creator_name(m) or "?",
        "elo": elo(m),
        "ci95": m.get("ci95") or m.get("ci_95"),
        "price_per_1m_chars": price_per_1m_chars(m),
        "chars_per_second": chars_per_second(m),
    } for i, m in enumerate(models, 1)]
