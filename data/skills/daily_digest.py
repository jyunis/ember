"""daily_digest — morning briefing email.

Fires at 5am America/Chicago year-round (DST-safe via per-spec timezone).

Pipeline: parallel fetch (weather, gmail per label, rss, recipe) →
single cheap-tier LLM call producing a structured JSON summary →
fixed HTML shell rendered around the summary → gmail_send to user.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
from html import escape
from pathlib import Path
from typing import Any

import httpx

from ember.providers.base import Message

_log = logging.getLogger("ember.skills.daily_digest")

name = "daily_digest"
description = "Renders and emails a morning briefing at 5am Central."
tools = ["gmail_list", "rss_fetch", "mealie_random", "calendar_list_events", "gmail_send"]
scheduled_runs = [
    {
        "cron": "0 5 * * *",
        "timezone": "America/Chicago",
        "function": "send_digest",
        "name": "morning",
        # Normal run finishes in ~2:35. send_digest now retries the full
        # pipeline on a delivery failure (see _RETRY_BACKOFFS_S) so a 5am
        # fire that lands inside a brief WAN/DNS outage still delivers once
        # the network returns. 1800s covers the worst-case backoff schedule
        # (~4 attempts + ~16 min of sleeps) without masking a genuine hang.
        "timeout_s": 1800,
        "misfire_grace_s": 3600,
        "coalesce": True,
    },
]

# Per-deployment settings come from the environment so the skill carries no
# personal data. DIGEST_LABELS is a comma-separated list of Gmail labels.
_RECIPIENT = os.environ.get("DIGEST_RECIPIENT", "")
_LABELS = [
    lbl.strip()
    for lbl in os.environ.get("DIGEST_LABELS", "INBOX").split(",")
    if lbl.strip()
]
_WEATHER_LAT = float(os.environ.get("DIGEST_WEATHER_LAT", "44.9778"))
_WEATHER_LON = float(os.environ.get("DIGEST_WEATHER_LON", "-93.2650"))
_WEATHER_PLACE = os.environ.get("DIGEST_WEATHER_PLACE", "Minneapolis")
_MEALIE_PUBLIC_URL = os.environ.get("MEALIE_PUBLIC_URL", "http://mealie.local").rstrip("/")
_PER_LABEL_LIMIT = 5
# Pull more raw items than we render so the LLM has slack to drop dupes
# against the recently-shown list without the news section shrinking.
_RSS_PER_FEED = 6
_RSS_DESC_MAX = 200

# Where per-day digest manifests live so future runs can dedupe against
# them (under the mounted data volume).
_BRIEFINGS_DIR = Path(os.environ.get("EMBER_BRIEFINGS_DIR", "/data/briefings"))
# How many prior days of digests to consider when deduping.
_DEDUP_LOOKBACK_DAYS = 3

_FEED_RE = re.compile(r"-\s+.+?—\s+(https?://\S+)")
_NWS_POINTS_URL = f"https://api.weather.gov/points/{_WEATHER_LAT},{_WEATHER_LON}"
# NWS requires a descriptive User-Agent or they'll block the request.
_NWS_HEADERS = {
    "User-Agent": f"ember-daily-digest/1.0 ({_RECIPIENT or 'self-hosted'})",
    "Accept": "application/geo+json",
}


# --- Data fetchers --------------------------------------------------------


def _extract_news_sources(profile: str) -> list[str]:
    if "## News sources" not in profile:
        return []
    chunk = profile.split("## News sources", 1)[1]
    end = chunk.find("\n## ")
    if end != -1:
        chunk = chunk[:end]
    return _FEED_RE.findall(chunk)


async def _fetch_weather() -> tuple[dict[str, Any] | None, Exception | None]:
    # NWS: two-step — resolve gridpoint, then fetch forecast.
    # No API key, no rate limits, US-only.
    # Single retry with 2s backoff on any failure.
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=25, headers=_NWS_HEADERS) as c:
                pts = await c.get(_NWS_POINTS_URL)
                pts.raise_for_status()
                forecast_url = pts.json()["properties"]["forecast"]
                fc = await c.get(forecast_url)
                fc.raise_for_status()
                return fc.json(), None
        except Exception as e:
            last_exc = e
            if attempt == 1:
                await asyncio.sleep(2)
    _log.warning("daily_digest: weather fetch failed after 2 attempts: %s", last_exc)
    return None, last_exc


async def _fetch_label(ctx, label: str) -> tuple[str, list[dict[str, Any]]]:
    res = await ctx.tools.call_tool(
        "gmail_list",
        {"label": label, "limit": _PER_LABEL_LIMIT, "unread_only": True, "since_days": 2},
    )
    if res.is_error:
        _log.warning("daily_digest: gmail_list(%s) failed: %s", label, res.output)
        return label, []
    return label, res.output.get("messages", [])


async def _fetch_rss(ctx, urls: list[str]) -> list[dict[str, Any]]:
    if not urls:
        return []
    res = await ctx.tools.call_tool(
        "rss_fetch",
        {"urls": urls, "limit_per_feed": _RSS_PER_FEED, "description_max_chars": _RSS_DESC_MAX},
    )
    if res.is_error:
        _log.warning("daily_digest: rss_fetch failed: %s", res.output)
        return []
    return res.output.get("feeds", [])


async def _fetch_recipe(ctx) -> dict[str, Any] | None:
    res = await ctx.tools.call_tool("mealie_random", {"count": 1})
    if res.is_error:
        return None
    items = res.output.get("items", [])
    return items[0] if items else None


try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ: datetime.tzinfo = ZoneInfo("America/Chicago")
except Exception:
    _LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-6))


def _local_day_window(today_local: datetime.date) -> tuple[str, str]:
    """[today 00:00 local, day-after-tomorrow 00:00 local] in UTC ISO.
    Window covers today + all of tomorrow."""
    start_local = datetime.datetime.combine(today_local, datetime.time(0, 0), _LOCAL_TZ)
    end_local = start_local + datetime.timedelta(days=2)
    return (
        start_local.astimezone(datetime.timezone.utc).isoformat(),
        end_local.astimezone(datetime.timezone.utc).isoformat(),
    )


async def _fetch_calendar(ctx, today_local: datetime.date) -> list[dict[str, Any]]:
    start_iso, end_iso = _local_day_window(today_local)
    res = await ctx.tools.call_tool(
        "calendar_list_events",
        {"start_iso": start_iso, "end_iso": end_iso},
    )
    if res.is_error:
        _log.warning("daily_digest: calendar_list_events failed: %s", res.output)
        return []
    return res.output.get("events", []) or []


def _bin_events_by_day(
    events: list[dict[str, Any]], today_local: datetime.date
) -> dict[str, list[dict[str, Any]]]:
    """Bucket events into 'today' and 'tomorrow' by local date of start_iso.
    All-day events use their date-only start verbatim; timed events convert
    UTC start_iso back to local."""
    today_str = today_local.isoformat()
    tomorrow_str = (today_local + datetime.timedelta(days=1)).isoformat()
    today_evs: list[dict[str, Any]] = []
    tomorrow_evs: list[dict[str, Any]] = []

    for ev in events:
        start_iso = ev.get("start_iso", "") or ""
        if not start_iso:
            continue
        if ev.get("all_day"):
            local_date_str = start_iso[:10]
            time_label = "All day"
        else:
            try:
                dt_utc = datetime.datetime.fromisoformat(start_iso)
                dt_local = dt_utc.astimezone(_LOCAL_TZ)
                local_date_str = dt_local.date().isoformat()
                time_label = dt_local.strftime("%-I:%M %p").lower()
            except (ValueError, TypeError):
                continue
        entry = {
            "time": time_label,
            "title": ev.get("title", "") or "(no title)",
            "calendar": ev.get("calendar", "") or "",
            "location": ev.get("location", "") or "",
            "all_day": bool(ev.get("all_day")),
            "_sort": start_iso,
        }
        if local_date_str == today_str:
            today_evs.append(entry)
        elif local_date_str == tomorrow_str:
            tomorrow_evs.append(entry)

    today_evs.sort(key=lambda e: (not e["all_day"], e["_sort"]))
    tomorrow_evs.sort(key=lambda e: (not e["all_day"], e["_sort"]))
    for e in today_evs + tomorrow_evs:
        e.pop("_sort", None)
    return {"today": today_evs, "tomorrow": tomorrow_evs}


# --- LLM editorial pass ---------------------------------------------------


_LLM_SYSTEM = """\
You are composing the editorial layer of a personal morning briefing email
for the user. You will receive raw data: weather, today + tomorrow's
calendar events, unread emails per label, RSS items grouped by feed, and
a recipe.

