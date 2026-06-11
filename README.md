# NYC Report Heat — "The Hot 100"

Pipeline + dashboard for finding NYC government reports/rule changes and ranking objective public heat for the exact report/link.

The frontend is **[The Hot 100](site/index.html)** — a kitschy radio-countdown dashboard that ranks the tracked inventory by public attention. It is a static site (no build step) that reads a single JSON feed produced by the pipeline ([site/data/dashboard.json](site/data/dashboard.json)) and is deployed to GitHub Pages.

```bash
# regenerate data, then preview the dashboard locally
uv run nyc-report-heat rank --config config/default.yml
python -m http.server -d site 8000   # open http://localhost:8000
```

## What it does

The project intentionally separates:

- **Inventory**: discovered report/rule/publication URLs from known public sources.
- **Harvest**: domain-first collection of every public pickup of a tracked gov domain — providers are queried per domain (~10 queries each), not per candidate, and results accumulate in a persistent mention store ([data/mentions.jsonl](data/mentions.jsonl)).
- **Matching**: harvested links are joined against the inventory locally (normalized exact URL, canonical variants, distinctive PDF filename). Gov links that don't match anything become the "shared but untracked" list — a blind-spot alert and discovery hint.
- **Daily delta**: newly discovered candidates compared with the previous inventory.

## Sources

Configured in [config/default.yml](config/default.yml): 30+ NYC & NYS agencies and bodies — city & state watchdogs (DOI, both comptrollers, IBO), agency report libraries, NYC Rules (proposed & adopted), and the Government Publications Portal. The config file is the canonical list.

The inventory model supports PDF and non-PDF report pages. `document_url` is populated when the actual artifact is a PDF/DOCX/XLSX; otherwise the HTML page URL is the canonical heat URL.

## Commands

```bash
uv run nyc-report-heat discover
uv run nyc-report-heat harvest                 # pull gov-domain mentions into the store
uv run nyc-report-heat rank --windows 1,7,30 --rank-window 30d
uv run nyc-report-heat daily --config config/default.yml
uv run nyc-report-heat show --limit 25
```

## Outputs

- [data/candidates.jsonl](data/candidates.jsonl): current canonical inventory.
- [data/mentions.jsonl](data/mentions.jsonl): persistent harvested-mention store (one gov-link pickup per line, deduped).
- `data/articles_seen.txt`: ledger of news articles already scanned for gov links.
- [outputs/ranked.jsonl](outputs/ranked.jsonl): ranked records with component scores and evidence.
- [outputs/ranked.csv](outputs/ranked.csv): frontend/spreadsheet-friendly ranking.
- [outputs/new_candidates.jsonl](outputs/new_candidates.jsonl): daily discovery delta.
- [outputs/new_candidates.csv](outputs/new_candidates.csv): spreadsheet-friendly daily delta.
- [outputs/daily_summary.md](outputs/daily_summary.md): human-readable daily summary.
- `outputs/top_reports.csv`, `outputs/top_rules.csv`, and `outputs/link_heat.csv`: pre-filtered cuts for a frontend or weekly briefing.
- [site/data/dashboard.json](site/data/dashboard.json): denormalized JSON feed for the dashboard (stats, per-window heat, evidence, and the daily delta in one file).

## Frontend & Deployment

The dashboard lives in [site/](site) as plain HTML/CSS/JS (`index.html`, `styles.css`, `app.js`) and fetches `data/dashboard.json` at runtime — no bundler, no framework, no API. This keeps deployment trivial: GitHub Pages serves the `site/` directory as-is.

- [.github/workflows/pages.yml](.github/workflows/pages.yml) publishes `site/` to GitHub Pages on every push to `main`, after each daily data refresh (`workflow_run`), or on manual dispatch.
- The daily refresh ([daily-report-heat.yml](.github/workflows/daily-report-heat.yml)) regenerates `site/data/dashboard.json` and commits it, which in turn triggers a fresh Pages deploy.

Because the feed is fetched over HTTP, preview it with a static server (`python -m http.server -d site`) rather than opening `index.html` from the filesystem.

