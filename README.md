# German Scraper Stremio Addon

This Stremio addon provides German streams for movies and series from multiple sources.

## Features
- **Movies**: Scrapes **Filmpalast.to**, **MegaKino**, and **Kinox.to**.
- **Series**: Scrapes **Serienstream.to**, **Kinox.to**, and **MegaKino**.
- **Robust Matching**: Uses advanced fuzzy title and year matching to find the best streams.
- **Auto-Correction**: Automatically retries searches with simplified titles if no results are found.
- **Captcha Handling**: Built-in support for solving provider captchas (e.g. on Serienstream).

## Installation
1. Install Python 3.10 or higher.
2. Install dependencies:
   ```bash
   pip install fastapi uvicorn niquests niquests[brotli] certifi packaging python-dotenv curl_cffi patchright
   patchright install chromium
   ```
3. Run the addon:
   ```bash
   python main.py
   ```
4. In Stremio, go to Addons -> Community Addons -> Add outside addon and enter:
   `http://localhost:8080/manifest.json`

## Credit
Based on logic from the AniWorld-Downloader project.


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
[AniWorld-Downloader](https://github.com/phoenixthrush/AniWorld-Downloader);
retain the upstream MIT license notice when redistributing those files.

## Docker / Dockhand

The image listens on container port `10000` and starts the FastAPI add-on with
one Uvicorn worker. Use Docker Compose in Dockhand and set the Pangolin network
name in `PANGOLIN_NETWORK` to the existing Docker network that can reach the
Pangolin public-access target. The Compose stack intentionally does not publish
a host port; Pangolin should reach the container internally.

The container uses `restart: unless-stopped`, so Docker restarts it after VPS
reboots. The health check requests `/manifest.json`.