Return ONE JSON object. No prose. No code fences. The object must have
these exact keys:

{
  "greeting": "<one short sentence, warm but not saccharine>",
  "weather": "<one or two sentences summarizing today + tomorrow>",
  "calendar": {
    "today": [{"time": "<verbatim from input>", "title": "<verbatim title>",
               "location": "<verbatim or empty>", "all_day": <bool>}],
    "tomorrow": [{"time": "...", "title": "...", "location": "...", "all_day": <bool>}]
  },
  "mail": [
    {"label": "<label>", "summary": "<one sentence summary if multiple, "
       "or null if there's only one item>", "items": [
       {"subject": "<subject>", "from": "<sender display>", "snippet": "<8-15 word "
        "single-sentence digest of the email>"}, ...
    ]}
  ],
  "news": [
    {"category": "Minnesota & Local|National & World|Tech|Crypto",
     "items": [{"headline": "<headline>", "link": "<url>",
                "summary": "<one short sentence>",
                "source": "<verbatim source name from input>"}]
    }
  ],
  "recipe": {"name": "<name>", "blurb": "<one short sentence>", "url": "<url or null>"}
}

Rules:
- Calendar: pass events through verbatim — do not invent, drop, or
  reword. Preserve the `time`, `title`, `location`, and `all_day` fields
  exactly as input. Keep input order. If an event list is empty, output
  an empty array for that day. The output `calendar.today` and
  `calendar.tomorrow` arrays must mirror the input one-to-one.
