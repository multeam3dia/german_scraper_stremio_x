# Conservative catalog crawling

The catalog crawl is intentionally conservative. It reads each provider's complete
series index once, then opens detail pages only for URLs that are new or still lack
a verified IMDb identity.

## Run

Run both catalog providers sequentially:

    python -m crawl_catalog all

Run one provider only:

    python -m crawl_catalog aniworld
    python -m crawl_catalog serienstream

First validate both indexes without opening detail pages:

    python -m crawl_catalog all --index-only

The index-only mode performs and validates the complete index scans, but does not
change availability status or remove catalog entries. It defers all new and
unresolved detail pages to a later full run.

The global SQLite lease prevents two catalog crawls from running at the same time,
even if separate scheduled jobs start concurrently.

## Request policy

- One request at a time per crawl session.
- Random delay between requests: 5–12 seconds by default.
- Configure the delay with CATALOG_CRAWL_DELAY_MIN and
  CATALOG_CRAWL_DELAY_MAX (seconds).
- Network errors are retried at most twice after the initial attempt; repeated
  failures then pause the provider for 15 minutes.
- HTTP 5xx responses use a short exponential backoff; persistent 5xx responses
  pause the provider for 30 minutes.
- HTTP 403 stops the provider crawl and starts a six-hour cooldown.
- HTTP 429 stops the provider crawl and honours Retry-After, with at least
  five minutes of cooldown.
- A detected CAPTCHA/anti-bot challenge stops the crawl and starts a 12-hour
  cooldown.
- Cooldowns and crawl locks are stored in the catalog SQLite database and
  survive container restarts.

The crawler does not rotate proxies, identities, or user agents to work around
provider restrictions. A block signal means stop and wait.

## Incremental behaviour

- Existing provider URLs are marked as seen from the index; their detail pages
  are not fetched again.
- New URLs are opened once to extract the provider's IMDb ID and supporting data.
- Unresolved IMDb records are retried with increasing intervals (14, 28, 56,
  then up to 90 days).
- The crawl resumes naturally after interruption: a later run reads the index
  again and skips entries already recorded.
- A provider is reconciled for missing titles only after its entire index has
  been consumed. A blocked or interrupted run never removes catalog entries.

## First run expectations

The initial run can take several hours because every previously unknown series
needs one detail-page request and the default delay is deliberately cautious.
Subsequent runs should be much shorter because known URLs are handled from the
index without detail-page requests.

## Detail-page batch limit

Each provider is limited to **200 detail-page attempts per run** by default.
The limit applies independently to AniWorld and SerienStream, so `all` can
process up to 200 detail pages from each provider in one invocation.

Configure the per-provider limit with this environment variable:

    CATALOG_CRAWL_MAX_DETAILS_PER_RUN=200

Or override it for one invocation:

    python -m crawl_catalog all --max-details 100

When the limit is reached, the crawler continues reading the index so it can
still recognize known URLs, but it does not open further detail pages. The
provider run is reported as `partial` with `detail_limit_reached=true`, and
availability reconciliation is skipped. The next run reads the index again
and skips provider URLs already recorded in SQLite. A budget-limited AniWorld
run does not prevent SerienStream from receiving its own batch in the same
`all` invocation. A safety block or an incomplete index still stops the
batch as before.
