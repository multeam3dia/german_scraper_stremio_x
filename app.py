import os
import uvicorn
from urllib.parse import parse_qs, urlparse
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from scraper import get_movie_streams, get_metadata
from providers.lazy import get_series_meta, get_series_streams_for_episode
from aniworld.config import logger
from mdblist import queue_verified_title
from mdblist_tracking import (
    get_mdblist_playback,
    get_mdblist_status,
    init_db as init_mdblist_tracking_db,
    scrobble_event as mdblist_scrobble_event,
)
from catalog_db import get_catalog_titles, init_db, list_genres
from playback_events import PlaybackEventError, playback_event_from_payload, stremio_player_event
from simkl_tracking import (
    SimklError,
    get_simkl_status,
    init_db as init_simkl_db,
    poll_device_authorization,
    scrobble_event,
    start_device_authorization,
)

ADDON_ID = "org.stremio.german.scraper"
VERSION = "1.4.0"

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


def _local_catalog_enabled() -> bool:
    """Whether the optional SQLite-backed Stremio catalogs are advertised."""
    return os.getenv("ENABLE_LOCAL_CATALOG", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def tokenized_addon_paths(request: Request, call_next):
    """Expose event resources only through a private, tokenized addon URL."""
    token = os.getenv("PLAYER_EVENT_PATH_TOKEN", "").strip().strip("/")
    request.state.tokenized_addon = False
    if token:
        prefix = f"/{token}"
        path = request.scope.get("path", "")
        if path == prefix or path.startswith(prefix + "/"):
            request.scope["path"] = path[len(prefix):] or "/"
            raw_path = request.scope.get("raw_path")
            if raw_path:
                request.scope["raw_path"] = raw_path[len(prefix.encode("utf-8")):] or b"/"
            request.state.tokenized_addon = True
    return await call_next(request)

@app.on_event("startup")
async def initialize_catalog_database():
    """Initialize the optional local catalog without making it an app dependency."""
    if not _local_catalog_enabled():
        logger.info("Local SQLite catalog disabled; live provider playback remains enabled")
        return
    try:
        init_db()
        logger.info("Persistent catalog database initialized")
    except Exception as exc:
        logger.warning(
            "Local SQLite catalog could not be initialized; continuing without it: %s",
            type(exc).__name__,
        )


@app.on_event("startup")
async def initialize_simkl_tracking_database():
    """Initialize encrypted token storage and playback-event state."""
    init_simkl_db()


@app.on_event("startup")
async def initialize_mdblist_tracking_database():
    """Initialize MDBList playback event deduplication state."""
    init_mdblist_tracking_db()


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
    try:
        genres = [item["name"] for item in list_genres(media_type)]
    except Exception as exc:
        logger.warning(
            "Could not load optional catalog genres for %s: %s",
            media_type,
            type(exc).__name__,
        )
        genres = []
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
    if not expected_type or not _local_catalog_enabled():
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


def _manifest_response(enable_player_events: bool = False) -> dict:
    manifest = dict(MANIFEST)
    manifest["resources"] = list(MANIFEST["resources"])
    if enable_player_events:
        manifest["resources"].append({
            "name": "player",
            "types": ["movie", "series"],
            "idPrefixes": ["tt"],
        })
    manifest["catalogs"] = (
        [
            _catalog_manifest_entry(
                "movie", "german_scraper_movies", "German Scraper X - Movies"
            ),
            _catalog_manifest_entry(
                "series", "german_scraper_shows", "German Scraper X - Shows"
            ),
        ]
        if _local_catalog_enabled()
        else []
    )
    return manifest


@app.get("/manifest.json")
async def get_manifest(request: Request):
    token_configured = bool(os.getenv("PLAYER_EVENT_PATH_TOKEN", "").strip())
    tracking_configured = bool(
        os.getenv("SIMKL_CLIENT_ID", "").strip()
        or os.getenv("MDBLIST_API_KEY", "").strip()
    )
    enable_player_events = (
        token_configured
        and tracking_configured
        and getattr(request.state, "tokenized_addon", False)
    )
    return _manifest_response(enable_player_events=enable_player_events)


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
                # Preserve Cinemeta's canonical episodes. A provider catalog or
                # structure-cache failure must not hide the series episode list.
                return {"meta": dict(fallback)}
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


def _require_tokenized_addon(request: Request) -> None:
    token_configured = bool(os.getenv("PLAYER_EVENT_PATH_TOKEN", "").strip())
    if not token_configured or not getattr(request.state, "tokenized_addon", False):
        raise HTTPException(status_code=404, detail="Not found")


def _dispatch_tracking_event(event):
    """Send one normalized event independently to every configured tracker."""
    try:
        simkl_result = scrobble_event(event)
    except SimklError:
        simkl_result = {"status": "error", "reason": "simkl_error"}
    except Exception as exc:
        logger.warning("Unexpected Simkl tracking error: %s", exc.__class__.__name__)
        simkl_result = {"status": "error", "reason": "unexpected_error"}

    try:
        mdblist_result = mdblist_scrobble_event(event)
    except Exception as exc:
        logger.warning("Unexpected MDBList tracking error: %s", exc.__class__.__name__)
        mdblist_result = {"status": "error", "reason": "unexpected_error"}

    providers = {"simkl": simkl_result, "mdblist": mdblist_result}
    statuses = [str(result.get("status") or "unknown") for result in providers.values()]
    successful = [
        name for name, result in providers.items()
        if result.get("status") in {"sent", "duplicate_watch"}
    ]
    if any(providers[name].get("status") == "sent" for name in successful):
        overall_status = "sent"
    elif successful:
        overall_status = "duplicate_watch"
    elif "rate_limited" in statuses:
        overall_status = "rate_limited"
    elif "error" in statuses:
        overall_status = "error"
    elif "ignored" in statuses:
        overall_status = "ignored"
    else:
        overall_status = "not_connected"

    primary_name = successful[0] if successful else "simkl"
    primary = dict(providers[primary_name])
    primary["status"] = overall_status
    primary["providers"] = providers
    return primary


@app.get("/player/{type}/{video_id}/{extra}.json")
def get_player_event(request: Request, type: str, video_id: str, extra: str):
    """Adapt Stremio player events to the shared playback contract."""
    _require_tokenized_addon(request)
    action = ""
    try:
        values = parse_qs(extra, keep_blank_values=True)
        action = values.get("action", [""])[-1].strip().lower()
        try:
            current_time_ms = max(0, int(values.get("currentTime", ["0"])[-1] or 0))
        except (TypeError, ValueError):
            current_time_ms = 0
        try:
            duration_ms = max(0, int(values.get("duration", ["0"])[-1] or 0))
        except (TypeError, ValueError):
            duration_ms = 0

        event = stremio_player_event(
            type, video_id, action, current_time_ms, duration_ms
        )
        logger.info(
            "PLAYBACK-TRACKING-DIAGNOSTIC received: media=%s action=%s "
            "current_time_ms=%s duration_ms=%s progress=%s",
            event.media_key,
            event.action,
            current_time_ms,
            duration_ms,
            event.progress,
        )

        result = _dispatch_tracking_event(event)
        result_status = (
            str(result.get("status") or "unknown")
            if isinstance(result, dict)
            else "invalid_response"
        )
        result_action = (
            result.get("action") if isinstance(result, dict) else None
        )
        result_reason = (
            result.get("reason") if isinstance(result, dict) else None
        )
        http_status = (
            result.get("http_status") if isinstance(result, dict) else None
        )
        provider_results = result.get("providers", {}) if isinstance(result, dict) else {}
        simkl_status = provider_results.get("simkl", {}).get("status")
        mdblist_status = provider_results.get("mdblist", {}).get("status")
        mdblist_http_status = provider_results.get("mdblist", {}).get("http_status")
        log_message = (
            "PLAYBACK-TRACKING-DIAGNOSTIC result: media=%s action=%s "
            "status=%s simkl=%s mdblist=%s mdblist_http=%s "
            "tracker_action=%s reason=%s http_status=%s progress=%s"
        )
        log_args = (
            event.media_key,
            event.action,
            result_status,
            simkl_status,
            mdblist_status,
            mdblist_http_status,
            result_action,
            result_reason,
            http_status,
            event.progress,
        )
        if result_status in {"sent", "duplicate_watch"}:
            logger.info(log_message, *log_args)
        elif result_status == "ignored":
            logger.info(log_message, *log_args)
        else:
            logger.warning(log_message, *log_args)
        return result
    except PlaybackEventError as exc:
        logger.warning(
            "PLAYBACK-TRACKING-DIAGNOSTIC ignored before tracking: "
            "media_type=%s action=%s reason=%s",
            type,
            action or "unknown",
            exc.reason,
        )
        return {"status": "ignored", "reason": exc.reason}
    except SimklError as exc:
        logger.warning(
            "PLAYBACK-TRACKING-DIAGNOSTIC Simkl exception: "
            "media_type=%s action=%s error_type=%s",
            type,
            action or "unknown",
            exc.__class__.__name__,
        )
        return {"status": "error"}
    except Exception as exc:
        logger.error(
            "PLAYBACK-TRACKING-DIAGNOSTIC unexpected error: "
            "media_type=%s action=%s error_type=%s",
            type,
            action or "unknown",
            exc.__class__.__name__,
        )
        return {"status": "error"}


@app.post("/playback/event")
async def ingest_playback_event(request: Request):
    """Receive source-neutral events from clients such as a future GMCA adapter."""
    _require_tokenized_addon(request)
    try:
        payload = await request.json()
        event = playback_event_from_payload(payload)
    except PlaybackEventError as exc:
        return {"status": "ignored", "reason": exc.reason}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON playback event")
    try:
        return _dispatch_tracking_event(event)
    except SimklError as exc:
        logger.warning("Playback event could not be processed: %s", str(exc))
        return {"status": "error"}
    except Exception as exc:
        logger.error(
            "Unexpected normalized playback event error: %s",
            exc.__class__.__name__,
        )
        return {"status": "error"}


@app.get("/simkl/connect")
def simkl_connect_page(request: Request):
    """Small PIN-flow page; the private addon path protects setup actions."""
    _require_tokenized_addon(request)
    return HTMLResponse(
        """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simkl verbinden – German Scraper X</title>
<style>
body{font:16px system-ui,sans-serif;max-width:620px;margin:3rem auto;padding:0 1rem;line-height:1.5}
button{font:inherit;padding:.7rem 1rem;cursor:pointer}code{font-size:1.3rem;font-weight:700}
#status{margin-top:1.2rem;padding:1rem;background:#f2f2f2;border-radius:8px}
</style>
</head>
<body>
<h1>Simkl verbinden</h1>
<p>Verbinde dein Simkl-Konto einmalig. German Scraper X speichert die Tokens verschlüsselt auf dem Server.</p>
<button id="start">Mit Simkl verbinden</button>
<div id="status" role="status">Noch nicht verbunden.</div>
<script>
const statusBox=document.getElementById("status");
const button=document.getElementById("start");
let pollTimer=null;
function show(text){statusBox.textContent=text;}
async function poll(){
  try{
    const response=await fetch("auth/status",{cache:"no-store"});
    const data=await response.json();
    if(data.status==="connected"){
      show("Simkl ist verbunden. Scrobbling ist aktiviert.");
      button.disabled=false;
      return;
    }
    if(data.status==="connected_readonly"){
      show("Simkl wurde verbunden, aber ohne Schreibberechtigung. Bitte die App mit media:read und media:write autorisieren.");
      button.disabled=false;
      return;
    }
    if(data.status==="pending"){
      show("Warte auf Freigabe bei Simkl …");
      pollTimer=setTimeout(poll,Math.max(5,data.retry_after||5)*1000);
      return;
    }
    if(data.status==="expired"){
      show("Der Code ist abgelaufen. Bitte starte die Verbindung erneut.");
      button.disabled=false;
      return;
    }
    show("Verbindungsstatus: "+(data.message||data.status||"unbekannt"));
    button.disabled=false;
  }catch(error){
    show("Verbindungsprüfung fehlgeschlagen. Bitte erneut versuchen.");
    button.disabled=false;
  }
}
button.addEventListener("click",async()=>{
  if(pollTimer) clearTimeout(pollTimer);
  button.disabled=true;
  show("Fordere einen Simkl-Code an …");
  try{
    const response=await fetch("auth/start",{method:"POST",cache:"no-store"});
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||"Simkl konnte nicht gestartet werden");
    if(data.status==="connected"){show("Simkl ist bereits verbunden.");button.disabled=false;return;}
    if(data.status==="connected_readonly"){show("Simkl ist verbunden, aber ohne Schreibberechtigung. Bitte die App mit media:read und media:write autorisieren.");button.disabled=false;return;}
    const link=document.createElement("a");
    link.href=data.verification_uri_complete;
    link.target="_blank";
    link.rel="noopener noreferrer";
    link.textContent="Simkl-Freigabe öffnen";
    statusBox.replaceChildren();
    statusBox.append("Öffne ",link," und bestätige die Verbindung. Code: ");
    const code=document.createElement("code");
    code.textContent=data.user_code;
    statusBox.append(code);
    pollTimer=setTimeout(poll,Math.max(5,data.interval||5)*1000);
  }catch(error){
    show(error.message||"Simkl-Verbindung konnte nicht gestartet werden.");
    button.disabled=false;
  }
});
</script>
</body>
</html>"""
    )


@app.post("/simkl/auth/start")
def simkl_auth_start(request: Request):
    _require_tokenized_addon(request)
    try:
        status = get_simkl_status()
        if status.get("connected"):
            if "media:write" not in str(status.get("scope") or ""):
                return {"status": "connected_readonly"}
            return {"status": "connected"}
        return start_device_authorization()
    except SimklError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/simkl/auth/status")
def simkl_auth_status(request: Request):
    _require_tokenized_addon(request)
    try:
        status = get_simkl_status()
        if status.get("connected"):
            if "media:write" not in str(status.get("scope") or ""):
                return {"status": "connected_readonly"}
            return {"status": "connected"}
        return poll_device_authorization()
    except SimklError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/simkl/status")
def simkl_status(request: Request):
    _require_tokenized_addon(request)
    return get_simkl_status()


@app.get("/mdblist/status")
def mdblist_status(request: Request):
    """Check the MDBList API key and show current quota headers."""
    _require_tokenized_addon(request)
    return get_mdblist_status()


@app.get("/mdblist/playback")
def mdblist_playback(request: Request):
    """Return paused MDBList sessions for cross-device Continue Watching."""
    _require_tokenized_addon(request)
    return get_mdblist_playback()


@app.get("/")
async def root():
    return RedirectResponse(url="/manifest.json")

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
