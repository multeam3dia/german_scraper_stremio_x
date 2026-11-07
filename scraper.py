import re
import html
import os
import time
import niquests
from threading import RLock
from typing import List, Dict, Optional, Any, Set
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlparse
from aniworld.search import query as query_aniworld, query_s_to, query_filmpalast, query_megakino, query_kinox
from aniworld.models.aniworld_to.series import AniworldSeries
from aniworld.models.s_to.episode import SerienstreamEpisode
from aniworld.models.filmpalast_to.episode import FilmPalastEpisode
from aniworld.models.megakino.series import MegaKinoEpisode
from aniworld.models.kinox.series import KinoxEpisode
from aniworld.models.s_to.auth import ensure_serienstream_login
from aniworld.config import logger, Audio, Subtitles, DEFAULT_USER_AGENT, PROVIDER_HEADERS_W

CINEMETA_URL = "https://v3-cinemeta.strem.io/meta/{type}/{id}.json"
TMDB_PROXY = "https://db.wingsdatabase.com/3/find/{id}?external_source=imdb_id&language=de-DE"

# Kinox currently has a broken search/base-URL configuration. Keep it off unless explicitly enabled.
KINOX_ENABLED = os.getenv("KINOX_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

METADATA_CACHE = {}
GERMAN_TITLE_CACHE = {}

# Shared, bounded pools: slow CAPTCHA work must not hold the HTTP request open.
_SCRAPE_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="provider")
_S_TO_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="serienstream")
_S_TO_LOCK = RLock()
_S_TO_INFLIGHT = set()
_S_TO_RESULT_CACHE = {}
_S_TO_CACHE_TTL = 45


def _fetch_s_to_cached(key, title, season, episode_num, binge_group):
    """Run a Serienstream lookup and retain its result if it outlives the HTTP request."""
    streams = fetch_series_site_streams(
        "Serienstream", title, season, episode_num, binge_group
    )
    with _S_TO_LOCK:
        _S_TO_RESULT_CACHE[key] = (time.monotonic(), streams)
    return streams


def _clear_s_to_inflight(key, future):
    # A callback also runs when a queued future is cancelled before it starts.
    with _S_TO_LOCK:
        _S_TO_INFLIGHT.discard(key)




def _collect_scrape_results(futures, timeout: int = 12) -> List[Dict]:
    """Collect completed provider results without waiting for slow background tasks."""
    if not futures:
        return []

    done, pending = wait(futures, timeout=timeout)
    streams = []
    for future in done:
        try:
            streams.extend(future.result())
        except Exception as exc:
            logger.warning("Provider task failed: %s", exc)

    if pending:
        logger.warning(
            "Scraping timed out: %d provider task(s) still running; returning available streams",
            len(pending),
        )
        for future in pending:
            future.cancel()

    return streams



def get_metadata(media_type: str, imdb_id: str) -> Optional[Dict]:
    """Fetch metadata from Cinemeta with caching."""
    cache_key = f"{media_type}_{imdb_id}"
    if cache_key in METADATA_CACHE:
        return METADATA_CACHE[cache_key]

    url = CINEMETA_URL.format(type=media_type, id=imdb_id)
    try:
        response = niquests.get(url, timeout=10)
        response.raise_for_status()
        meta = response.json().get("meta")
        if meta:
            METADATA_CACHE[cache_key] = meta
        return meta
    except Exception as e:
        logger.error(f"Failed to fetch metadata for {imdb_id}: {e}")
        return None


def is_animation(meta: Dict) -> bool:
    """Return whether metadata identifies the title as animation.

    AniWorld is queried only for animation titles. Country/original language is
    deliberately not used as a requirement, so Korean manhwa adaptations,
    Chinese donghua and other non-Japanese animation remain eligible.
    """
    genres = meta.get("genres") or []
    if not isinstance(genres, (list, tuple, set)):
        return False

    normalized_genres = set()
    for genre in genres:
        if isinstance(genre, dict):
            genre = genre.get("name") or genre.get("title") or ""
        if genre:
            normalized_genres.add(str(genre).strip().casefold())

    return bool(normalized_genres.intersection({"animation", "anime", "donghua"}))

