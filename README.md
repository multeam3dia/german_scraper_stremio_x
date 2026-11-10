# German Scraper Stremio Addon

This Stremio addon provides German streams for movies and series from multiple sources.

## Provider crawler foundation

The `providers/` package defines a shared provider contract and normalized
records for titles, seasons, episodes, language variants, and hosters.

- `AniWorldProvider` discovers series from the provider's genre pages.
- `SerienstreamProvider` discovers titles from the single-page alphabetical `/serien`
  index. The A-Z controls navigate within that page; the parser normalizes and
  deduplicates series URLs. A successful index fetch is considered a complete
  discovery pass, while unresolved IMDb IDs or per-title failures still make
  the overall crawl partial.
- IMDb identity is not guessed. Entries without a valid IMDb ID are skipped
  from the public catalog and counted as unresolved.
- The initial crawl verifies each listed title, reads its provider detail page
  for the IMDb identity, and stores the title/provider URL relationship. It does
  **not** enumerate seasons, episodes, or hosters.
- Season and episode lists are fetched only when Stremio opens a series. This
  reads the series page and season listing pages, but not individual episode
  pages. The result is cached in SQLite for 24 hours by default.
- An individual episode page is fetched only when playback is requested, and
  only from providers recorded as available for that IMDb title. Temporary
  direct media URLs are never persisted.
- Provider-side genre labels are retained as raw metadata only. Canonical
  Stremio genres are populated later by the TMDB/IMDb metadata enrichment step,
  so tags such as language flags are not mistakenly exposed as genres.

Run a crawl manually inside the application container:

```bash
python -m providers --provider aniworld
python -m providers --provider serienstream
```

The structure-cache lifetime can be changed with the container environment
variable `SERIES_STRUCTURE_CACHE_TTL_SECONDS` (default: `86400` seconds).
Resolved playback URLs are cached in process memory for 30 seconds by default
(`EPISODE_STREAM_CACHE_TTL_SECONDS`) and are never written to SQLite because
they may contain temporary access tokens.

## Provider identity resolution (TMDB)

Titles without a verified IMDb ID are retained in the SQLite
`unresolved_provider_titles` queue. The optional identity resolver uses TMDB
search results and TMDB external IDs to find an IMDb ID. It compares normalized
titles and release years, and requires both a high confidence score and a clear
margin over the next candidate. Ambiguous matches are left unresolved.

Configure one of the following container environment variables:

- `TMDB_READ_ACCESS_TOKEN` (recommended, TMDB API v4 read access token), or
- `TMDB_API_KEY` (TMDB API v3 key).

The resolver is report-only by default. First inspect candidates:

```bash
python -m identity_resolver --provider serienstream --limit 500
```

Only after reviewing the report, persist high-confidence matches:

```bash
python -m identity_resolver --provider serienstream --limit 500 --apply
```

Omit `--provider` to process both providers. The resolver records the TMDB
candidate and confidence details in title metadata. It does not silently
promote ambiguous candidates. Re-run the provider crawl later to refresh the
full season/episode structure for titles that have just been identified.

## TMDB metadata enrichment

After titles have a verified IMDb ID, enrich their display metadata and
canonical genres using that IMDb ID. This is separate from identity matching:
it never guesses or changes a title's IMDb identity.

```bash
python -m metadata_enricher --limit 500
```

The enrichment stores localized TMDB titles, poster and backdrop URLs,
descriptions, release years, and canonical genre names in SQLite. Provider
labels such as GerDub and GerSub remain separate and are never exposed as
canonical genres. Use `--force` to refresh titles that already have TMDB
metadata.

The crawler and enrichment commands are currently run manually inside the
application container. A scheduled crawl, availability reconciliation, and
automatic outbox delivery are not enabled yet.

## Features
- **Movies**: Scrapes **Filmpalast.to**, **MegaKino**, and **Kinox.to**, with AniWorld anime film collections as an additional source.
- **Series**: Scrapes **Serienstream.to**, **Kinox.to**, **MegaKino**, and **AniWorld**.
- **Robust Matching**: Uses title and year matching to find candidate streams.
- **Auto-Correction**: Retries searches with simplified titles where supported.
- **Captcha Handling**: Includes provider-specific handling from the underlying scraper projects.

## Installation (local development)
1. Install Python 3.10 or higher.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   patchright install chromium
   ```
3. Run the addon:
   ```bash
   PORT=10000 python main.py
   ```
4. In Stremio, add the manifest URL:
   `http://localhost:10000/manifest.json`

## AniWorld integration

AniWorld is queried alongside the existing providers for series and movies.
Series requests resolve the requested season and episode. Movie requests search
AniWorld anime film collections and match the film's German or English title.
The provider offers German Dub and Japanese audio with German subtitles when
available. Hoster links are resolved by the AniWorld episode models.

