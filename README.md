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

## Optional MDBList catalog export (proof of concept)

The add-on can push IMDb IDs that have **already been verified by a scraper
source** into two MDBList static lists. This is an opt-in bridge for testing;
it does not yet crawl provider catalogs or automatically discover all titles.

Create two static lists in MDBList (one for movies and one for shows), then set
these environment variables in Dockhand:

- `MDBLIST_API_KEY`: API key from MDBList Preferences.
- `MDBLIST_MOVIE_LIST_ID`: numeric ID of the movie static list.
- `MDBLIST_SHOW_LIST_ID`: numeric ID of the show static list.
- `MDBLIST_SYNC_TOKEN`: a separate long random token protecting the local
  export endpoint. Do not reuse the MDBList API key.

After redeploying, send a POST request to
`/admin/mdblist/sync` with the header
`X-MDBList-Sync-Token: <your-sync-token>` and a JSON body such as:

```json
{
  "movies": ["tt0111161"],
  "shows": ["tt0108778"]
}
```

The example IDs are test fixtures only; use IDs for titles that you have
actually confirmed in the relevant German scraper source. The endpoint adds
items only; it never removes entries from your lists. It validates IMDb ID
format, de-duplicates each request, and limits each media type to 50 IDs.
API keys and sync tokens must never be committed to Git or pasted into logs.

The MDBList API currently documents static-list item additions as
`POST /lists/{listid}/items/add` with IMDb IDs in a `movies` or `shows`
array. See the [MDBList API documentation](https://api.mdblist.com/docs/).