def get_german_title(media_type: str, imdb_id: str) -> Optional[str]:
    """Fetch German title from TMDB via proxy."""
    if imdb_id in GERMAN_TITLE_CACHE:
        return GERMAN_TITLE_CACHE[imdb_id]

    url = TMDB_PROXY.format(id=imdb_id)
    try:
        response = niquests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = data.get("movie_results") if media_type == "movie" else data.get("tv_results")
        if results and len(results) > 0:
            german_title = results[0].get("title") or results[0].get("name")
            if german_title:
                GERMAN_TITLE_CACHE[imdb_id] = german_title
                return german_title
    except Exception as e:
        logger.debug(f"Failed to fetch German title for {imdb_id}: {e}")
    return None

def clean_for_match(t: str) -> str:
    """Normalize titles, including HTML-formatted provider search results."""
    if not t: return ""
    # AniWorld wraps matched search terms in tags such as <em>Naruto</em>.
    # Decode entities and remove markup before comparing titles.
    t = html.unescape(str(t))
    t = re.sub(r"<[^>]*>", " ", t)
    t = re.sub(r'[\(\[].*?[\)\]]', '', t)
    return re.sub(r'\W+', '', t.lower())


def _is_naruto_diagnostic(title: Optional[str]) -> bool:
    """Enable verbose AniWorld diagnostics only for Naruto-related lookups."""
    return "naruto" in clean_for_match(title or "")

def find_best_matches(
    results: List[Dict],
    title: str,
    year: Optional[int] = None,
    season: Optional[int] = None,
    allow_season_suffix: bool = True,
    allow_possessive_prefix: bool = False,
    allow_subtitle_suffix: bool = False,
) -> List[str]:
    """Match whole normalized titles; never use substring matching (Monk != Bad Monkey)."""
    if not results:
        return []

    title_clean = clean_for_match(title)
    if not title_clean:
        return []

    candidates = []

    def result_title(result: Dict) -> str:
        return str(result.get("title") or result.get("name") or result.get("series") or "")

    def is_match(res_title: str) -> bool:
        res_clean = clean_for_match(res_title)
        if allow_season_suffix and season:
            # Permit only the requested season suffix, never a different season.
            for suffix in (f"staffel{season}", f"season{season}"):
                if res_clean.endswith(suffix):
                    res_clean = res_clean[:-len(suffix)]
                    break
        if res_clean == title_clean:
            return True

        # SerienStream may append a descriptive subtitle to the exact series
        # title (e.g. "Slow Horses - Ein Fall für Jackson Lamb"). Only accept
        # an explicit separator after the complete requested title.
        if allow_subtitle_suffix:
            decoded_title = html.unescape(str(res_title or ""))
            # Search APIs may return highlighted titles such as
            # "<em>Slow Horses</em> - Ein Fall für Jackson Lamb".
            decoded_title = re.sub(r"<[^>]*>", " ", decoded_title)
            decoded_title = re.sub(r"\s+", " ", decoded_title).strip()
            subtitle_match = re.match(
                rf"^{re.escape(str(title).strip())}\s+[-–—:]\s+.+$",
                decoded_title,
                re.IGNORECASE,
            )
            if subtitle_match:
                return True

        # SerienStream sometimes prefixes a show with its author's possessive
        # name (e.g. "Carl Hiaasen’s Bad Monkey"). Accept only that explicit
        # possessive-title form, and only when the remaining title is exact.
        if allow_possessive_prefix:
            decoded_title = html.unescape(str(res_title or "")).strip()
            possessive_match = re.match(r"^.+?['’]s\s+(.+)$", decoded_title, re.IGNORECASE)
            if possessive_match:
                suffix_clean = clean_for_match(possessive_match.group(1))
                if allow_season_suffix and season:
                    for suffix in (f"staffel{season}", f"season{season}"):
                        if suffix_clean.endswith(suffix):
                            suffix_clean = suffix_clean[:-len(suffix)]
                            break
                return suffix_clean == title_clean
        return False

    for res in results:
        if not isinstance(res, dict):
            continue
        res_url = res.get("url") or res.get("link")
        if not res_url or res_url in candidates:
            continue
        if is_match(result_title(res)):
            candidates.append(res_url)

    return candidates