## Daily GitHub Action

[.github/workflows/daily-report-heat.yml](.github/workflows/daily-report-heat.yml) runs daily at 11:30 UTC and can also be triggered manually. It:

1. Installs `uv`.
2. Runs `uv run nyc-report-heat daily --config config/default.yml`.
3. Uploads outputs as an artifact.
4. Commits changed data/output files back to the repo.

Every provider is free and unauthenticated. Heat collection is **domain-first**: instead of searching the world once per candidate (which is exactly the request shape that gets rate-limited and returns zeros), each provider is queried once per tracked gov domain and the results are matched against the inventory locally.

- **Bluesky** (`api.bsky.app` search, no auth): posts sharing a tracked gov link, with likes/reposts/quotes/replies as engagement. This is the core "people sending it around" signal.
- **Reddit** (OAuth script app, skips cleanly without credentials): posts linking a tracked gov domain, with score + comments as engagement.
- **Hacker News** (Algolia, no auth): stories whose URL is on a tracked gov domain, with points + comments as engagement.
- **NYC press RSS** (`newsrss`): polls Gothamist, THE CITY, City & State, NY Post Metro, NYT NYRegion, amNY, and City Limits feeds, fetches each new article once (ledger: `data/articles_seen.txt`), and extracts outbound links to tracked gov domains — direct observation of journalists citing the document.

Title-based news matching (GDELT) was tried and removed: matching distinctive title
terms against a global news firehose produces confident-looking but coincidental hits
("Waste Management stocks" for a commercial-waste rule). The board counts only
verifiable evidence — a real post or article carrying the exact link or filename.

All harvested mentions accumulate in `data/mentions.jsonl` (deduped on a stable uid, committed by the daily workflow), so rolling windows are computed from history rather than re-queried live, and a missed CI day loses nothing from backfill-capable providers. The HTTP client uses a browser User-Agent, retry/backoff on 429/5xx, and a per-request cache.

## Heat Windows

Discovery and heat windows are separate. The manual `discover` command is the backfill path and defaults to `discovery_lookback_days: 90` so the first inventory does not go back forever. The `daily` command uses `daily_discovery_lookback_days: 14` to look only for recent additions, then merges those into the existing tracked inventory. A report is not dropped just because it fell off a source's recent/reports page. Every tracked candidate is scored in every configured heat window. The default config computes:

- `today` (`1` day)
- `7d`
- `30d`

The default ranking window is `30d`, set by `rank_window: 30d`. CSV outputs include per-window columns such as `heat_score_today`, `heat_score_7d`, `heat_score_30d`, `exact_url_mentions_7d`, and `social_engagement_7d`.

Windows are computed from the mention store using each mention's published timestamp (harvest time as fallback), so heat is recalculated for every tracked candidate on each daily run as the `today`, `7d`, and `30d` windows move forward. Bluesky and Hacker News can backfill ~30 days on a fresh store; press-RSS citations accrue from the day harvesting starts (feeds only expose recent articles).

## Scoring Notes

Heat is an objective measure of public attention to the exact report. The signals are
**exact-link pickups only**: news articles whose body links the document, and social
posts sharing the link (weighted by engagement).

Current weights:

- `6.0 * exact_url_mentions` — news articles linking the exact URL
- `2.0 * social_exact_mentions` — Bluesky/HN posts sharing the exact link
- `1.0 * log10(1 + social_engagement)` — amplification of those posts (likes/reposts/points/comments); log-scaled so it discriminates across the range reports actually see (single digits to dozens) without a cap, while a single news citation still outweighs any realistic engagement bonus
- `2.0 * filename_mentions`, capped at 5 — the distinctive PDF filename seen without the full URL

The metric deliberately excludes source priority, OID relevance, topic keywords, and
human-interest heuristics. It is title/URL-anchored to the specific document, not the
broader topic. Mentions are windowed by post/article date, so the chart surfaces reports
drawing attention *now* — older reports with no recent pickups correctly read as cold. The
daily command writes grouped CSVs so the frontend can expose “all links,” “reports,”
“rules,” and “nonzero heat” without changing the metric.
