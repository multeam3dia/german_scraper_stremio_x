import hmac
import os
import uvicorn
from urllib.parse import urlparse
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from scraper import get_movie_streams, get_series_streams
from aniworld.config import logger
from mdblist import MDBListAPIError, MDBListConfigurationError, sync_imdb_ids

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

    return {"streams": unique_streams}


class MDBListSyncRequest(BaseModel):
    """IMDb IDs already confirmed by one of the configured scraper sources."""

    movies: list[str] = Field(default_factory=list)
    shows: list[str] = Field(default_factory=list)


@app.post("/admin/mdblist/sync")
def sync_mdblist_catalog(
    payload: MDBListSyncRequest,
    x_mdblist_sync_token: str = Header(default=""),
):
    """Add verified IMDb IDs to the configured MDBList static lists.

    This endpoint is disabled unless all MDBList settings are configured and
    protected by a separate high-entropy admin token.
    """
    expected_token = os.getenv("MDBLIST_SYNC_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(status_code=503, detail="MDBList sync is not configured")
    if not hmac.compare_digest(x_mdblist_sync_token, expected_token):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        return sync_imdb_ids(payload.movies, payload.shows)
    except MDBListConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except MDBListAPIError:
        logger.warning("MDBList sync request failed")
        raise HTTPException(status_code=502, detail="MDBList API request failed")


@app.get("/")
async def root():
    return RedirectResponse(url="/manifest.json")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