def extract_streams(episode_obj: Any, provider_site_name: str, meta_title: str, binge_group: Optional[str] = None) -> List[Dict]:
    """Generic helper to extract streams from an episode model object."""
    streams = []
    try:
        provider_data = episode_obj.provider_data
        if not provider_data: return []

        data_items = provider_data._data.items() if hasattr(provider_data, "_data") else provider_data.items()

        for (audio, subs), providers in data_items:
            if audio.value != "German": continue

            for name, redirect_url in providers.items():
                try:
                    # Filter out problematic mirrors
                    if provider_site_name == "MegaKino" and name.lower() == "megakino":
                        continue

                    episode_obj.selected_provider = name
                    episode_obj.selected_language = audio.value
                    direct_url = episode_obj.stream_url
                    if not direct_url: continue

                    # PRECISE HEADERS: Align UA with crawler to fix "click twice" issue
                    headers = PROVIDER_HEADERS_W.get(name, {})
                    ua = headers.get("User-Agent", DEFAULT_USER_AGENT)

                    # DYNAMIC REFERER: Use the exact embed URL for CDN trust
                    embed_url = getattr(episode_obj, "provider_url", direct_url)
                    parsed_embed = urlparse(embed_url)
                    origin = f"{parsed_embed.scheme}://{parsed_embed.netloc}"

                    # Vidmoly needs the same playback headers on SerienStream
                    # that are already applied in the AniWorld extraction path.
                    playback_headers = {}
                    if name == "Vidmoly":
                        from aniworld.extractors.provider.vidmoly import get_playback_headers
                        playback_headers = get_playback_headers(embed_url)
                    elif name == "Filemoon":
                        # Filemoon direct URLs are extracted through the shared
                        # Byse/legacy extractor; retain its provider UA and use
                        # the actual embed origin as playback Referer/Origin.
                        playback_headers = {
                            "User-Agent": ua,
                            "Referer": embed_url,
                            "Origin": origin,
                            "Accept": "*/*",
                        }

                    # SMART EXTENSION: Fix player recognition for HLS vs MP4
                    is_hls = ".m3u8" in direct_url.lower() or "urlset" in direct_url.lower()
                    if not any(ext in direct_url.lower() for ext in [".mp4", ".m3u8", ".ts"]):
                        direct_url += "#.m3u8" if is_hls else "#.mp4"

                    request_headers = {
                        "User-Agent": ua,
                        "Referer": embed_url,
                        "Origin": origin,
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "cross-site",
                        "Accept": "*/*",
                    }
                    request_headers.update(playback_headers)
                    proxy_headers = {"request": request_headers}

                    stream_entry = {
                        "name": f"[{provider_site_name}] {name}",
                        "title": f"{meta_title} | {audio.value} | {name}",
                        "url": direct_url,
                        "behaviorHints": {
                            "notWebReady": True,
                            "proxyHeaders": proxy_headers
                        }
                    }

                    if binge_group:
                        stream_entry["behaviorHints"]["bingeGroup"] = binge_group

                    streams.append(stream_entry)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error extracting streams from {provider_site_name}: {e}")
    return streams