- Group news items by category. Lead with Minnesota & Local. Skip
  duplicate stories across feeds. 3-6 items per category. Skip a
  category entirely if its feeds returned nothing.
- The input may include a `recently_shown` array — items the user
  already saw in the last few days' briefings, each with `headline`,
  `link`, and `date_shown`. DO NOT include any item whose link matches
  one in `recently_shown`. Also drop items that are clearly the same
  underlying news event as a recently-shown headline (different outlet,
  reworded headline, or same story with no substantive new
  development) — when in doubt, prefer a fresher unrelated story over
  a stale rerun. An item with genuinely new developments on a prior
  story is fine to include; lead the summary with what's new.
- For each mail label, keep all messages; just write one-sentence
  digests for context. If a label has no items, omit it from the array.
- Write naturally. Don't editorialize beyond what the data says.
- Don't include emoji.
- Don't include URLs in `summary`/`snippet` fields — only in `link`.
- Output strict JSON. Quotes escaped properly. No trailing commas.
"""


async def _llm_summarize(ctx, data: dict[str, Any]) -> dict[str, Any] | None:
    # Use claude_cli (subscription, sonnet) for the editorial pass — better
    # grouping/dedup than haiku, and metered-API cost is zero on the user's
    # Max plan. Falls back to cheap-tier if claude_cli isn't registered.
    try:
        resolved = ctx.router.resolve("claude_cli")
    except Exception:
        try:
            resolved = ctx.router.resolve_cheap_tier()
        except Exception as e:
            _log.warning("daily_digest: no LLM route available: %s", e)
            return None
    user_msg = "## Data\n\n```json\n" + json.dumps(data, indent=2, default=str) + "\n```"
    try:
        result = await resolved.provider.complete(
            system=_LLM_SYSTEM,
            messages=[Message(role="user", content=user_msg)],
            tools=None,
            model=resolved.model,
            session_id=None,
            stream_handler=None,
        )
    except Exception as e:
        _log.warning("daily_digest: LLM call failed: %s", e)
        return None
    text = (result.text or "").strip()
    # Defensive: strip code fences if the model added them despite instructions.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        _log.warning("daily_digest: LLM did not return valid JSON: %s; head=%r", e, text[:200])
        return None


# --- HTML rendering -------------------------------------------------------


_BG = "#f3ece0"
_CARD = "#ffffff"
_TEXT = "#2a2622"
_MUTED = "#7a6f66"
_RULE = "#ece2d2"
_ACCENT = "#b9531a"
_ACCENT_SOFT = "#e9d3b8"
_FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"


def _h_section_title(title: str) -> str:
    return (
        f'<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;'
        f'font-weight:600;color:{_ACCENT};margin:0 0 12px 0;">{escape(title)}</div>'
    )


def _h_rule() -> str:
    return (
        f'<div style="height:1px;background:{_RULE};margin:32px 0 32px 0;'
        'line-height:1px;font-size:0;">&nbsp;</div>'
    )


def _h_greeting(greeting: str, today_str: str) -> str:
    return (
        f'<div style="margin:0 0 24px 0;">'
        f'<div style="font-size:12px;letter-spacing:.16em;text-transform:uppercase;'
        f'color:{_MUTED};font-weight:600;">{escape(today_str)}</div>'
        f'<div style="font-size:24px;font-weight:600;color:{_TEXT};margin-top:8px;'
        f'line-height:1.3;">{escape(greeting)}</div>'
        f"</div>"
    )


def _h_weather(text: str) -> str:
    return (
        _h_section_title("Weather") + f'<div style="font-size:16px;line-height:1.6;color:{_TEXT};">'
        f"{escape(text)}</div>"
    )


def _h_calendar(cal: dict[str, Any] | None) -> str:
    cal = cal or {}
    today = cal.get("today") or []
    tomorrow = cal.get("tomorrow") or []
    if not today and not tomorrow:
        return (
            _h_section_title("Schedule")
            + f'<div style="font-size:15px;color:{_MUTED};">Nothing scheduled today or tomorrow.</div>'
        )

    def _one_event(ev: dict[str, Any]) -> str:
        time = ev.get("time", "") or ""
        title = ev.get("title", "") or ""
        loc = ev.get("location", "") or ""
        return (
            f'<div style="font-size:14px;line-height:1.55;color:{_TEXT};'
            f'padding:8px 0;border-top:1px solid {_RULE};">'
            f'<div style="color:{_ACCENT};font-size:12px;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:.08em;">{escape(time)}</div>'
            f'<div style="font-weight:600;margin-top:2px;">{escape(title)}</div>'
            + (
                f'<div style="color:{_MUTED};font-size:13px;margin-top:2px;">{escape(loc)}</div>'
                if loc
                else ""
            )
            + "</div>"
        )

    def _day_block(label: str, evs: list[dict[str, Any]]) -> str:
        if not evs:
            return (
                f'<div style="margin:0 0 16px 0;">'
                f'<div style="font-size:14px;font-weight:600;color:{_TEXT};margin-bottom:6px;">'
                f'{escape(label)} <span style="color:{_MUTED};font-weight:400;">· nothing</span>'
                f"</div></div>"
            )
        return (
            f'<div style="margin:0 0 20px 0;">'
            f'<div style="font-size:14px;font-weight:600;color:{_TEXT};margin-bottom:6px;">'
            f'{escape(label)} <span style="color:{_MUTED};font-weight:400;">'
            f"· {len(evs)}</span></div>" + "".join(_one_event(e) for e in evs) + "</div>"
        )

    return (
        _h_section_title("Schedule") + _day_block("Today", today) + _day_block("Tomorrow", tomorrow)
    )


def _h_mail(sections: list[dict[str, Any]]) -> str:
    if not sections:
        return (
            _h_section_title("Inbox")
            + f'<div style="font-size:15px;color:{_MUTED};">Nothing waiting. Inbox zero.</div>'
        )
    out = [_h_section_title("Inbox")]
    for s in sections:
        label = s.get("label", "")
        items = s.get("items", []) or []
        summary = s.get("summary")
        out.append(
            f'<div style="margin:0 0 20px 0;">'
            f'<div style="font-size:14px;font-weight:600;color:{_TEXT};margin-bottom:6px;">'
            f'{escape(label)} <span style="color:{_MUTED};font-weight:400;">'
            f"· {len(items)}</span></div>"
        )
        if summary:
            out.append(
                f'<div style="font-size:14px;color:{_MUTED};margin-bottom:10px;'
                f'line-height:1.55;">{escape(summary)}</div>'
            )
        for it in items:
            subj = it.get("subject", "")
            sender = it.get("from", "")
            snip = it.get("snippet", "")
            out.append(
                f'<div style="font-size:14px;line-height:1.55;color:{_TEXT};'
                f'padding:8px 0;border-top:1px solid {_RULE};">'
                f'<div style="font-weight:600;">{escape(subj)}</div>'
                f'<div style="color:{_MUTED};font-size:13px;margin-top:2px;">'
                f"{escape(sender)}</div>"
                + (f'<div style="margin-top:4px;">{escape(snip)}</div>' if snip else "")
                + "</div>"
            )
        out.append("</div>")
    return "\n".join(out)


def _h_news(sections: list[dict[str, Any]]) -> str:
    if not sections:
        return ""
    out = [_h_section_title("News")]
    for s in sections:
        cat = s.get("category", "")
        items = s.get("items", []) or []
        if not items:
            continue
        out.append(
            f'<div style="font-size:14px;font-weight:600;color:{_TEXT};'
            f'margin:0 0 10px 0;">{escape(cat)}</div>'
        )
        for it in items:
            link = it.get("link", "") or ""
            head = it.get("headline", "")
            summ = it.get("summary", "")
            src = it.get("source", "") or ""
            head_html = (
                f'<a href="{escape(link, quote=True)}" '
                f'style="color:{_ACCENT};text-decoration:none;font-weight:600;">'
                f"{escape(head)}</a>"
                if link
                else f'<span style="font-weight:600;color:{_TEXT};">{escape(head)}</span>'
            )
            out.append(
                f'<div style="font-size:14px;line-height:1.55;color:{_TEXT};'
                f'padding:8px 0;border-top:1px solid {_RULE};">'
                f"{head_html}"
                + (
                    f'<div style="color:{_MUTED};margin-top:4px;">{escape(summ)}</div>'
                    if summ
                    else ""
                )
                + (
                    f'<div style="color:{_MUTED};font-size:12px;margin-top:4px;'
                    f'text-transform:uppercase;letter-spacing:.08em;">{escape(src)}</div>'
                    if src
                    else ""
                )
                + "</div>"
            )
        out.append('<div style="height:14px;line-height:14px;font-size:0;">&nbsp;</div>')
    return "\n".join(out)


def _h_recipe(rec: dict[str, Any] | None) -> str:
    if not rec:
        return ""
    name_ = rec.get("name", "")
    blurb = rec.get("blurb", "") or ""
    url = rec.get("url") or ""
    title_html = (
        f'<a href="{escape(url, quote=True)}" '
        f'style="color:{_ACCENT};text-decoration:none;font-weight:600;">'
        f"{escape(name_)}</a>"
        if url
        else f'<span style="font-weight:600;color:{_TEXT};">{escape(name_)}</span>'
    )
    return (
        _h_section_title("Recipe")
        + f'<div style="font-size:16px;line-height:1.6;color:{_TEXT};">{title_html}</div>'
        + (
            f'<div style="font-size:14px;line-height:1.6;color:{_MUTED};margin-top:6px;">'
            f"{escape(blurb)}</div>"
            if blurb
            else ""
        )
    )


def _render_html(summary: dict[str, Any], today_str: str) -> str:
    body_parts = [
        _h_greeting(summary.get("greeting", "Good morning."), today_str),
        _h_weather(summary.get("weather", "")),
        _h_rule(),
        _h_calendar(summary.get("calendar")),
        _h_rule(),
        _h_mail(summary.get("mail", []) or []),
        _h_rule(),
        _h_news(summary.get("news", []) or []),
    ]
    recipe_html = _h_recipe(summary.get("recipe"))
    if recipe_html:
        body_parts.append(_h_rule())
        body_parts.append(recipe_html)
    inner = "\n".join(p for p in body_parts if p)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Morning briefing</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:{_FONT_STACK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{_BG};">
  <tr>
    <td align="center" style="padding:32px 16px 48px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="max-width:600px;background:{_CARD};border-radius:14px;
                    box-shadow:0 1px 2px rgba(60,40,20,.04),0 4px 16px rgba(60,40,20,.05);">
        <tr>
          <td style="padding:36px 36px 40px 36px;">
            <div style="height:4px;width:42px;background:{_ACCENT_SOFT};
                        border-radius:2px;margin-bottom:28px;"></div>
            {inner}
          </td>
        </tr>
      </table>
      <div style="max-width:600px;color:{_MUTED};font-size:12px;line-height:1.5;
                  margin:20px auto 0 auto;text-align:center;">
        ember · daily digest
      </div>
    </td>
  </tr>
</table>
</body>
</html>"""


