import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from scraper import get_movie_streams, get_series_streams
from aniworld.config import logger

ADDON_ID = "org.stremio.german.scraper"
VERSION = "1.0.0"

MANIFEST = {
    "id": ADDON_ID,
    "version": VERSION,
    "name": "German Scraper",
    "description": "German streams from AniWorld, Filmpalast, Serienstream, MegaKino, and Kinox.",
    "logo": "https://www.stremio.com/static/images/stremio-logo.png",
    "resources": ["stream"],
    "types": ["movie", "series"],
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
    logger.info(
        "SERIENSTREAM-AUTH-DIAGNOSTIC-V2 | email=%s, password=%s",
        "set" if email_set else "MISSING",
        "set" if password_set else "MISSING",
    )

@app.get("/manifest.json")
async def get_manifest():
    return MANIFEST

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
            logger.info(f"Adding stream for {id}: {s.get('name')} -> {s.get('url')}")
            unique_streams.append(s)
            seen_urls.add(s["url"])

    return {"streams": unique_streams}

@app.get("/")
async def root():
    return RedirectResponse(url="/manifest.json")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