def _aniworld_candidates(title: str) -> List[AniworldSeries]:
    """Search AniWorld and construct a small, title-ranked candidate list."""
    if not title:
        return []
    diagnostic = _is_naruto_diagnostic(title)
    try:
        results = query_aniworld(title) or []
        if isinstance(results, dict):
            results = [results]
        wanted = clean_for_match(title)
        candidates = []
        seen = set()
        if diagnostic:
            result_summary = [
                {
                    "title": item.get("title") or item.get("name") or "",
                    "link": item.get("link") or item.get("url") or "",
                }
                for item in results if isinstance(item, dict)
            ]
            logger.debug(
                "[AniWorld Naruto] search query=%r normalized=%r results=%d: %s",
                title, wanted, len(results), result_summary[:20],
            )
        for index, item in enumerate(results):
            if not isinstance(item, dict):
                if diagnostic:
                    logger.debug("[AniWorld Naruto] result #%d ignored: not a dict (%r)", index + 1, item)
                continue
            name = item.get("title") or item.get("name") or ""
            link = item.get("link") or item.get("url")
            if not link:
                if diagnostic:
                    logger.debug("[AniWorld Naruto] result #%d %r rejected: missing link", index + 1, name)
                continue
            url = link if link.startswith("http") else f"https://aniworld.to{link if link.startswith('/') else '/'+link}"
            valid_url = AniworldSeries.is_valid_aniworld_series_url(url)
            if not valid_url or url in seen:
                if diagnostic:
                    logger.debug(
                        "[AniWorld Naruto] result #%d %r rejected: valid_series_url=%s duplicate=%s url=%r",
                        index + 1, name, valid_url, url in seen, url,
                    )
                continue
            seen.add(url)
            found = clean_for_match(name)
            exact_match = bool(wanted and found == wanted)
            if diagnostic:
                logger.debug(
                    "[AniWorld Naruto] result #%d title=%r normalized=%r exact_match=%s url=%r",
                    index + 1, name, found, exact_match, url,
                )
            # Never accept fuzzy/unrelated search results: AniWorld's search can
            # return completely different shows (e.g. One Piece for "Monk").
            # Bracketed year/extra text is already removed by clean_for_match.
            if not exact_match:
                continue
            candidates.append((0, AniworldSeries(url)))
        selected = [series for _, series in candidates[:5]]
        if diagnostic:
            logger.debug(
                "[AniWorld Naruto] accepted %d candidate(s) for %r: %s",
                len(selected), title, [series.url for series in selected],
            )
        return selected
    except Exception as exc:
        if diagnostic:
            logger.debug("[AniWorld Naruto] search/candidate processing failed for %r: %s", title, exc)
        logger.debug(f"AniWorld search failed for '{title}': {exc}")
        return []


def _extract_aniworld_streams(episode: Any, display_title: str, binge_group: Optional[str] = None) -> List[Dict]:
    """Resolve German Dub and German Sub variants from an AniWorld episode."""
    streams = []
    try:
        data = episode.provider_data
        provider_map = data._data if hasattr(data, "_data") else data
        for (audio, subtitles), providers in provider_map.items():
            if audio == Audio.GERMAN and subtitles == Subtitles.NONE:
                language = "German Dub"
            elif audio == Audio.JAPANESE and subtitles == Subtitles.GERMAN:
                language = "German Sub"
            else:
                continue

            for provider_name in providers:
                try:
                    episode.selected_language = language
                    episode.selected_provider = provider_name
                    direct_url = episode.stream_url
                    if not direct_url:
                        continue
                    embed_url = episode.provider_url
                    parsed = urlparse(embed_url)
                    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://aniworld.to"
                    headers = PROVIDER_HEADERS_W.get(provider_name, {})
                    playback_headers = {}
                    if provider_name == "Doodstream":
                        from aniworld.extractors.provider.doodstream import get_playback_headers
                        playback_headers = get_playback_headers(embed_url)
                    elif provider_name == "Vidmoly":
                        from aniworld.extractors.provider.vidmoly import get_playback_headers
                        playback_headers = get_playback_headers(embed_url)

                    is_hls = ".m3u8" in direct_url.lower() or "urlset" in direct_url.lower()
                    if not any(ext in direct_url.lower() for ext in (".mp4", ".m3u8", ".ts")):
                        direct_url += "#.m3u8" if is_hls else "#.mp4"

                    request_headers = {
                        "User-Agent": headers.get("User-Agent", DEFAULT_USER_AGENT),
                        "Referer": embed_url,
                        "Origin": origin,
                        "Accept": "*/*",
                    }
                    # Provider extractors may require a different playback UA,
                    # final embed Referer, or cookies than the AniWorld redirect.
                    request_headers.update(playback_headers)

                    entry = {
                        "name": f"[AniWorld] {provider_name}",
                        "title": f"{display_title} | {language} | {provider_name}",
                        "url": direct_url,
                        "behaviorHints": {
                            "notWebReady": True,
                            "proxyHeaders": {"request": request_headers},
                        },
                    }
                    if binge_group:
                        entry["behaviorHints"]["bingeGroup"] = binge_group
                    streams.append(entry)
                except Exception as exc:
                    # Provider exceptions may contain temporary tokens in their URLs.
                    logger.debug(
                        "AniWorld provider %s failed (%s)",
                        provider_name,
                        type(exc).__name__,
                    )
    except Exception as exc:
        logger.debug(f"AniWorld episode stream extraction failed: {exc}")
    return streams