Movie discovery is limited to films listed in a matching AniWorld anime's
`/filme` collection. Since Stremio movie metadata and AniWorld franchise
collections do not always use identical titles, some movies may require
additional aliases or matching rules.

The AniWorld-derived models are based on
[AniWorld-Downloader](https://github.com/phoenixthrush/AniWorld-Downloader).
The upstream MIT license notice is retained in `LICENSE.AniWorld-Downloader`.

## Kinox integration (currently disabled)

Kinox is currently **not working**: its search/base-URL configuration is broken,
so searches can fail before a series or movie can be resolved. The integration
code is retained for future repair, but Kinox is disabled by default and is not
queried for movies or series.

To explicitly re-enable it for debugging, set this container environment variable:

\`KINOX_ENABLED=true\`

Only the values \"1\", \"true\", \"yes\" and \"on\" (case-insensitive) enable it.
Re-enabling does not fix the integration; expect lookup failures until the
Kinox search configuration and provider code have been repaired.

## Optional SerienStream account login

The addon can optionally sign in to SerienStream automatically before a
SerienStream lookup. Configure these container environment variables:

- `SERIENSTREAM_EMAIL`: the e-mail address used for the SerienStream account.
- `SERIENSTREAM_PASSWORD`: the account password.

For compatibility, `SERIENSTREAM_USERNAME` is also accepted as an alias for
`SERIENSTREAM_EMAIL`. The login is attempted only when both values are set.
The resulting session cookies are kept in the running process and refreshed
periodically; they are not written to the repository or a cookie file. If login
fails or the variables are absent, the existing provider/CAPTCHA flow continues.

Treat the password as a secret. Prefer Dockhand's secret/environment facilities
where available, and do not publish it in a compose file or logs. Automatic
login does not guarantee that SerienStream will stop presenting CAPTCHA
challenges.

## Docker / Dockhand / Pangolin

The container listens on port `10000` and runs FastAPI with one Uvicorn worker.
The Compose file is prepared for Dockhand stacks:

- It builds from the public GitHub branch `feature/aniworld-integration`.
- It joins the existing external Docker network `pangolin_frontend`.
- It uses `expose`, not `ports`; no host port is published.
- Pangolin should target container `german-scraper-stremio` on port `10000`.

In Pangolin, create a Public Resource for that container. Stremio must be able
to retrieve the manifest, catalogs, and stream responses without an
interactive login. The relevant application paths are `/manifest.json`,
`/catalog/*`, and `/stream/*`. Configure narrowly scoped bypass rules for
those paths, and keep other paths protected or denied. Check Pangolin's HTTP
request logs for any additional blocked requests before adding rules.

The stack expects that the Docker network `pangolin_frontend` already exists,
as described in the self-hosting guide. Do not create a second network with the
same purpose or publish port `10000` directly to the internet.

The health check requests `/manifest.json`. The container restarts unless
stopped manually.

## SQLite catalogs and optional MDBList export

The two Stremio catalogs are served directly from the persistent SQLite provider
index:

- **German Scraper X - Movies** (movie)
- **German Scraper X - Shows** (series)

Only titles with a verified IMDb ID and at least one provider marked as
available are returned. Catalog requests support title search, canonical genre
filtering, and Stremio pagination. Genre options in the manifest are generated
from the canonical genres currently stored in SQLite.

The systematic provider adapters currently cover series (AniWorld and
Serienstream). The Movies catalog endpoint is in place, but it will remain
empty until a movie provider has a validated full-catalog discovery adapter.
The existing playback-time movie stream lookup is unchanged.

MDBList is no longer the source of truth for Stremio catalog contents. The
existing optional exporter can still add a title to a configured MDBList static
list after a stream lookup returns at least one usable stream. This is useful
for external lists, but it does not control whether a title appears in the
SQLite-backed Stremio catalogs.

To enable that optional export, configure:

- `MDBLIST_API_KEY`
- `MDBLIST_MOVIE_LIST_NAME=German Scraper Stremio X - Movies`
- `MDBLIST_SHOW_LIST_NAME=German Scraper Stremio X - Shows`

The configured list names must match exactly. Numeric
`MDBLIST_MOVIE_LIST_ID` and `MDBLIST_SHOW_LIST_ID` settings can be used
instead. If these settings are absent, MDBList export is skipped.

## MDBList export behavior

The addon adds a movie or series to the corresponding MDBList static list
**only after a stream lookup has returned at least one usable stream**. MDBList
API writes run asynchronously and do not delay Stremio stream responses.
If the MDBList settings are absent or invalid, export is skipped. The exporter
uses the MDBList API to submit IMDb IDs under movies or shows.

The API key must remain a secret: do not commit it to Git or paste it into logs.
See the MDBList API documentation: https://api.mdblist.com/docs/.


## Persistent provider catalog index

The addon initializes a local SQLite database at `/app/data/catalog.sqlite3`
(or the path configured by `CATALOG_DB_PATH`). Docker Compose mounts
`./data` to `/app/data`, so the database survives container recreation.
Keep this directory on persistent storage and include it in backups.

SQLite is the source of truth for provider discovery and Stremio catalog
responses. The main tables are:

- `titles`: canonical media identity (IMDb ID), title, year and metadata.
- `provider_entries`: which provider exposes each title, its provider URL,
  last-seen/last-checked timestamps and availability state.
- `crawl_runs`: scan history, progress and errors per provider.
- `sync_outbox`: durable add/remove operations reserved for MDBList delivery.
- `unresolved_provider_titles`: provider titles without a verified IMDb ID,
  retained with title, URL, year, poster and metadata. These are not exposed in
  Stremio catalogs until identity has been resolved.
- `genres` and `title_genres`: normalized many-to-many canonical genre
  assignments.

The Stremio catalog endpoints read directly from SQLite. They return only
verified titles with at least one provider marked as available, and support
search, genre filtering, and pagination. The manifest's genre options are
generated from the canonical genres currently assigned to active titles.
Provider-specific labels such as GerDub and GerSub remain raw metadata and
are never treated as canonical genres.

The provider crawler stores titles without a valid IMDb ID in
`unresolved_provider_titles`. Repeated scans update the same provider URL
record and increment its attempt count. The TMDB identity resolver can report
high-confidence candidates and, with `--apply`, persist only matches that pass
its confidence and margin thresholds. Ambiguous records remain outside the
public catalog.

The provider crawler and TMDB metadata enrichment are implemented as manual
CLI jobs, not as a scheduled service. After a fully completed provider scan,
availability reconciliation increments misses only for entries not seen in
that scan. One missed scan is tolerated; after two consecutive completed scans
the provider entry is marked missing. A title remains visible while any other
provider still confirms it. Partial or failed scans never remove titles.
Automatic scheduling and the MDBList sync-outbox worker are still future work.
A title is admitted to the public catalog only after its provider detail page
has been resolved to a sufficiently certain IMDb identity.
## Simkl scrobbling (optional)

German Scraper X can receive Stremio player events and send them to Simkl. This is
separate from the SQLite catalog and provider availability: it does not change
which titles appear in Stremio or which provider is used for playback.

### Configuration

1. Create an AUTH V2 app in [Simkl Developer Settings](https://simkl.com/settings/developer/).
   Choose **TV, devices & command line**. This is the device/PIN flow intended
   for consoles and headless services; it does not require a client secret.
2. Add these variables to the stack's `.env` file (or Dockhand environment):

   ```dotenv
   SIMKL_CLIENT_ID=your_simkl_v2_client_id
   PLAYER_EVENT_PATH_TOKEN=replace_with_a_long_random_hex_secret
   ```

   Generate the path secret with `openssl rand -hex 32`. Do not reuse a password
   or publish this value.
3. Recreate the container so the new environment variables are loaded.
4. Install the tokenized manifest URL in Stremio:

   `https://your-public-host/PLAYER_EVENT_PATH_TOKEN/manifest.json`

   Replace the final path segment with the exact value configured above. The
   ordinary `/manifest.json` intentionally does **not** advertise player events.
   The secret path prevents arbitrary visitors from sending fake watch events
   to the connected Simkl account.
5. Open the matching setup page:

   `https://your-public-host/PLAYER_EVENT_PATH_TOKEN/simkl/connect`

   Press **Mit Simkl verbinden**, open the Simkl approval link and authorize the
   app. The page polls until the device flow completes.

### Behaviour and storage

- Stremio `start`, `pause` and `stop` events are forwarded to the matching
  Simkl scrobble endpoint. IMDb IDs and canonical season/episode numbers are used.
- Playback position and duration arrive in milliseconds and are converted to a
  percentage. Simkl's default stop threshold is 80%: a stop at or above it marks
  the item watched; an earlier stop saves a resumable playback.
- No periodic heartbeat is sent. Repeated consecutive start/pause states are
  coalesced because Stremio also emits those states when the user seeks.
- Access and refresh tokens are encrypted at rest. The encryption key is kept
  in `./data/simkl_tokens.key`; the encrypted credentials and event state are
  in `./data/simkl_tracking.sqlite3`. Back up both files together. The key file
  is created with owner-only permissions. An optional stable Fernet key can be
  supplied through `SIMKL_TOKEN_ENCRYPTION_KEY`.
- Keep the tokenized manifest URL private. Anyone who obtains it can submit
  player events to this one connected Simkl account.

The Stremio addon protocol defines `player` events for Stremio core 0.64 and
newer. Enable Stremio's **Share playback events** option where the client exposes
it, then reinstall the tokenized addon. Client support still needs to be verified
on each target build.
The current upstream GMCA Stremio backend does not yet dispatch these events;
GMCA needs a client-side change before Switch playback can be scrobbled through
this route.
