"""recipe_of_day — daily recipe suggestion to the scheduler channel.

Pipeline:
1. Compute today's day-of-week + vibe (weeknight quick / weekend
   project / Sunday dinner).
2. Proposer LLM call: given the vibe, recently-shown picks, and a
   cuisine palette, propose N specific dish names that span different
   cuisines — bias toward variety and surprise gems over safe defaults.
3. Parallel `web_search(f"{dish} recipe")` for each proposed dish.
4. Selector LLM call: pick the best single-recipe page from each
   dish's search results.
5. Format + post to scheduler channel + persist for dedup.

File path kept at the legacy `mealie_recipe_of_day.py` location so the
scheduler reload is a drop-in; internal `name` is `recipe_of_day`.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ember.providers.base import Message

_log = logging.getLogger("ember.skills.recipe_of_day")

name = "recipe_of_day"
description = "Posts a day-of-week-aware recipe suggestion from the wider web."
tools = ["web_search"]
scheduled_runs = [
    {
        "cron": "0 11 * * *",
        "timezone": "America/Chicago",
        "function": "post_recipe",
        "name": "daily",
        "timeout_s": 180,
        "misfire_grace_s": 3600,
        "coalesce": True,
    },
]

_TZ = ZoneInfo("America/Chicago")
_HISTORY_DIR = Path(os.environ.get("EMBER_RECIPES_DIR", "/data/recipes"))
_DEDUP_LOOKBACK_DAYS = 21
_SEARCH_RESULT_COUNT = 10


def _picks_per_day() -> int:
    """How many recipes to surface per fire. Env-tunable; clamped to
    [1, 6] — past 6 you're outpacing the search-pool diversity."""
    raw = os.environ.get("EMBER_RECIPE_PICKS_PER_DAY", "3")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(6, n))


# DOW vibe — free-form prompt context for the proposer LLM. The
# proposer generates dish names from the vibe; the selector then picks
# the best recipe page from search results for each dish. No more
# hardcoded dish lists — the LLM owns dish identity, the search owns
# recipe-page quality.
_DOW_PROFILES: dict[int, str] = {
    0: "Monday — easy weeknight, ~30-45 min, family-friendly, low effort after a workday",
    1: "Tuesday — easy weeknight, ~30-45 min, crowd-pleaser, kid-friendly (Taco Tuesday vibes welcome)",
    2: "Wednesday — easy weeknight, midweek pick-me-up, ~30-45 min",
    3: "Thursday — easy weeknight, ~30-45 min, slightly more interesting than Mon-Wed",
    4: "Friday — fun weekend kickoff, family pizza/tacos/burgers/comfort food, can be a bit more festive",
    5: "Saturday — bigger weekend project, an hour-plus, something a bit more involved or adventurous",
    6: "Sunday — classic Sunday dinner, slow / batch-cook, leftovers welcome",
}

# Loose cuisine palette to nudge the proposer toward variety. Not
# mandatory — the proposer should rotate across these over time so
# the user gets surprise gems instead of the same five American/Italian
# defaults week after week.
_CUISINE_PALETTE = [
    "American (comfort, BBQ, regional)",
    "Italian (pasta, pizza, braises)",
    "Authentic Mexican / regional (birria, mole poblano, cochinita pibil, tinga, chilaquiles, pozole, carnitas, al pastor, enmoladas) — NOT generic Tex-Mex ground-beef-taco-night",
    "Korean (bibimbap, bulgogi, jjigae)",
    "Japanese (donburi, ramen, yakitori)",
    "Thai (curries, stir-fries, salads)",
    "Chinese (stir-fries, noodles, dumplings, regional)",
    "Vietnamese (pho, bun, banh mi, caramel pork)",
    "Indian (curries, biryani, dal)",
    "Lebanese / Middle Eastern (shawarma, kibbeh, mansaf, kafta, mezze)",
    "Mediterranean (Greek, Turkish, North African)",
    "French (braises, gratins, salads)",
    "Spanish / Portuguese (paella, romesco, piri-piri)",
    "Caribbean / Latin American (arepas, jerk, ropa vieja)",
    "Ethiopian / East African (doro wat, misir wat)",
    "Filipino (adobo, sinigang, lumpia)",
]