def _get_cinemeta_episode(meta: Dict, season_number: int, episode_number: int) -> Optional[Dict]:
    """Return the requested episode record from Cinemeta's series metadata."""
    videos = meta.get("videos") or []
    for video in videos:
        if not isinstance(video, dict):
            continue
        try:
            video_season = int(video.get("season"))
            video_episode = int(video.get("episode"))
        except (TypeError, ValueError):
            continue
        if video_season == season_number and video_episode == episode_number:
            return video
    return None


def _find_aniworld_episode_by_title(
    series: AniworldSeries,
    target_episode_title: str,
    requested_season: int,
    requested_episode: int,
):
    """Find an AniWorld episode by its title, independent of season numbering."""
    wanted = clean_for_match(target_episode_title)
    # Generic placeholders are not reliable enough to identify an episode.
    if (
        not wanted
        or wanted in {"episode", "folge", "episodetba", "tba", "untitled"}
        or re.fullmatch(r"(episode|folge)\d+", wanted)
    ):
        return None

    seasons = series.seasons
    # Fast path: if the title at the requested numeric position also matches,
    # the provider numbering is confirmed for this episode. Avoid loading all
    # other season pages for the common case.
    for season in seasons:
        if season.are_movies or season.season_number != requested_season:
            continue
        for episode in season.episodes:
            if episode.episode_number != requested_episode:
                continue
            episode_titles = {
                clean_for_match(getattr(episode, "title_en", "") or ""),
                clean_for_match(getattr(episode, "title_de", "") or ""),
            }
            if wanted in episode_titles:
                return season, episode
        break

    # Otherwise scan every season, because AniWorld's numbering may differ
    # from Cinemeta's numbering or may split/merge seasons differently.
    matches = []
    for season in seasons:
        if season.are_movies:
            continue
        for episode in season.episodes:
            episode_titles = {
                clean_for_match(getattr(episode, "title_en", "") or ""),
                clean_for_match(getattr(episode, "title_de", "") or ""),
            }
            episode_titles.discard("")
            if wanted in episode_titles:
                matches.append((season, episode))

    # Repeated episode titles can occur in anthologies or across seasons. Only
    # use the requested numeric location as a tie-breaker when it is itself a
    # member of the title-matched set; otherwise fail closed.
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        same_position = [
            pair for pair in matches
            if pair[0].season_number == requested_season
            and pair[1].episode_number == requested_episode
        ]
        if len(same_position) == 1:
            return same_position[0]
    return None


