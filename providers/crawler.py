"""Run a provider catalog crawl and persist verified title structures."""
from __future__ import annotations

import re
from typing import Any

from catalog_db import (
    finish_crawl_run,
    record_unresolved_title,
    record_discovery,
    record_series_structure,
    start_crawl_run,
)
from . import PROVIDERS
from .base import BaseProvider


def crawl_provider(provider: BaseProvider) -> dict[str, Any]:
    """Discover titles, resolve provider details, and store verified IMDb records.

    Provider listing failures never count as evidence that a title disappeared.
    Records without a valid IMDb ID are reported for later matching and are not
    admitted to the public catalog.
    """
    run_id = start_crawl_run(provider.provider_id, "series")
    found = 0
    unresolved = 0
    failed = 0
    errors: list[str] = []

    try:
        for reference in provider.iter_catalog():
            try:
                detail = provider.get_title(reference.url)
                if not detail.imdb_id or not re.fullmatch(r"tt\\d{7,10}", str(detail.imdb_id).strip()):
                    unresolved += 1
                    record_unresolved_title(
                        detail.provider,
                        detail.url,
                        detail.title or reference.title,
                        detail.media_type,
                        year=detail.year,
                        poster_url=detail.poster_url,
                        description=detail.description,
                        metadata={
                            **detail.metadata,
                            "provider_genres": detail.provider_genres,
                        },
                        error="Provider detail page did not expose a valid IMDb ID",
                    )
                    continue

                record_discovery(
                    detail.media_type,
                    detail.imdb_id,
                    detail.provider,
                    detail.title,
                    provider_title=detail.title,
                    provider_url=detail.url,
                    year=detail.year,
                    metadata={
                        **detail.metadata,
                        "provider_genres": detail.provider_genres,
                    },
                )
                seasons = provider.get_seasons(detail)
                record_series_structure(
                    detail.media_type,
                    detail.imdb_id,
                    detail.provider,
                    [season.to_dict() for season in seasons],
                )
                found += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 20:
                    errors.append(f"{reference.url}: {type(exc).__name__}: {exc}")

        # A partial listing adapter must never be marked as a complete crawl.
        complete = bool(getattr(provider, "catalog_scan_complete", False))
        status = "completed" if complete and not failed and not unresolved else "partial"
        finish_crawl_run(
            run_id,
            status=status,
            titles_found=found,
            error=" | ".join(errors) if errors else (
                f"{unresolved} titles without IMDb ID; scan coverage is partial"
                if unresolved or not complete else None
            ),
        )
    except Exception as exc:
        finish_crawl_run(
            run_id,
            status="failed",
            titles_found=found,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    return {
        "provider": provider.provider_id,
        "run_id": run_id,
        "status": status,
        "titles_recorded": found,
        "unresolved_imdb": unresolved,
        "title_errors": failed,
        "errors": errors,
    }


def crawl_named_provider(provider_id: str) -> dict[str, Any]:
    provider_class = PROVIDERS.get(provider_id.strip().lower())
    if provider_class is None:
        raise ValueError(f"Unknown provider: {provider_id}")
    return crawl_provider(provider_class())
