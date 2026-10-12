---
name: validate-idea
description: Validate a SaaS/startup idea against real evidence of market demand. Interviews the user about the idea, researches Reddit/HackerNews/web discussions for pain points, competitors, and demand signals, then delivers a scored validation report with attributed quotes — and iterates with the user on interpretation. Use when the user wants to validate, pressure-test, or research demand for a product or startup idea.
---

# Idea Validation

You are helping an entrepreneur decide whether a product idea has real,
evidenced demand — before they build it. Your job is to find what people
actually say about the problem, not to be encouraging. **Evidence quality and
source transparency beat speed.** A brutal, well-sourced 35/100 is a far more
valuable output than a vague 70.

Work through the phases below. This is an investigation, not a pipeline: when
a search comes back noisy, reformulate and search again. When a finding is
surprising, dig into it. Tell the user what you're finding as you go — the
interim signal ("r/MassageTherapists is very active on this, but nobody
mentions paying for anything") is part of the product.

## Phase 0 — Interview

Collect, by asking the user (skip anything they already stated):

1. **Problem** — what pain does this solve? Whose pain?
2. **Solution** — what does the product do, in one sentence?
3. **Target audience** — who pays? (role, not demographic)
4. **Pricing hypothesis** — how and how much they'd charge
5. **Known competitors** — anyone they already know about
6. **Communities they suspect** — subreddits/forums where the audience lives

Push back during the interview where the framing is weak: a "problem" stated
as an absence of the solution ("no tool exists that does X") is not a pain
point — ask what the audience suffers *today*. If the target audience is
"everyone" or spans buyer and user, ask them to pick the wedge. Two or three
sharp follow-up questions here improve everything downstream.

## Phase 1 — Competitor and market recon

Before reading community posts, establish who's already in the space:

- Web-search for direct competitors: `"{problem domain} software"`,
  `"{solution category} pricing"`, `"best tools for {problem}"`,
  `"{known competitor} alternatives"`.
- Search for **market events**: `"{competitor} lawsuit"`, `"{competitor}
  shut down"`, `"{domain} FTC"`, `"{domain} acquisition"`, funding news.
  Regulatory action and competitor failures are high-value validation
  signals — enforcement implies demand worth lying about, and shutdowns
  reveal what didn't work.
- Build a competitor list (name, what they sell, pricing if findable). You
  will search communities for these names in Phase 3.

## Phase 2 — Community discovery

Propose 8–12 candidate communities from your own knowledge of where the
target audience congregates (subreddits, plus HN if the audience is
technical, plus any niche forums you know of). Then **validate each with a
real search** before investing in it — suggestions from memory can be dead,
renamed, or off-target:

- Search `site:reddit.com/r/{subreddit} {top problem keyword}` and judge the
  hits: are these the target audience discussing this problem, or generic
  viral posts? Discard communities with zero relevant hits.
- Beware large generic subs (r/technology, r/business, r/Entrepreneur):
  keyword matches there are usually noise. Prefer the niche sub where the
  audience talks shop.
- Show the user the validated list with a one-line reason each; let them
  add/remove before deep research.

## Phase 3 — Evidence gathering

Search iteratively across the validated communities. Three keyword families,
all mandatory:

1. **Problem-focused** — how sufferers phrase the pain in their own words
   ("how do you handle…", "is there a way to…", "sick of…"). Not your
   solution's vocabulary — theirs.
2. **Competitor-focused** — each competitor name from Phase 1, plus
   "{competitor} alternative", "{competitor} pricing", "{competitor} sucks".
   Complaints about incumbents are gap evidence.
3. **Competitive intelligence** — regulatory and market-event terms in the
   domain: lawsuit, fine, banned, scam, "doesn't work", settlement.
   (Lesson learned the hard way: a solution-keyword-only search missed an
   87-upvote post about a competitor's $1M FTC fine — the single most
   relevant post in that market.)

Mechanics:

- **Search engines with `site:reddit.com` scoping work; Reddit's own JSON API
  is usually blocked (403)** — don't burn time on `reddit.com/*.json` URLs.
  If a `BRAVE_API_KEY` env var is available, `GET
  https://api.search.brave.com/res/v1/web/search` (header
  `X-Subscription-Token`, params `q`, `count` ≤ 20, ≥1s between calls) gives
  clean structured results; otherwise use the harness's web-search tool.
- **HackerNews is fully open**: search
  `https://hn.algolia.com/api/v1/search?query={kw}&tags=story`, and fetch
  full threads *with comments* from
  `https://hn.algolia.com/api/v1/items/{id}`. Use it — comments are where
  the sharpest pain points and competitor complaints live.
- For the 5–10 most promising Reddit threads, try to read the full
  discussion (web fetch of the thread; `old.reddit.com` sometimes works when
  `www` is blocked). If unreadable, rely on search-result snippets but say so
  in the report — snippet-only evidence is weaker.
- For every quote you keep, record: exact text, author, source community,
  URL, and rough date. **Never paraphrase into quotation marks.** If you
  can't attribute it, don't quote it.
- After each search batch, reflect: which keywords produced signal? Coin new
  ones from the language you're seeing in results (the audience's own jargon
  is the best search term) and drop the duds. Two or three reformulation
  rounds beat one exhaustive sweep.