def fetch_aniworld_series_streams(
    title: str,
    season_number: int,
    episode_number: int,
    binge_group: str,
    expected_imdb_id: str,
    target_episode_title: str,
) -> List[Dict]:
    """Resolve an AniWorld episode by series IMDb ID and Cinemeta episode title."""
    diagnostic = _is_naruto_diagnostic(title)
    if diagnostic:
        logger.debug(
            "[AniWorld Naruto] resolve request title=%r imdb=%s S%02dE%02d episode_title=%r",
            title, expected_imdb_id, season_number, episode_number, target_episode_title,
        )

    candidates = _aniworld_candidates(title)
    if diagnostic and not candidates:
        logger.debug("[AniWorld Naruto] no exact-title candidates found for %r", title)

    for series in candidates:
        try:
            actual_title = getattr(series, "title", "") or ""
            actual_imdb_id = getattr(series, "imdb", None)
            if actual_imdb_id and actual_imdb_id != expected_imdb_id:
                logger.warning(
                    "Skipping AniWorld candidate %r: IMDb ID %s does not match requested %s",
                    actual_title, actual_imdb_id, expected_imdb_id,
                )
                continue

            # When AniWorld exposes an IMDb ID, it is the strongest identity
            # check. If it does not, retain the previous exact-title guard.
            if not actual_imdb_id and clean_for_match(actual_title) != clean_for_match(title):
                logger.debug(
                    "Skipping AniWorld candidate %r for requested title %r: no matching IMDb ID",
                    actual_title, title,
                )
                continue

            if target_episode_title:
                matched = _find_aniworld_episode_by_title(
                    series, target_episode_title, season_number, episode_number
                )
                if not matched:
                    logger.info(
                        "No unambiguous AniWorld episode title match for %r (%s), requested %s S%02dE%02d",
                        actual_title, target_episode_title, expected_imdb_id, season_number, episode_number,
                    )
                    continue
            else:
                # Cinemeta sometimes omits the videos array entirely. In that
                # case retain the original numeric mapping, but only after the
                # series identity has passed the IMDb/exact-title check above.
                matched = None
                for candidate_season in series.seasons:
                    if candidate_season.are_movies or candidate_season.season_number != season_number:
                        continue
                    for candidate_episode in candidate_season.episodes:
                        if candidate_episode.episode_number == episode_number:
                            matched = (candidate_season, candidate_episode)
                            break
                    break
                if not matched:
                    logger.info(
                        "No numeric AniWorld fallback for %r, requested S%02dE%02d",
                        actual_title, season_number, episode_number,
                    )
                    continue
                logger.warning(
                    "Cinemeta has no episode title for %r S%02dE%02d; using numeric AniWorld fallback after series identity check",
                    title, season_number, episode_number,
                )

            matched_season, episode = matched
            if diagnostic:
                logger.debug(
                    "[AniWorld Naruto] mapped episode %r to AniWorld S%02dE%02d (%r / %r)",
                    target_episode_title or f"S{season_number:02d}E{episode_number:02d} (numeric fallback)",
                    matched_season.season_number,
                    episode.episode_number,
                    episode.title_de,
                    episode.title_en,
                )

            streams = _extract_aniworld_streams(
                episode,
                f"{actual_title} S{season_number:02d}E{episode_number:02d}",
                binge_group,
            )
            if diagnostic:
                logger.debug(
                    "[AniWorld Naruto] stream extraction returned %d stream(s) for requested S%02dE%02d",
                    len(streams), season_number, episode_number,
                )
            if streams:
                return streams
        except Exception as exc:
            logger.debug("AniWorld series processing failed for %s: %s", getattr(series, "url", title), exc)
    return []

def fetch_aniworld_movie_streams(title: str) -> List[Dict]:
    """Search AniWorld anime film collections and match the requested film title."""
    wanted = clean_for_match(title)
    if not wanted:
        return []
    for series in _aniworld_candidates(title):
        try:
            for season in series.seasons:
                if not season.are_movies:
                    continue
                for episode in season.episodes:
                    names = [episode.title_de or "", episode.title_en or ""]
                    names = [clean_for_match(name) for name in names if name]
                    if not any(name and (name == wanted or wanted in name or name in wanted) for name in names):
                        continue
                    streams = _extract_aniworld_streams(episode, episode.title_de or episode.title_en or series.title)
                    if streams:
                        return streams
        except Exception as exc:
            logger.debug(f"AniWorld movie collection processing failed for {series.url}: {exc}")
    return []


def fetch_movie_site_streams(site_name: str, query_fn: Any, episode_cls: Any, title: str, year: Optional[int]) -> List[Dict]:
    """Search and extract streams for a movie from a single site."""
    try:
        results = query_fn(title)
        if not results and ":" in title:
            results = query_fn(title.split(":")[0])
        if not results: return []

        candidate_urls = find_best_matches(results, title, year)
        site_streams = []
        for url in candidate_urls[:2]:
            try:
                episode = episode_cls(url)
                streams = extract_streams(episode, site_name, title)
                if streams:
                    site_streams.extend(streams)
                    break
            except Exception:
                pass
        return site_streams
    except Exception as e:
        logger.error(f"Failed to query {site_name}: {e}")
        return []

