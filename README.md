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
