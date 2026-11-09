import uvicorn
from urllib.parse import parse_qs, urlparse
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from scraper import get_movie_streams, get_metadata
from providers.lazy import get_series_meta, get_series_streams_for_episode
from aniworld.config import logger
from mdblist import queue_verified_title
from catalog_db import get_catalog_titles, init_db, list_genres

ADDON_ID = "org.stremio.german.scraper"
VERSION = "1.2.0"

MANIFEST = {
    "id": ADDON_ID,
    "version": VERSION,
    "name": "German Scraper",
    "description": "German streams and verified availability catalogs from AniWorld, Serienstream, Filmpalast, and MegaKino.",
    "logo": "https://www.stremio.com/static/images/stremio-logo.png",
    "resources": ["stream", "catalog", "meta"],
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
async def initialize_catalog_database():
    """Initialize the persistent provider catalog index."""
    init_db()
    logger.info("Persistent catalog database initialized")


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

def _catalog_manifest_entry(media_type: str, catalog_id: str, name: str) -> dict:
    genres = [item["name"] for item in list_genres(media_type)]
    extra = [
        {"name": "search", "isRequired": False},
        {"name": "skip", "isRequired": False},
    ]
    if genres:
        extra.insert(1, {"name": "genre", "isRequired": False})
    entry = {
        "type": media_type,
        "id": catalog_id,
        "name": name,
        "extra": extra,
    }
    if genres:
        entry["genres"] = genres
    return entry


def _catalog_meta(record: dict) -> dict:
    """Convert one verified SQLite title into a Stremio Meta Preview Object."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    tmdb = metadata.get("tmdb") if isinstance(metadata.get("tmdb"), dict) else {}

    meta = {
        "id": record["imdb_id"],
        "type": record["media_type"],
        "name": str(tmdb.get("title") or record.get("title") or record["imdb_id"]),
        "posterShape": "poster",
    }
    poster = tmdb.get("poster_url") or metadata.get("poster_url") or metadata.get("poster")
    background = (
        tmdb.get("backdrop_url")
        or metadata.get("backdrop_url")
        or metadata.get("background")
    )
    description = tmdb.get("overview") or metadata.get("description")
    year = record.get("year") or tmdb.get("year") or metadata.get("release_year")
    genres = record.get("genres") or tmdb.get("genres") or []

    if poster:
        meta["poster"] = poster
    if background:
        meta["background"] = background
    if description:
        meta["description"] = str(description)
    if year:
        meta["releaseInfo"] = str(year)[:4]
    if isinstance(genres, list) and genres:
        meta["genres"] = [str(value) for value in genres if value]
    imdb_rating = tmdb.get("imdb_rating") or metadata.get("imdb_rating")
    try:
        if imdb_rating is not None:
            meta["imdbRating"] = str(round(float(imdb_rating), 1))
    except (TypeError, ValueError):
        pass
    return meta


def _catalog_response(
    media_type: str,
    catalog_id: str,
    *,
    skip: object = 0,
    search: str = "",
    genre: str = "",
) -> dict:
    catalog_types = {
        ("movie", "german_scraper_movies"): "movie",
        ("series", "german_scraper_shows"): "series",
    }
    expected_type = catalog_types.get((media_type, catalog_id))
    if not expected_type:
        return {"metas": []}
    try:
        records = get_catalog_titles(
            expected_type,
            skip=skip,
            limit=100,
            search=search,
            genre=genre,
        )
        return {"metas": [_catalog_meta(record) for record in records]}
    except Exception as exc:
        logger.error(
            "SQLite catalog request failed for %s: %s: %s",
            catalog_id, exc.__class__.__name__, str(exc),
        )
        return {"metas": []}


@app.get("/manifest.json")
async def get_manifest():
    manifest = dict(MANIFEST)
    manifest["catalogs"] = [
        _catalog_manifest_entry(
            "movie", "german_scraper_movies", "German Scraper X - Movies"
        ),
        _catalog_manifest_entry(
            "series", "german_scraper_shows", "German Scraper X - Shows"
        ),
    ]
    return manifest


@app.get("/catalog/{type}/{catalog_id}.json")
def get_catalog(
    type: str,
    catalog_id: str,
    skip: int = 0,
    search: str = "",
    genre: str = "",
):
    """Serve verified catalog entries directly from the persistent SQLite index."""
    return _catalog_response(
        type, catalog_id, skip=skip, search=search, genre=genre
    )


@app.get("/catalog/{type}/{catalog_id}/{extra}.json")
def get_catalog_with_extra(type: str, catalog_id: str, extra: str):
    """Handle Stremio's path-encoded search, genre and pagination arguments."""
    values = parse_qs(extra, keep_blank_values=True)
    return _catalog_response(
        type,
        catalog_id,
        skip=values.get("skip", ["0"])[-1],
        search=values.get("search", [""])[-1],
        genre=values.get("genre", [""])[-1],
    )


@app.get("/meta/{type}/{id}.json")
def get_meta(type: str, id: str):
    """Return provider-backed episode metadata for series, on demand."""
    imdb_id = id.split(":", 1)[0]
    if type == "series":
        try:
            return get_series_meta(imdb_id)
        except Exception as exc:
            logger.error(
                "Series metadata request failed for %s: %s: %s",
                imdb_id, exc.__class__.__name__, str(exc),
            )
            fallback = get_metadata("series", imdb_id)
            if isinstance(fallback, dict):
                fallback = dict(fallback)
                fallback["videos"] = []
                return {"meta": fallback}
            return {"meta": None}

    if type == "movie":
        meta = get_metadata("movie", imdb_id)
        return {"meta": meta} if isinstance(meta, dict) else {"meta": None}

    return {"meta": None}


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
                streams = get_series_streams_for_episode(imdb_id, season, episode)
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
