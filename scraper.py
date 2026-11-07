import re
import niquests
from typing import List, Dict, Optional, Any, Set
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from urllib.parse import urlparse
from aniworld.search import query as query_aniworld, query_s_to, query_filmpalast, query_megakino, query_kinox
from aniworld.models.aniworld_to.series import AniworldSeries
from aniworld.models.s_to.episode import SerienstreamEpisode
from aniworld.models.filmpalast_to.episode import FilmPalastEpisode
from aniworld.models.megakino.series import MegaKinoEpisode
from aniworld.models.kinox.series import KinoxEpisode
from aniworld.config import logger, Audio, Subtitles, DEFAULT_USER_AGENT, PROVIDER_HEADERS_W

CINEMETA_URL = "https://v3-cinemeta.strem.io/meta/{type}/{id}.json"
TMDB_PROXY = "https://db.wingsdatabase.com/3/find/{id}?external_source=imdb_id&language=de-DE"

METADATA_CACHE = {}
GERMAN_TITLE_CACHE = {}

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
    """Lowercase and alphanumeric only for title comparison."""
    if not t: return ""
    t = re.sub(r'[\(\[].*?[\)\]]', '', t)
    return re.sub(r'\W+', '', t.lower())

def find_best_matches(
    results: List[Dict],
    title: str,
    year: Optional[int] = None,
    season: Optional[int] = None,
    allow_season_suffix: bool = True,
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
        return res_clean == title_clean

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

                    # SMART EXTENSION: Fix player recognition for HLS vs MP4
                    is_hls = ".m3u8" in direct_url.lower() or "urlset" in direct_url.lower()
                    if not any(ext in direct_url.lower() for ext in [".mp4", ".m3u8", ".ts"]):
                        direct_url += "#.m3u8" if is_hls else "#.mp4"

                    proxy_headers = {
                        "request": {
                            "User-Agent": ua,
                            "Referer": embed_url,
                            "Origin": origin,
                            "Sec-Fetch-Mode": "cors",
                            "Sec-Fetch-Site": "cross-site",
                            "Accept": "*/*"
                        }
                    }

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
    try:
        results = query_aniworld(title) or []
        if isinstance(results, dict):
            results = [results]
        wanted = clean_for_match(title)
        candidates = []
        seen = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            name = item.get("title") or item.get("name") or ""
            link = item.get("link") or item.get("url")
            if not link:
                continue
            url = link if link.startswith("http") else f"https://aniworld.to{link if link.startswith('/') else '/'+link}"
            if not AniworldSeries.is_valid_aniworld_series_url(url) or url in seen:
                continue
            seen.add(url)
            found = clean_for_match(name)
            # Never accept fuzzy/unrelated search results: AniWorld's search can
            # return completely different shows (e.g. One Piece for "Monk").
            # Bracketed year/extra text is already removed by clean_for_match.
            if not wanted or found != wanted:
                continue
            candidates.append((0, AniworldSeries(url)))
        return [series for _, series in candidates[:5]]
    except Exception as exc:
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
                    is_hls = ".m3u8" in direct_url.lower() or "urlset" in direct_url.lower()
                    if not any(ext in direct_url.lower() for ext in (".mp4", ".m3u8", ".ts")):
                        direct_url += "#.m3u8" if is_hls else "#.mp4"
                    entry = {
                        "name": f"[AniWorld] {provider_name}",
                        "title": f"{display_title} | {language} | {provider_name}",
                        "url": direct_url,
                        "behaviorHints": {
                            "notWebReady": True,
                            "proxyHeaders": {
                                "request": {
                                    "User-Agent": headers.get("User-Agent", DEFAULT_USER_AGENT),
                                    "Referer": embed_url,
                                    "Origin": origin,
                                    "Accept": "*/*",
                                }
                            },
                        },
                    }
                    if binge_group:
                        entry["behaviorHints"]["bingeGroup"] = binge_group
                    streams.append(entry)
                except Exception as exc:
                    logger.debug(f"AniWorld provider {provider_name} failed: {exc}")
    except Exception as exc:
        logger.debug(f"AniWorld episode stream extraction failed: {exc}")
    return streams


def fetch_aniworld_series_streams(title: str, season_number: int, episode_number: int, binge_group: str) -> List[Dict]:
    """Resolve the requested season/episode from AniWorld."""
    for series in _aniworld_candidates(title):
        try:
            # Final guard: do not emit streams unless the resolved AniWorld
            # series itself matches the requested title exactly.
            if clean_for_match(getattr(series, "title", "")) != clean_for_match(title):
                logger.warning(
                    f"Skipping AniWorld candidate '{getattr(series, 'title', '')}' "
                    f"for requested title '{title}'"
                )
                continue
            for season in series.seasons:
                if season.are_movies or season.season_number != season_number:
                    continue
                for episode in season.episodes:
                    if episode.episode_number == episode_number:
                        return _extract_aniworld_streams(
                            episode, f"{series.title} S{season_number:02d}E{episode_number:02d}", binge_group
                        )
        except Exception as exc:
            logger.debug(f"AniWorld series processing failed for {series.url}: {exc}")
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
        ("Kinox", query_kinox, KinoxEpisode),
    ]
    aniworld_enabled = is_animation(meta)

    with ThreadPoolExecutor(max_workers=(len(providers) + int(aniworld_enabled)) * len(titles_to_search)) as executor:
        futures = []
        for title in titles_to_search:
            if aniworld_enabled:
                futures.append(executor.submit(fetch_aniworld_movie_streams, title))
            for site, q_fn, cls in providers:
                futures.append(executor.submit(fetch_movie_site_streams, site, q_fn, cls, title, year))

        try:
            for future in as_completed(futures, timeout=12):
                all_streams.extend(future.result())
                if len(all_streams) >= 6: break
        except TimeoutError:
            logger.warning("Scraping timed out for some providers")

    return all_streams

def fetch_series_site_streams(site_name: str, title: str, season: int, episode_num: int, binge_group: str) -> List[Dict]:
    """Search and extract streams for a series from a single site."""
    try:
        if site_name == "Serienstream":
            results = query_s_to(title)
            if not results:
                logger.info(f"Serienstream: no search results for '{title}'")
                return []
            links = find_best_matches(results, title, season=season, allow_season_suffix=True)
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
    sites = ["Serienstream", "MegaKino", "Kinox"]
    aniworld_enabled = is_animation(meta)

    with ThreadPoolExecutor(max_workers=(len(sites) + int(aniworld_enabled)) * len(titles_to_search)) as executor:
        futures = []
        for title in titles_to_search:
            if aniworld_enabled:
                futures.append(executor.submit(fetch_aniworld_series_streams, title, season, episode_num, binge_group))
            for site in sites:
                futures.append(executor.submit(fetch_series_site_streams, site, title, season, episode_num, binge_group))

        try:
            for future in as_completed(futures, timeout=12):
                all_streams.extend(future.result())
        except TimeoutError:
            logger.warning("Scraping timed out for some providers")

    return all_streams