def get_movie_streams(imdb_id: str) -> List[Dict]:
    """Scrape streams for a movie using parallel searches for English and German titles."""
    meta = get_metadata("movie", imdb_id)
    if not meta: return []

    en_title = meta.get("name")
    year = int(meta.get("releaseInfo", 0)) if meta.get("releaseInfo") else None
    de_title = get_german_title("movie", imdb_id)

    titles_to_search = {en_title}
    if de_title: titles_to_search.add(de_title)

    all_streams = []
    providers = [
        ("MegaKino", query_megakino, MegaKinoEpisode),
        ("Filmpalast", query_filmpalast, FilmPalastEpisode),
    ]
    if KINOX_ENABLED:
        providers.append(("Kinox", query_kinox, KinoxEpisode))
    aniworld_enabled = is_animation(meta)
    logger.debug(
        "AniWorld lookup for movie %r: enabled=%s",
        en_title, aniworld_enabled,
    )

    futures = []
    for title in titles_to_search:
        if aniworld_enabled:
            futures.append(_SCRAPE_EXECUTOR.submit(fetch_aniworld_movie_streams, title))
        for site, q_fn, cls in providers:
            futures.append(_SCRAPE_EXECUTOR.submit(fetch_movie_site_streams, site, q_fn, cls, title, year))

    all_streams.extend(_collect_scrape_results(futures, timeout=12))
    return all_streams[:6]

def fetch_series_site_streams(site_name: str, title: str, season: int, episode_num: int, binge_group: str) -> List[Dict]:
    """Search and extract streams for a series from a single site."""
    try:
        if site_name == "Serienstream":
            # If credentials are configured, refresh the account session before
            # searching. Login failure is non-fatal; the existing CAPTCHA flow remains.
            ensure_serienstream_login()
            results = query_s_to(title)
            if not results:
                logger.info(f"Serienstream: no search results for '{title}'")
                return []
            links = find_best_matches(
                results,
                title,
                season=season,
                allow_season_suffix=True,
                allow_possessive_prefix=True,
                allow_subtitle_suffix=True,
            )
            if not links:
                returned_titles = [
                    str(item.get("title") or item.get("name") or item.get("series") or "")
                    for item in results if isinstance(item, dict)
                ]
                logger.info(
                    f"Serienstream: no exact title match for '{title}'. "
                    f"Search returned: {returned_titles[:10]}"
                )
                return []
            # Try a few exact-title candidates in case one page or episode URL is stale.
            for link in links[:3]:
                series_url = f"https://serienstream.to{link}" if not link.startswith("http") else link
                ep_url = f"{series_url.rstrip('/')}/staffel-{season}/episode-{episode_num}"
                try:
                    episode = SerienstreamEpisode(url=ep_url)
                    streams = extract_streams(episode, site_name, title, binge_group)
                    if streams:
                        return streams
                    logger.info(f"Serienstream: no German streams at {ep_url}")
                except Exception as exc:
                    logger.warning(f"Serienstream failed for {ep_url}: {exc}")

        elif site_name == "MegaKino":
            # The season-specific query can return unrelated results. If none
            # match the requested title, retry with the plain title.
            results = query_megakino(f"{title} Staffel {season}") or []
            urls = find_best_matches(results, title, season=season)
            if not urls:
                results = query_megakino(title) or []
                urls = find_best_matches(results, title, season=season)
            if not urls:
                returned_titles = [
                    str(item.get("title") or item.get("name") or "")
                    for item in results if isinstance(item, dict)
                ]
                logger.info(
                    f"MegaKino: no exact title match for '{title}'. "
                    f"Search returned: {returned_titles[:10]}"
                )
                return []

            for url in urls[:2]:
                ep_url = f"{url}#mkep={episode_num}"
                try:
                    episode = MegaKinoEpisode(ep_url)
                    streams = extract_streams(episode, site_name, title, binge_group)
                    if streams:
                        return streams
                    logger.info(f"MegaKino: no German streams at {ep_url}")
                except Exception as exc:
                    logger.warning(f"MegaKino failed for {ep_url}: {exc}")

        elif site_name == "Kinox":
            results = query_kinox(title)
            if not results: return []
            urls = find_best_matches(results, title, season=season)
            for url in urls[:1]:
                ep_url = f"{url.split('?')[0]}?s={season}&e={episode_num}"
                try:
                    episode = KinoxEpisode(ep_url)
                    return extract_streams(episode, site_name, title, binge_group)
                except Exception: pass
    except Exception as e:
        logger.error(f"Error fetching from {site_name}: {e}")
    return []

