# parkrun-monitoring

Tracks the public [parkrun](https://www.parkrun.com) events catalogue and
weekly per-country statistics in a compact local SQLite database, and reports
catalogue changes (new / disappeared / renamed events) to VK or stdout.

Data sources — two official, cheap, CDN-friendly endpoints (no scraping of
result protocols, no load on the parkrun website):

| Source | What it gives |
|---|---|
| `images.parkrun.com/events.json` | all active events worldwide: slug, names, country, series (5k / junior), coordinates |
| `results-service.parkrun.com/.../globalChartNumRunnersAndEvents.php` | weekly totals (events / finishers / volunteers) since 2004, worldwide and per country — including countries that left parkrun (Russia, France) |

A full sync makes ~25 HTTP requests with a configurable delay.

Note: the results-service endpoint rejects non-browser user agents with 403,
so the client defaults to a browser-like UA (override with `PM_USER_AGENT`).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # optionally set VK_TOKEN / VK_PEER_ID
```

## Usage

```bash
.venv/bin/parkrun-monitoring sync            # catalogue + weekly stats, notify on changes
.venv/bin/parkrun-monitoring fetch-history   # walk eventhistory summaries, stalest first
.venv/bin/parkrun-monitoring push            # send fresh data to the canonical DB
.venv/bin/parkrun-monitoring status          # database summary
```

`sync` flags: `--catalogue-only`, `--stats-only`, `--no-notify`.
`fetch-history` flags: `--limit N` (default 25), `--delay S` (default 30s
between requests), `--event slug`. Every pass stamps
`events.history_synced_at`, so `status` and the table itself always show
when each event's summary was last walked.

## Collector / canonical split

The website WAF may treat a server IP worse than a residential one. The tool
supports splitting roles: the *canonical* instance (server) runs the
catalogue sync on cron, while a *collector* instance (e.g. a laptop) runs
`sync` + `fetch-history` and delivers results with `push`. `push` exports
fresh rows as portable SQL and pipes them to `PM_PUSH_COMMAND` — any command
that applies stdin SQL to the canonical database (typically a small ssh
wrapper). A watermark in the local `kv` table keeps pushes incremental.

The first run imports the catalogue as a baseline (no change spam). Every
following run:

* upserts the events catalogue and appends any differences to `event_changes`
  (`added` / `removed` / `reappeared` / `modified` with a field-level JSON diff);
* upserts weekly statistics rows, touching only new or revised weeks;
* sends one VK message when something changed (or prints it without a token).

## Schedule

Three times a week is more than enough — the catalogue changes a few times a
month. Cron example (Mon / Wed / Sat mornings):

```cron
0 9 * * 1,3,6 cd /path/to/parkrun-monitoring && .venv/bin/parkrun-monitoring sync >> data/sync.log 2>&1
```

On macOS, a launchd agent survives sleep better than cron — see
[deploy/launchd.example.plist](deploy/launchd.example.plist).

## Coordinating with other parkrun tooling

If the same host runs other parkrun scrapers, set `PM_GATE_COMMAND` to a
shell command that exits non-zero while syncing is unwise (for example,
while another tool is serving a parkrun ban cooldown). The sync then stands
down entirely and records a `skipped` run.

Note that the two sources this tool polls out of the box (the CDN-hosted
events catalogue and the results-service chart endpoint) live outside the
WAF surface that bans event/results pages, so a ban there does not require
gating them — the gate is aimed at deployments that extend the tool to
fetch regular website pages:

```sh
#!/bin/sh
# Example gate: skip while a ban cooldown timestamp is in the future.
until=$(redis-cli GET parkrun:fetch:ban_cooldown_until | tr -d '\r')
[ -z "$until" ] && exit 0
awk -v u="$until" -v n="$(date +%s)" 'BEGIN { exit (n < u) ? 1 : 0 }'
```

## Database

SQLite file at `data/parkrun.db` (override with `PM_DB_PATH`). Schema:
`events` (one row per event, activity flags), `event_changes` (append-only
log), `country_weekly_stats` (`WITHOUT ROWID`, keyed by country + week),
`countries`, `sync_runs`. A full worldwide dataset is ~3 MB.

## Fair use

This tool polls two aggregate endpoints a few times a week — orders of
magnitude below normal browser traffic. If you fork it for heavier data
collection, respect parkrun's infrastructure: keep delays generous, cache
everything, and don't fetch what you already have.