_PROPOSER_SYSTEM_TEMPLATE = """\
You are proposing {n} specific dish names for a family dinner at home.
You will receive today's day-of-week vibe, a
list of dishes shown in the past few weeks (DO NOT repeat), and a
cuisine palette to draw from.

Rules for the {n} dishes:
- Each MUST be a specific, named dish — not a category. "Korean beef
  bowl" yes; "an Asian dish" no. "Lamb shawarma" yes; "Middle Eastern
  food" no. The name should be specific enough that searching
  `"{{dish}} recipe"` reliably returns a single recipe page on a
  reputable food site.
- Each MUST be a complete meal — not a sauce, marinade, side, dessert,
  or component.
- Each MUST fit the day's vibe (weeknight time budget, weekend project
  scale, Sunday-dinner feel, etc.).
- The {n} dishes MUST be distinct — different cuisines, proteins, or
  formats. Don't propose two pastas, two chicken dishes, or two
  pizzas in the same fire.
- DO NOT repeat anything in the recently-shown list. Avoid close
  variations too (e.g. if "chicken parmesan" was shown last week,
  don't propose "chicken piccata" today — pick something farther
  afield).
- Aim for variety ACROSS time, not just within today. Use the
  cuisine palette as inspiration. The user enjoys gems they wouldn't
  have thought of themselves — bias toward dishes that feel like a
  small surprise rather than the same five American/Italian defaults.
  Lebanese / Middle Eastern and authentic regional Mexican (birria,
  mole, cochinita pibil, tinga, chilaquiles, etc. — NOT generic
  Tex-Mex ground-beef tacos) are known favorites; weave them in
  regularly without overdoing either.
- Prefer dishes that are well-documented on reputable food sites
  (NYT Cooking, Serious Eats, Smitten Kitchen, Bon Appetit, Food52,
  Half Baked Harvest, Saveur, ATK / Cook's Illustrated, Maangchi,
  Hetty McKinnon, Half Baked Harvest, etc.) — that way the search
  step has good options to pick from.

Return ONE JSON object. No prose. No code fences:

{{
  "dishes": [
    {{
      "name": "<specific dish name, e.g. 'Chicken Shawarma Bowls'>",
      "cuisine": "<one short label, e.g. 'Lebanese', 'Italian'>",
      "rationale": "<one short sentence on why it fits today's vibe>"
    }},
    ...exactly {n} entries...
  ]
}}
"""


_SELECTOR_SYSTEM_TEMPLATE = """\
You are picking the best recipe page for each of {n} dishes that were
already chosen. For each dish you'll receive a list of web search
results; pick exactly ONE result per dish.

Rules for each pick:
- Pick a single-recipe page, not a roundup/listicle. Strongly prefer
  reputable food sites (NYT Cooking, Serious Eats, Smitten Kitchen,
  Bon Appetit, Food52, Half Baked Harvest, Saveur, ATK / Cook's
  Illustrated, Maangchi, RecipeTin Eats, etc.) over content farms.
  If the only options are listicles, pick the best listicle and
  reflect that in the title.
- The pick MUST be a complete recipe for the dish — not a related
  sauce, marinade, or component page.
- If no result for a dish is acceptable, omit it from the output —
  don't pad with bad picks.

Return ONE JSON object. No prose. No code fences:

{{
  "picks": [
    {{
      "title": "<recipe title, cleaned up — strip site suffixes like ' - NYT Cooking'>",
      "url": "<the recipe URL, verbatim from the search results>",
      "blurb": "<1-2 sentences pitching why this fits today's vibe — concrete, no filler>",
      "source": "<short site name, e.g. 'Smitten Kitchen', 'NYT Cooking'>"
    }},
    ...up to {n} entries (one per dish, in input order; omit dishes with no good result)...
  ]
}}
"""


async def _search_one(ctx, query: str) -> list[dict[str, Any]]:
    res = await ctx.tools.call_tool("web_search", {"query": query, "count": _SEARCH_RESULT_COUNT})
    if res.is_error:
        _log.warning("recipe_of_day: web_search(%r) failed: %s", query, res.output)
        return []
    return res.output.get("results", []) or []


def _load_recent(today: datetime.date) -> list[dict[str, str]]:
    """Flatten history files into a list of past picks. Reads both the
    new {picks: [...]} shape and the legacy single-pick shape so any
    pre-existing files remain usable."""
    out: list[dict[str, str]] = []
    if not _HISTORY_DIR.exists():
        return out
    for back in range(1, _DEDUP_LOOKBACK_DAYS + 1):
        d = today - datetime.timedelta(days=back)
        path = _HISTORY_DIR / f"{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            _log.warning("recipe_of_day: could not read %s: %s", path, e)
            continue
        picks = payload.get("picks")
        if isinstance(picks, list):
            for p in picks:
                if p.get("title") and p.get("url"):
                    out.append({"title": p["title"], "url": p["url"], "date_shown": d.isoformat()})
        elif payload.get("title") and payload.get("url"):
            out.append(
                {
                    "title": payload["title"],
                    "url": payload["url"],
                    "date_shown": d.isoformat(),
                }
            )
    return out


def _save_picks(today: datetime.date, picks: list[dict[str, Any]]) -> None:
    try:
        _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = _HISTORY_DIR / f"{today.isoformat()}.json"
        path.write_text(json.dumps({"date": today.isoformat(), "picks": picks}, indent=2))
    except OSError as e:
        _log.warning("recipe_of_day: could not save manifest: %s", e)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _resolve_provider(ctx):
    try:
        return ctx.router.resolve("claude_cli")
    except Exception:
        try:
            return ctx.router.resolve_cheap_tier()
        except Exception as e:
            _log.warning("recipe_of_day: no LLM route available: %s", e)
            return None