# --- Entry point ----------------------------------------------------------


def _format_today() -> str:
    now = datetime.datetime.now()
    return now.strftime("%A · %B %-d, %Y")


def _summarize_weather(raw: dict[str, Any] | None) -> str:
    if not raw:
        return "Weather unavailable."
    periods = (raw.get("properties") or {}).get("periods") or []
    if not periods:
        return "Weather unavailable."
    # At 5am NWS may lead with an "Overnight" period; search the first four.
    today = next((p for p in periods[:4] if p.get("isDaytime")), None)
    tonight = next((p for p in periods[:4] if not p.get("isDaytime")), None)
    parts: list[str] = []
    if today:
        high = today.get("temperature")
        cond = today.get("shortForecast", "")
        pop = (today.get("probabilityOfPrecipitation") or {}).get("value") or 0
        s = (
            f"Today: {cond}, high {high}°F in {_WEATHER_PLACE}"
            if cond and high
            else (f"High {high}°F in {_WEATHER_PLACE}" if high else "")
        )
        if s:
            if pop >= 30:
                s += f", {int(pop)}% chance of precipitation"
            parts.append(s)
    if tonight:
        low = tonight.get("temperature")
        cond = tonight.get("shortForecast", "")
        pop = (tonight.get("probabilityOfPrecipitation") or {}).get("value") or 0
        s = (
            f"Tonight: {cond}, low {low}°F"
            if cond and low
            else (f"Low tonight {low}°F" if low else "")
        )
        if s:
            if pop >= 30:
                s += f", {int(pop)}% chance of precipitation"
            parts.append(s)
    return ". ".join(parts) + "." if parts else "Weather unavailable."