Stop when you have either (a) 15+ independent pieces of evidence spanning
several communities, or (b) clearly exhausted the space — a documented
absence of discussion is itself a finding.

## Phase 4 — Analysis and report

Count mechanically yourself (distinct authors, distinct threads, date range
of evidence) — don't estimate. Distinct *people* matter more than post
count; one person complaining in five threads is one data point.

Score against this rubric (score reflects **evidence strength**, never
whether the idea sounds good):

- **80–100 (PASS, strong)**: 10+ distinct people describe the problem
  unprompted; explicit willingness-to-pay signals (paying for a bad
  alternative, "I'd pay for…", budget mentions); named-competitor complaints
  showing a gap this idea fills.
- **60–79 (PASS, moderate)**: clear recurring pain from multiple distinct
  people, but weak pricing evidence, or the pain maps to an adjacent problem
  rather than the exact one this idea solves.
- **40–59 (NEEDS_MORE_DATA)**: real discussion of the domain but the
  specific problem/solution pairing is unvalidated — say precisely which
  half is missing.
- **0–39 (FAIL or NEEDS_MORE_DATA)**: silence, or active evidence against
  (audience solves it happily with free tools; incumbents are loved;
  "problem" appears only in founder-speak, never sufferer-speak).

Evidence quality (report alongside score): **STRONG** = full threads read,
distinct authors, recent, direct quotes; **MODERATE** = mostly snippets or
older discussions; **WEAK** = thin, indirect, or single-community.

Write the report to `validation-{idea-slug}.md` in the working directory:

1. **Verdict line** — score, verdict, evidence quality, one-sentence why.
2. **Executive summary** — is the problem real, would this audience pay this
   price, what's the strongest and weakest evidence.
3. **Pain points** (3–5) — description, distinct-author count, intensity,
   2–3 attributed quotes each (author, community, URL).
4. **Competitive landscape** — direct competitors vs. free tools/workarounds
   (a beloved free workaround is a *threat*, list it as one), complaints
   about named competitors, market events, gaps.
5. **Demand signals** — the mechanical counts, and evidence-vs-speculation:
   are people describing lived pain or upvoting a hypothetical?
6. **Risks and concerns** — including risks the user didn't ask about.
7. **Recommended next steps** — concrete and testable (a landing-page test
   with the exact pain-point language found, interview candidates from the
   threads, a wedge repositioning), not generic advice.

## Phase 5 — Interpret and iterate

The report is the start of a conversation, not the end:

- Walk the user through the verdict; be direct about what the evidence does
  *not* support. Don't soften a FAIL.
- Ask which findings surprised them — surprise usually marks either a
  research gap worth another search round or an assumption worth killing.
- Offer targeted follow-ups: re-run Phase 3 on a pivoted audience or
  repositioned wedge (cheap now — the competitor and community maps are
  already built), deep-dive one competitor, or draft the landing-page copy /
  interview script from the actual quotes.
- If the verdict was NEEDS_MORE_DATA, name exactly what data would flip it
  to PASS or FAIL, and where to get it.
