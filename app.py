import uvicorn
from urllib.parse import urlparse
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from scraper import get_movie_streams, get_series_streams
from aniworld.config import logger
from mdblist import queue_verified_title, get_catalog_metas

ADDON_ID = "org.stremio.german.scraper"
VERSION = "1.0.0"

MANIFEST = {
    "id": ADDON_ID,
    "version": VERSION,
    "name": "German Scraper",
    "description": "German streams from AniWorld, Filmpalast, Serienstream, MegaKino, and Kinox.",
    "logo": "https://www.stremio.com/static/images/stremio-logo.png",
    "resources": ["stream", "catalog"],
    "types": ["movie", "series"],
    "catalogs": [
        {"type": "movie", "id": "german_scraper_movies", "name": "German Scraper X - Movies"},
        {"type": "series", "id": "german_scraper_shows", "name": "German Scraper X - Shows"},
    ],
    "idPrefixes": ["tt"],
}

app = FastAPI(title="German Scraper Stremio Addon", version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def log_serienstream_login_configuration():
    """Report whether login credentials are configured, without exposing them."""
    import os

    email_set = bool(
        os.getenv("SERIENSTREAM_EMAIL") or os.getenv("SERIENSTREAM_USERNAME")
    )
    password_set = bool(os.getenv("SERIENSTREAM_PASSWORD"))
    print(
        "SERIENSTREAM-AUTH-DIAGNOSTIC-V3 | email={} password={}".format(
            "set" if email_set else "MISSING",
            "set" if password_set else "MISSING",
        ),
        flush=True,
    )

@app.get("/manifest.json")
async def get_manifest():
    return MANIFEST

@app.get("/catalog/{type}/{catalog_id}.json")
def get_catalog(type: str, catalog_id: str, skip: int = 0, search: str = ""):
    """Return MDBList static-list items as Stremio catalog metadata."""
    catalog_types = {
        ("movie", "german_scraper_movies"): "movie",
        ("series", "german_scraper_shows"): "series",
    }
    media_type = catalog_types.get((type, catalog_id))
    if not media_type:
        return {"metas": []}
    try:
        return {"metas": get_catalog_metas(media_type, skip=max(0, skip), search=search)}
    except Exception as exc:
        logger.error("MDBList catalog request failed for %s: %s", catalog_id, type(exc).__name__)
        return {"metas": []}


@app.get("/stream/{type}/{id}.json")
def get_stream(type: str, id: str):
    logger.info(f"Stream request: Type={type}, ID={id}")
    streams = []
    try:
        if type == "movie":
            streams = get_movie_streams(id)
        elif type == "series":
            parts = id.split(":")
            if len(parts) >= 3:
                imdb_id = parts[0]
                season = int(parts[1])
                episode = int(parts[2])
                streams = get_series_streams(imdb_id, season, episode)
    except Exception as e:
        logger.error(f"Error handling stream request for {id}: {e}")

    unique_streams = []
    seen_urls = set()
    for s in streams:
        if s.get("url") and s["url"] not in seen_urls:
            # Signed media URLs contain temporary access tokens; never write them to logs.
            stream_host = urlparse(s["url"]).netloc or "unknown-host"
            logger.info(
                "Adding stream for %s: %s (host=%s)",
                id,
                s.get("name"),
                stream_host,
            )
            unique_streams.append(s)
            seen_urls.add(s["url"])

    if unique_streams:
        # Export only after at least one source has yielded a usable stream.
        media_id = id.split(":", 1)[0] if type == "series" else id
        queue_verified_title(type, media_id)

    return {"streams": unique_streams}

@app.get("/")
async def root():
    return RedirectResponse(url="/manifest.json")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