async def _llm_call(ctx, system: str, user_msg: str) -> dict[str, Any] | None:
    resolved = _resolve_provider(ctx)
    if resolved is None:
        return None
    try:
        result = await resolved.provider.complete(
            system=system,
            messages=[Message(role="user", content=user_msg)],
            tools=None,
            model=resolved.model,
            session_id=None,
            stream_handler=None,
        )
    except Exception as e:
        _log.warning("recipe_of_day: LLM call failed: %s", e)
        return None
    text = _strip_fences(result.text or "")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        _log.warning("recipe_of_day: LLM did not return valid JSON: %s; head=%r", e, text[:200])
        return None
    return obj if isinstance(obj, dict) else None


async def _propose_dishes(
    ctx, vibe: str, recent: list[dict[str, str]], n: int
) -> list[dict[str, Any]]:
    user_msg = (
        f"## Today\n{vibe}\n\n"
        f"## Recently shown (do NOT repeat or closely echo these)\n"
        f"```json\n{json.dumps(recent, indent=2)}\n```\n\n"
        f"## Cuisine palette (rotate across these over time for variety)\n"
        + "\n".join(f"- {c}" for c in _CUISINE_PALETTE)
    )
    obj = await _llm_call(ctx, _PROPOSER_SYSTEM_TEMPLATE.format(n=n), user_msg)
    if not obj:
        return []
    raw = obj.get("dishes")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for d in raw:
        if isinstance(d, dict) and d.get("name"):
            out.append(d)
    return out[:n]


async def _select_recipes(
    ctx, vibe: str, dishes: list[dict[str, Any]], pools: list[list[dict[str, Any]]], n: int
) -> list[dict[str, Any]]:
    dish_blocks = []
    for d, results in zip(dishes, pools, strict=False):
        dish_blocks.append(
            {
                "dish": d.get("name"),
                "cuisine": d.get("cuisine"),
                "results": results,
            }
        )
    user_msg = (
        f"## Today\n{vibe}\n\n"
        f"## Dishes to find recipes for\n"
        f"```json\n{json.dumps(dish_blocks, indent=2)}\n```"
    )
    obj = await _llm_call(ctx, _SELECTOR_SYSTEM_TEMPLATE.format(n=n), user_msg)
    if not obj:
        return []
    raw = obj.get("picks")
    if not isinstance(raw, list):
        return []
    picks: list[dict[str, Any]] = []
    for p in raw:
        if isinstance(p, dict) and p.get("title") and p.get("url"):
            picks.append(p)
    return picks[:n]


def _format_body(dow_name: str, picks: list[dict[str, Any]]) -> str:
    if len(picks) == 1:
        p = picks[0]
        title = p.get("title") or "(untitled)"
        source = (p.get("source") or "").strip()
        blurb = (p.get("blurb") or "").strip()
        url = p.get("url") or ""
        body = f"🍽️ **Today's recipe ({dow_name}):** {title}"
        if source:
            body += f" _({source})_"
        if blurb:
            body += f"\n{blurb}"
        if url:
            body += f"\n{url}"
        return body

    lines = [f"🍽️ **{dow_name} dinner ideas — {len(picks)} picks:**", ""]
    for i, p in enumerate(picks, 1):
        title = p.get("title") or "(untitled)"
        source = (p.get("source") or "").strip()
        blurb = (p.get("blurb") or "").strip()
        url = p.get("url") or ""
        head = f"**{i}. {title}**"
        if source:
            head += f" _({source})_"
        lines.append(head)
        if blurb:
            lines.append(blurb)
        if url:
            lines.append(url)
        lines.append("")
    return "\n".join(lines).rstrip()


async def post_recipe(ctx) -> dict[str, Any]:
    now_local = datetime.datetime.now(_TZ)
    today = now_local.date()
    dow = now_local.weekday()
    dow_name = now_local.strftime("%A")
    vibe = _DOW_PROFILES[dow]
    n = _picks_per_day()

    recent = _load_recent(today)

    dishes = await _propose_dishes(ctx, vibe, recent, n)
    if not dishes:
        await ctx.notify("⚠️ recipe_of_day: proposer returned no dishes today")
        return {"posted": False, "reason": "no_dishes"}

    queries = [f"{d['name']} recipe" for d in dishes]
    search_tasks = [asyncio.create_task(_search_one(ctx, q)) for q in queries]
    pools = await asyncio.gather(*search_tasks)

    if not any(pools):
        await ctx.notify(f"⚠️ recipe_of_day: no search results for any of {queries}")
        return {"posted": False, "reason": "no_results", "queries": queries}

    picks = await _select_recipes(ctx, vibe, dishes, pools, n)
    if not picks:
        await ctx.notify("⚠️ recipe_of_day: selector produced no usable picks today")
        return {
            "posted": False,
            "reason": "no_selection",
            "dishes": [d.get("name") for d in dishes],
        }

    body = _format_body(dow_name, picks)
    await ctx.notify(body)
    _save_picks(today, picks)
    return {
        "posted": True,
        "count": len(picks),
        "titles": [p.get("title") for p in picks],
        "dishes": [d.get("name") for d in dishes],
    }