def _fallback_summary(
    weather: dict[str, Any] | None,
    mail: list[tuple[str, list[dict[str, Any]]]],
    feeds: list[dict[str, Any]],
    recipe: dict[str, Any] | None,
    calendar: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Used when LLM JSON parse fails. Plain data, no editorial."""
    mail_sections = []
    for label, msgs in mail:
        if not msgs:
            continue
        mail_sections.append(
            {
                "label": label,
                "summary": None,
                "items": [
                    {
                        "subject": m.get("subject", ""),
                        "from": m.get("from", ""),
                        "snippet": (m.get("snippet", "") or "")[:160],
                    }
                    for m in msgs
                ],
            }
        )
    news_items = []
    for f in feeds:
        source = f.get("source") or ""
        for it in (f.get("items") or [])[:3]:
            news_items.append(
                {
                    "headline": it.get("title", ""),
                    "link": it.get("link", ""),
                    "summary": (it.get("description", "") or "")[:140],
                    "source": source,
                }
            )
    news_sections = [{"category": "Top stories", "items": news_items[:10]}] if news_items else []
    recipe_obj = None
    if recipe:
        slug = recipe.get("slug") or ""
        recipe_obj = {
            "name": recipe.get("name") or "(untitled)",
            "blurb": (recipe.get("description") or "").strip()[:200],
            "url": f"{_MEALIE_PUBLIC_URL}/g/home/r/{slug}" if slug else None,
        }
    return {
        "greeting": "Good morning.",
        "weather": _summarize_weather(weather),
        "calendar": calendar or {"today": [], "tomorrow": []},
        "mail": mail_sections,
        "news": news_sections,
        "recipe": recipe_obj,
    }


def _normalize_link(link: str) -> str:
    """Strip query strings and fragments so trackers don't defeat exact-match
    dedup. e.g. `?utm_source=...` and `#hashlink` shouldn't make the same
    article look novel day-over-day."""
    if not link:
        return ""
    s = link.strip()
    for sep in ("#", "?"):
        i = s.find(sep)
        if i != -1:
            s = s[:i]
    return s.rstrip("/")


def _load_recently_shown(today: datetime.date) -> list[dict[str, str]]:
    """Read the last `_DEDUP_LOOKBACK_DAYS` of digest manifests and return a
    flat list of `{headline, link, date_shown}`. Missing files are fine —
    first run after deploy just produces an empty list."""
    out: list[dict[str, str]] = []
    if not _BRIEFINGS_DIR.exists():
        return out
    for back in range(1, _DEDUP_LOOKBACK_DAYS + 1):
        d = today - datetime.timedelta(days=back)
        path = _BRIEFINGS_DIR / f"{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            _log.warning("daily_digest: could not read %s: %s", path, e)
            continue
        for it in payload.get("items", []):
            out.append(
                {
                    "headline": it.get("headline", ""),
                    "link": it.get("link", ""),
                    "date_shown": d.isoformat(),
                }
            )
    return out


def _save_manifest(today: datetime.date, summary: dict[str, Any]) -> None:
    """Write today's news selections so future runs can dedupe against them.
    Best-effort — IO failure here must not break the morning email."""
    items: list[dict[str, str]] = []
    for section in summary.get("news", []) or []:
        category = section.get("category", "")
        for it in section.get("items", []) or []:
            items.append(
                {
                    "headline": it.get("headline", ""),
                    "link": it.get("link", ""),
                    "source": it.get("source", ""),
                    "category": category,
                }
            )
    try:
        _BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = _BRIEFINGS_DIR / f"{today.isoformat()}.json"
        path.write_text(json.dumps({"date": today.isoformat(), "items": items}, indent=2))
    except OSError as e:
        _log.warning("daily_digest: could not save manifest: %s", e)


def _filter_recently_shown(
    summary: dict[str, Any], recently_shown: list[dict[str, str]]
) -> dict[str, Any]:
    """Belt-and-suspenders: drop any news item whose normalized link matches
    something the user already saw, in case the LLM ignored the rule."""
    if not recently_shown:
        return summary
    seen_links = {_normalize_link(r["link"]) for r in recently_shown if r.get("link")}
    if not seen_links:
        return summary
    new_news = []
    for section in summary.get("news", []) or []:
        kept = [
            it
            for it in section.get("items", []) or []
            if _normalize_link(it.get("link", "")) not in seen_links
        ]
        if kept:
            new_news.append({**section, "items": kept})
    summary["news"] = new_news
    return summary


async def _run_digest_once(ctx) -> dict[str, Any]:
    """One full digest attempt: fetch → compose → send. Returns a result
    dict with `sent` (bool), `weather_ok`, and either send metadata or an
    `error`. Emits no user-facing notifications — `send_digest` owns those
    so retries don't spam the channel."""
    profile = ctx.profile or ""
    feed_urls = _extract_news_sources(profile)
    today_local = datetime.datetime.now().date()
    recently_shown = _load_recently_shown(today_local)

    weather_task = asyncio.create_task(_fetch_weather())
    mail_tasks = [asyncio.create_task(_fetch_label(ctx, label)) for label in _LABELS]
    rss_task = asyncio.create_task(_fetch_rss(ctx, feed_urls))
    recipe_task = asyncio.create_task(_fetch_recipe(ctx))
    calendar_task = asyncio.create_task(_fetch_calendar(ctx, today_local))

    weather_result, mail_results, feeds, recipe, raw_events = await asyncio.gather(
        weather_task,
        asyncio.gather(*mail_tasks),
        rss_task,
        recipe_task,
        calendar_task,
    )
    weather, weather_err = weather_result
    events_binned = _bin_events_by_day(raw_events, today_local)
    weather_ok = weather is not None
    if not weather_ok:
        _log.warning(
            "daily_digest: weather fetch failed: %s",
            f"{type(weather_err).__name__}: {weather_err}" if weather_err else "unknown error",
        )

    # Build raw data block for the LLM.
    mail_data = []
    for label, msgs in mail_results:
        if not msgs:
            continue
        mail_data.append(
            {
                "label": label,
                "messages": [
                    {
                        "subject": m.get("subject", ""),
                        "from": m.get("from", ""),
                        "date": m.get("date_iso", ""),
                        "snippet": m.get("snippet", ""),
                    }
                    for m in msgs
                ],
            }
        )
    news_data: list[dict[str, Any]] = []
    for f in feeds:
        if not f.get("ok", True):
            continue
        source = f.get("source") or ""
        for it in (f.get("items") or [])[:_RSS_PER_FEED]:
            news_data.append(
                {
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "description": (it.get("description", "") or "")[:_RSS_DESC_MAX],
                    "source": source,
                }
            )

    recipe_data = None
    if recipe:
        slug = recipe.get("slug") or ""
        recipe_data = {
            "name": recipe.get("name", ""),
            "description": (recipe.get("description") or "").strip(),
            "url": f"{_MEALIE_PUBLIC_URL}/g/home/r/{slug}" if slug else None,
        }

    raw_data = {
        "weather_summary": _summarize_weather(weather),
        "calendar": events_binned,
        "mail_by_label": mail_data,
        "news_by_feed": news_data,
        "recently_shown": recently_shown,
        "recipe": recipe_data,
    }

    summary = await _llm_summarize(ctx, raw_data)
    if summary is None:
        summary = _fallback_summary(weather, mail_results, feeds, recipe, events_binned)
    summary = _filter_recently_shown(summary, recently_shown)

    today_str = _format_today()
    html = _render_html(summary, today_str)
    subject = f"Morning brief · {today_str.split(' · ')[1]}"

    res = await ctx.tools.call_tool(
        "gmail_send",
        {
            "to": _RECIPIENT,
            "subject": subject,
            "body": html,
            "html": True,
        },
    )
    if res.is_error:
        _log.warning("daily_digest: gmail_send failed: %s", res.output)
        return {"sent": False, "error": str(res.output), "weather_ok": weather_ok}
    _save_manifest(today_local, summary)
    return {"sent": True, "subject": subject, "bytes": len(html), "weather_ok": weather_ok}


# Retry schedule for send_digest. A 5am fire can land inside a brief
# WAN/DNS outage (e.g. an ISP maintenance window). Each value is the
# seconds to wait before the next full-pipeline retry, so a short outage
# tail still gets the brief delivered once the network returns. Cumulative
# reach ≈ 16 min past the scheduled fire.
_RETRY_BACKOFFS_S = (180, 300, 480)


async def _safe_notify(ctx, message: str) -> None:
    """Best-effort channel notify. notify itself needs the network, so a
    failure here (e.g. mid-outage) is logged, not raised."""
    try:
        await ctx.notify(message)
    except Exception:
        _log.exception("daily_digest: ctx.notify() failed")


async def _notify_delivered(ctx, result: dict[str, Any], *, retry: int) -> None:
    parts = [f"✉️ digest sent ({result.get('bytes', 0)} bytes)"]
    if retry:
        parts.append(f"on retry {retry} after an earlier failure")
    if not result.get("weather_ok", True):
        parts.append("— weather unavailable, sent with placeholder")
    await _safe_notify(ctx, " ".join(parts))


async def send_digest(ctx) -> dict[str, Any]:
    """Scheduled entry point. Runs the digest pipeline, retrying the whole
    thing on a delivery failure per `_RETRY_BACKOFFS_S` — so a fire that
    lands in a brief outage still delivers once the network is back. All
    user-facing notifications are emitted here (not per-attempt) so retries
    don't spam the channel."""
    if not _RECIPIENT:
        raise RuntimeError("daily_digest: DIGEST_RECIPIENT is not set")
    attempts = 1 + len(_RETRY_BACKOFFS_S)
    last: dict[str, Any] = {"sent": False, "error": "no attempt", "weather_ok": False}
    for i in range(attempts):
        last = await _run_digest_once(ctx)
        if last.get("sent"):
            await _notify_delivered(ctx, last, retry=i)
            return last
        if i < len(_RETRY_BACKOFFS_S):
            wait = _RETRY_BACKOFFS_S[i]
            _log.warning(
                "daily_digest: attempt %d/%d failed (%s); retrying in %ds",
                i + 1,
                attempts,
                last.get("error"),
                wait,
            )
            await asyncio.sleep(wait)
    await _safe_notify(
        ctx,
        f"⚠️ daily_digest: no email sent — gave up after {attempts} attempts. "
        f"Last error: {last.get('error')}",
    )
    return last
