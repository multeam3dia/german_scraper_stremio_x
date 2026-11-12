"""Low-request catalog index readers for AniWorld and SerienStream."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from aniworld.config import GLOBAL_SESSION, logger


class _AniWorldIndexParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            parsed = urlparse(urljoin("https://aniworld.to/", href))
            if (
                parsed.hostname in {"aniworld.to", "www.aniworld.to"}
                and re.fullmatch(r"/anime/stream/[A-Za-z0-9-]+/?", parsed.path)
            ):
                self.current = {
                    "url": f"https://aniworld.to{parsed.path}",
                    "title_attr": attrs.get("title") or attrs.get("aria-label") or "",
                    "alt": "",
                    "poster_url": "",
                    "heading": [],
                    "in_heading": False,
                    "text": [],
                }
        elif self.current is not None and tag == "h3":
            self.current["in_heading"] = True
        elif self.current is not None and tag == "img":
            self.current["alt"] = self.current["alt"] or (attrs.get("alt") or "").strip()
            poster = (
                attrs.get("data-src")
                or attrs.get("data-original")
                or attrs.get("data-lazy-src")
                or attrs.get("src")
                or ""
            ).strip()
            if poster and not self.current["poster_url"]:
                self.current["poster_url"] = urljoin("https://aniworld.to/", poster)

    def handle_data(self, data):
        if self.current is not None:
            value = data.strip()
            if value:
                if self.current["in_heading"]:
                    self.current["heading"].append(value)
                else:
                    self.current["text"].append(value)

    def handle_endtag(self, tag):
        if tag == "h3" and self.current is not None:
            self.current["in_heading"] = False
            return
        if tag != "a" or self.current is None:
            return
        item = self.current
        self.current = None
        title = (
            " ".join(item["heading"])
            or item["alt"]
            or item["title_attr"]
            or " ".join(item["text"])
        )
        title = html.unescape(" ".join(str(title).split())).strip()
        if title:
            self.items.append({
                "title": title,
                "url": item["url"],
                "poster_url": item["poster_url"],
            })


def fetch_aniworld_index(session=None):
    """Read the single A-Z index; reject suspiciously incomplete responses."""
    session = session or GLOBAL_SESSION
    response = session.get("https://aniworld.to/animes-alphabet", timeout=25)
    response.raise_for_status()
    page = response.text or ""
    if not re.search(r"(?:Anime-Liste|Alle\s+Animes|Sortieren\s+nach)", page, re.I):
        raise RuntimeError("AniWorld A-Z index marker was not found")

    parser = _AniWorldIndexParser()
    parser.feed(page)
    parser.close()

    results = []
    seen = set()
    for item in parser.items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        results.append(item)

    # The current public A-Z index contains well over 1,000 entries. A much
    # smaller response is not safe evidence for a complete availability scan.
    if len(results) < 1000:
        raise RuntimeError(
            f"AniWorld A-Z index looks incomplete ({len(results)} series links; expected at least 1,000)"
        )
    logger.info("AniWorld A-Z index discovered %s unique entries", len(results))
    return results


def _parse_serienstream_index(page: str, base_url: str):
    class _IndexParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.cards = []
            self.current = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a":
                href = (attrs.get("href") or "").strip()
                parsed = urlparse(urljoin(base_url, href))
                match = re.match(
                    r"^/serie/([^/?#]+)(?:/staffel-\d+)?/?$", parsed.path
                )
                if match:
                    self.current = {
                        "slug": match.group(1),
                        "title": attrs.get("title") or "",
                        "text": [],
                        "poster_url": "",
                    }
            elif self.current is not None and tag == "img":
                alt = (attrs.get("alt") or "").strip()
                if alt and not self.current["title"]:
                    self.current["title"] = alt
                poster = (
                    attrs.get("data-src")
                    or attrs.get("data-original")
                    or attrs.get("src")
                    or ""
                ).strip()
                if poster and not self.current["poster_url"]:
                    self.current["poster_url"] = urljoin(base_url, poster)

        def handle_data(self, data):
            if self.current is not None:
                self.current["text"].append(data)

        def handle_endtag(self, tag):
            if tag != "a" or self.current is None:
                return
            item = self.current
            self.current = None
            title = item["title"] or " ".join(" ".join(item["text"]).split())
            if title:
                self.cards.append({
                    "title": html.unescape(title).strip(),
                    "url": urljoin(base_url, "/serie/" + item["slug"]),
                    "poster_url": item["poster_url"],
                })

    parser = _IndexParser()
    parser.feed(page)
    parser.close()
    results, seen = [], set()
    for item in parser.cards:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        results.append(item)
    return results


def fetch_serienstream_index(session=None):
    """Read the single all-series index through the guarded crawl session."""
    from aniworld.models.s_to.http import sto_get, sto_host

    response = sto_get("https://serienstream.to/serien", session=session)
    response.raise_for_status()
    page = response.text or ""
    if not re.search(r"Alle\s+Einträge", page, re.I):
        raise RuntimeError("SerienStream all-series index marker was not found")
    base_url = f"https://{sto_host()}/serien"
    results = _parse_serienstream_index(page, base_url)
    if len(results) < 50:
        raise RuntimeError(
            f"SerienStream all-series index looks incomplete ({len(results)} entries)"
        )
    logger.info(
        "SerienStream alphabetical index discovered %s unique entries", len(results)
    )
    return results
