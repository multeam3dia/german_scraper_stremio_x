# German Scraper Stremio Addon

This Stremio addon provides German streams for movies and series from multiple sources.

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
to retrieve the manifest and stream responses without an interactive login.
For this addon, the relevant application paths are `/manifest.json` and
`/stream/*`. Configure narrowly scoped bypass rules for those paths, and keep
other paths protected or denied. Check Pangolin's HTTP request logs for any
additional blocked requests before adding rules.

The stack expects that the Docker network `pangolin_frontend` already exists,
as described in the self-hosting guide. Do not create a second network with the
same purpose or publish port `10000` directly to the internet.

The health check requests `/manifest.json`. The container restarts unless
stopped manually.

## MDBList catalogs and automatic list export

The addon exposes two Stremio catalogs backed by the configured MDBList static lists:

- **German Scraper X - Movies** (movie)
- **German Scraper X - Shows** (series)

The catalog endpoints read list items from MDBList and return Stremio metadata using
IMDb IDs. Posters, backgrounds, descriptions, release years, genres and IMDb ratings
are included when MDBList provides them. Catalog pages use Stremio's skip value
and are cached in memory for five minutes.

The existing background exporter remains enabled: after a stream lookup returns at
least one usable stream, the title is queued for the matching static list. This means
the lists can be curated manually in MDBList and also grow automatically as titles
are successfully found by the scraper.

Required Dockhand environment variables:

- MDBLIST_API_KEY
- MDBLIST_MOVIE_LIST_NAME=German Scraper Stremio X - Movies
- MDBLIST_SHOW_LIST_NAME=German Scraper Stremio X - Shows

The configured list names must match exactly and the MDBList list types must be
Movie and Show respectively. Numeric MDBLIST_MOVIE_LIST_ID and
MDBLIST_SHOW_LIST_ID settings can be used instead of names.

## MDBList export behavior

The addon automatically adds a movie or series to the corresponding static list
**only after a stream lookup has returned at least one usable stream**. This avoids
populating the lists with titles that our German scraper sources do not currently
serve. It does not crawl all provider catalogs in the background.

MDBList API writes run asynchronously and do not delay Stremio stream responses.
Each title is submitted once per running process after a successful API result.
The exporter uses POST /lists/{listid}/items/add and submits IMDb IDs under
movies or shows. If the MDBList settings are absent or invalid, export is skipped.

The API key must remain a secret: do not commit it to Git or paste it into logs.
See the MDBList API documentation: https://api.mdblist.com/docs/.