def get_series_streams(imdb_id: str, season: int, episode_num: int) -> List[Dict]:
    """Scrape streams for a series episode using parallel searches for English and German titles."""
    pure_imdb_id = imdb_id.split(":")[0]
    meta = get_metadata("series", pure_imdb_id)
    if not meta: return []

    en_title = meta.get("name")
    de_title = get_german_title("series", pure_imdb_id)
    titles_to_search = {en_title}
    if de_title: titles_to_search.add(de_title)

    all_streams = []
    binge_group = f"{pure_imdb_id}"
    aniworld_enabled = is_animation(meta)
    target_video = _get_cinemeta_episode(meta, season, episode_num) if aniworld_enabled else None
    target_episode_title = (target_video or {}).get("title") if target_video else None
    if aniworld_enabled and not target_episode_title:
        logger.info(
            "Cinemeta has no video title for %r S%02dE%02d; AniWorld will use guarded numeric fallback",
            en_title, season, episode_num,
        )
    logger.debug(
        "AniWorld lookup for series %r: enabled=%s episode_title=%r",
        en_title, aniworld_enabled, target_episode_title,
    )
    if _is_naruto_diagnostic(en_title) or _is_naruto_diagnostic(de_title):
        logger.debug(
            "[AniWorld Naruto] metadata gate: en_title=%r de_title=%r genres=%r enabled=%s requested=S%02dE%02d",
            en_title, de_title, meta.get("genres"), aniworld_enabled, season, episode_num,
        )
        logger.debug(
            "[AniWorld Naruto] provider search titles=%r",
            sorted(title for title in titles_to_search if title),
        )

    futures = []
    for title in titles_to_search:
        if aniworld_enabled:
            futures.append(_SCRAPE_EXECUTOR.submit(
                fetch_aniworld_series_streams,
                title,
                season,
                episode_num,
                binge_group,
                pure_imdb_id,
                target_episode_title,
            ))

        # Reuse a recently completed S.to result, or leave one background task
        # running. This lets a later Stremio request receive a result that needed
        # more than the 12-second response budget (for example, a CAPTCHA).
        s_to_key = (pure_imdb_id, title, season, episode_num)
        with _S_TO_LOCK:
            cached = _S_TO_RESULT_CACHE.get(s_to_key)
            if cached and time.monotonic() - cached[0] < _S_TO_CACHE_TTL:
                all_streams.extend(cached[1])
            else:
                if cached:
                    _S_TO_RESULT_CACHE.pop(s_to_key, None)

                if s_to_key in _S_TO_INFLIGHT:
                    logger.info("Serienstream lookup still running for %s S%02dE%02d",
                                title, season, episode_num)
                else:
                    _S_TO_INFLIGHT.add(s_to_key)
                    try:
                        s_to_future = _S_TO_EXECUTOR.submit(
                            _fetch_s_to_cached, s_to_key, title, season, episode_num, binge_group
                        )
                        s_to_future.add_done_callback(
                            lambda future, key=s_to_key: _clear_s_to_inflight(key, future)
                        )
                        futures.append(s_to_future)
                    except Exception as exc:
                        _S_TO_INFLIGHT.discard(s_to_key)
                        logger.warning("Could not schedule Serienstream lookup: %s", exc)

        # MegaKino is intentionally movie-only: skip its series lookup to
        # avoid adding latency and unrelated search traffic to series requests.
        if KINOX_ENABLED:
            futures.append(_SCRAPE_EXECUTOR.submit(
                fetch_series_site_streams, "Kinox", title, season, episode_num, binge_group
            ))

    all_streams.extend(_collect_scrape_results(futures, timeout=12))
    return all_streams
