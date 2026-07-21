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
.venv/bin/parkrun-monitoring sync             # catalogue + weekly stats, notify on changes
.venv/bin/parkrun-monitoring fetch-history    # walk eventhistory summaries, stalest first
.venv/bin/parkrun-monitoring import-archive   # recover closed events from web.archive.org
.venv/bin/parkrun-monitoring push             # send fresh data to the canonical DB
.venv/bin/parkrun-monitoring status           # database summary
```

`sync` flags: `--catalogue-only`, `--stats-only`, `--no-notify`.
`fetch-history` flags: `--limit N` (default 25), `--delay S` (default 30s
between requests), `--event slug`, `--push-each`. Every pass stamps
`events.history_synced_at`, so `status` and the table itself always show
when each event's summary was last walked.

With `--push-each` every event is delivered to the canonical database the
moment it is fetched, so an interrupted long run keeps everything it has
collected. Pushes are watermark-bound deltas (one event ≈ a few hundred KB
of SQL), and the bundled ssh wrapper multiplexes them over a single
connection, so hundreds of pushes still authenticate only once.

## Closed events

`events.json` only lists what is running today, so closed venues — and the
countries that left parkrun, Russia in 2022 and France — disappear from it
without trace. `import-archive` walks archived copies of that file on
web.archive.org and restores the missing events, coordinates included, as
inactive rows tagged `catalogue_source='wayback'`. Live rows are never
overwritten.

```bash
parkrun-monitoring import-archive --country 79 --to-year 2022
```

Flags: `--country CODE`, `--from-year`, `--to-year`, `--delay` (default 1s
between snapshots). Coverage is bounded by what the archive captured —
snapshots start around 2019, so venues that closed earlier stay missing.

## Collector / canonical split

The website WAF may treat a server IP worse than a residential one. The tool
supports splitting roles: the *canonical* instance (server) runs the
catalogue sync on cron, while a *collector* instance (e.g. a laptop) runs
`sync` + `fetch-history` and delivers results with `push`. `push` exports
fresh rows as portable SQL and pipes them to `PM_PUSH_COMMAND` — any command
that applies stdin SQL to the canonical database (typically a small ssh
wrapper). A watermark in the local `kv` table keeps pushes incremental.

### Parallel queue workers

`work` runs the history walk as a claim-based queue worker, so several
workers — across processes and even across machines — never fetch the same
event twice:

```bash
parkrun-monitoring work --worker de --limit 40 --proxy http://127.0.0.1:10811
```

Each worker atomically claims the stalest free event (a lease in the
`events` table with a TTL, so a crashed worker's claim expires), fetches
its history, releases the claim and pauses `PM_WORKER_DELAY` seconds
(default 60, ±25% jitter) before the next one. Three consecutive failures
abort the worker — that usually means the WAF noticed the exit IP.
`--proxy` lets every worker use its own egress (e.g. one VPN country per
worker); [deploy/xray.collector.example.json](deploy/xray.collector.example.json)
is a template for such a multi-country proxy, and
[deploy/collector_run.sh](deploy/collector_run.sh) starts one worker per
entry in `PM_WORKERS` (`name:proxy,name:proxy,…`).

A remote worker coordinates through the same claims table by setting
`PM_CLAIM_COMMAND` — a shell hook (typically ssh) that forwards
`claim <worker> <ttl>` / `release <worker> <event>` to the canonical
instance, where they land in the `claim-one` / `release-claim` CLI
commands. `status-report` summarises recent `worker_runs` activity (and
sends it to VK when configured):

```bash
parkrun-monitoring status-report --hours 3
```

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
