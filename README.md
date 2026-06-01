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
- **Link heat**: high-confidence exact URL or filename pickup when available.
- **Report heat**: distinctive PDF/report filenames when the file itself is mentioned without the full URL.
- **Daily delta**: newly discovered candidates compared with the previous inventory.

## Sources

Configured in [config/default.yml](config/default.yml):

- NYC Department of Investigation
- NYC Comptroller
- NYS Comptroller
- NYC Independent Budget Office
- NYC Rules: proposed rules
- NYC Rules: adopted rules
- NYC Government Publications Portal

The inventory model supports PDF and non-PDF report pages. `document_url` is populated when the actual artifact is a PDF/DOCX/XLSX; otherwise the HTML page URL is the canonical heat URL.

## Commands

```bash
uv run nyc-report-heat discover
uv run nyc-report-heat rank --windows 1,7,30 --rank-window 7d
uv run nyc-report-heat daily --config config/default.yml
uv run nyc-report-heat show --limit 25
```

## Outputs

- [data/candidates.jsonl](data/candidates.jsonl): current canonical inventory.
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

The workflow uses only free public sources by default. The default providers are GDELT (news coverage matched on the report title) and Hacker News Algolia. Google News RSS is supported in the code path but disabled by default because it rate-limits datacenter/CI IPs aggressively (HTTP 503). The HTTP client uses a browser User-Agent, retry/backoff on 429/5xx, and a per-request cache that de-duplicates the identical provider query issued once per heat window. The runner uses bounded concurrency (`max_workers`), a bounded timeout (`request_timeout_seconds`), and a polite inter-request delay (`request_sleep_seconds`). Media Cloud and Bluesky are supported but need credentials/testing. Common Crawl is intentionally skipped for rolling-window heat because it is a historical crawl index, not a today/7d/30d attention signal.

## Heat Windows

Discovery and heat windows are separate. The manual `discover` command is the backfill path and defaults to `discovery_lookback_days: 90` so the first inventory does not go back forever. The `daily` command uses `daily_discovery_lookback_days: 14` to look only for recent additions, then merges those into the existing tracked inventory. A report is not dropped just because it fell off a source's recent/reports page. Every tracked candidate is scored in every configured heat window. The default config computes:

- `today` (`1` day)
- `7d`
- `30d`

The default ranking window is `7d`, set by `rank_window: 7d`. CSV outputs include per-window columns such as `heat_score_today`, `heat_score_7d`, `heat_score_30d`, `exact_url_mentions_7d`, and `filename_mentions_30d`.

Providers only contribute to a window when they can expose or infer a timestamp. GDELT uses its `timespan` parameter; Media Cloud, Google News, Bluesky, and Hacker News results are filtered by published/created time after exact URL or filename search.

Heat is recalculated for every tracked candidate on each daily run because the `today`, `7d`, and `30d` windows move forward every day. `--per-source` and `--limit` remain available for smoke runs, but the default `daily` and `rank` commands score the whole tracked inventory.

## Scoring Notes

Heat is an objective measure of public attention to the exact report. The primary
signal is **news coverage**: the count of news articles (via GDELT) that reference the
report by its exact title within each window. Exact-URL / filename / social pickups are
kept as high-confidence bonuses when they occur.

Current weights:

- `1.0 * coverage_mentions`, capped at 30 — news articles matching the report title
- `6.0 * exact_url_mentions`
- `3.0 * canonical_mentions`
- `2.0 * filename_mentions`, capped at 5
- `2.0 * social_exact_mentions`

The metric deliberately excludes source priority, OID relevance, topic keywords, and
human-interest heuristics. It is title/URL-anchored to the specific document, not the
broader topic. Coverage is windowed by article date, so the chart surfaces reports drawing
attention *now* — older reports with no recent coverage correctly read as cold. The daily
command writes grouped CSVs so the frontend can expose “all links,” “reports,” “rules,” and
“nonzero heat” without changing the metric.
