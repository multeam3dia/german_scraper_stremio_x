"""Run conservative, incremental provider catalog crawls."""
from __future__ import annotations

import os
import re
from typing import Any

from catalog_db import (
    finish_crawl_run,
    get_resolved_imdb_id,
    init_db,
    mark_unresolved_title_resolved,
    record_discovery,
    record_unresolved_title,
    reconcile_provider_availability,
    start_crawl_run,
)
from . import PROVIDERS
from .base import BaseProvider
from .crawl_safety import (
    CrawlBlockedError,
    CrawlGuard,
    ProviderAlreadyRunningError,
)
from .crawl_state import (
    load_provider_url_state,
    touch_provider_url,
    unresolved_retry_due,
)


_VALID_IMDB_ID = re.compile(r"tt\d{7,10}")


def _detail_limit_from_environment() -> int:
    raw_limit = os.getenv("CATALOG_CRAWL_MAX_DETAILS_PER_RUN", "200")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ValueError(
            "CATALOG_CRAWL_MAX_DETAILS_PER_RUN must be a positive integer"
        ) from exc
    if limit < 1:
        raise ValueError("CATALOG_CRAWL_MAX_DETAILS_PER_RUN must be at least 1")
    return limit


def crawl_provider(
    provider: BaseProvider,
    *,
    index_only: bool = False,
    max_details: int | None = None,
) -> dict[str, Any]:
    """Crawl a provider's complete index and resolve only new/due detail pages.

    Existing provider URLs are refreshed from the index without reopening their
    detail pages. Unresolved IMDb records are retried with a capped backoff.
    Detail-page requests are capped per provider run. Availability reconciliation
    runs only after the full index and all eligible detail work were completed.
    """
    init_db()
    detail_limit = (
        _detail_limit_from_environment() if max_details is None else max_details
    )
    if detail_limit < 1:
        raise ValueError("max_details must be at least 1")

    supported_types = tuple(getattr(provider, "supported_media_types", ()) or ())
    media_type = supported_types[0] if supported_types else "series"
    run_id = start_crawl_run(provider.provider_id, media_type)

    found = 0
    skipped_known = 0
    deferred_index_only = 0
    deferred_detail_limit = 0
    detail_pages_attempted = 0
    detail_limit_reached = False
    skipped_backoff = 0
    retried_unresolved = 0
    unresolved = 0
    failed = 0
    errors: list[str] = []
    index_exhausted = False
    blocked = False
    requests_made = 0

    try:
        with CrawlGuard(provider.provider_id) as session:
            known_urls, pending_unresolved = load_provider_url_state(
                provider.provider_id
            )

            for reference in provider.iter_catalog(session=session):
                url = str(reference.url or "").strip()
                if not url:
                    continue

                if url in known_urls:
                    touch_provider_url(provider.provider_id, url)
                    skipped_known += 1
                    continue

                unresolved_record = pending_unresolved.get(url)
                if unresolved_record:
                    touch_provider_url(provider.provider_id, url)
                    if index_only:
                        deferred_index_only += 1
                        continue
                    if not unresolved_retry_due(unresolved_record):
                        skipped_backoff += 1
                        continue
                    retried_unresolved += 1

                if index_only:
                    deferred_index_only += 1
                    continue

                # Keep walking the cheap index after reaching the detail budget:
                # known URLs can still be touched, but no more detail requests
                # are made during this provider run.
                if detail_pages_attempted >= detail_limit:
                    detail_limit_reached = True
                    deferred_detail_limit += 1
                    continue

                detail_pages_attempted += 1
                try:
                    detail = provider.get_title(url, session=session)
                except CrawlBlockedError:
                    raise
                except Exception as exc:
                    failed += 1
                    unresolved += 1
                    record_unresolved_title(
                        provider.provider_id,
                        url,
                        reference.title or url,
                        media_type,
                        poster_url=reference.poster_url,
                        error=f"Detail fetch failed: {type(exc).__name__}: {exc}",
                    )
                    if len(errors) < 20:
                        errors.append(
                            f"{url}: {type(exc).__name__}: {exc}"
                        )
                    continue

                # Prefer an IMDb ID exposed by the provider. Reuse a reviewed
                # mapping only for this exact provider URL; never title-match here.
                provider_imdb_id = str(detail.imdb_id or "").strip()
                resolved_id = (
                    provider_imdb_id
                    if _VALID_IMDB_ID.fullmatch(provider_imdb_id)
                    else get_resolved_imdb_id(detail.provider, detail.url)
                )
                if not resolved_id or not _VALID_IMDB_ID.fullmatch(str(resolved_id).strip()):
                    unresolved += 1
                    record_unresolved_title(
                        detail.provider,
                        detail.url,
                        detail.title or reference.title,
                        detail.media_type,
                        year=detail.year,
                        poster_url=detail.poster_url or reference.poster_url,
                        description=detail.description,
                        metadata={
                            **detail.metadata,
                            "poster_url": detail.poster_url,
                            "description": detail.description,
                            "provider_genres": detail.provider_genres,
                        },
                        error="Provider detail page did not expose a valid IMDb ID",
                    )
                    continue

                detail.imdb_id = str(resolved_id).strip()
                record_discovery(
                    detail.media_type,
                    detail.imdb_id,
                    detail.provider,
                    detail.title or reference.title,
                    provider_title=detail.title or reference.title,
                    provider_url=detail.url,
                    year=detail.year,
                    metadata={
                        **detail.metadata,
                        "poster_url": detail.poster_url,
                        "description": detail.description,
                        "provider_genres": detail.provider_genres,
                    },
                )
                mark_unresolved_title_resolved(
                    detail.provider, detail.url, detail.imdb_id
                )
                found += 1

            # Reaching the end means the index adapter completed normally.
            index_exhausted = True
            requests_made = session.request_count

    except CrawlBlockedError as exc:
        blocked = True
        if len(errors) < 20:
            errors.append(str(exc))
        if "session" in locals() and session is not None:
            requests_made = session.request_count
    except ProviderAlreadyRunningError as exc:
        blocked = True
        if len(errors) < 20:
            errors.append(str(exc))
    except KeyboardInterrupt:
        # Keep the run history consistent when an operator stops a long drain.
        # Every title processed before the interrupt has already been committed.
        finish_crawl_run(
            run_id,
            status="partial",
            pages_scanned=1 if index_exhausted else 0,
            titles_found=found,
            error="crawl interrupted by operator",
        )
        raise
    except Exception as exc:
        finish_crawl_run(
            run_id,
            status="failed",
            titles_found=found,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    index_complete = (
        index_exhausted
        and bool(getattr(provider, "catalog_scan_complete", False))
    )
    complete = index_complete and not blocked and not detail_limit_reached
    status = "completed" if complete else "partial"
    summary = []
    if failed:
        summary.append(f"{failed} detail-page errors")
    if unresolved:
        summary.append(f"{unresolved} unresolved IMDb identities")
    if blocked:
        summary.append("crawl stopped by provider safety guard")
    if detail_limit_reached:
        summary.append(
            f"detail-page budget reached ({detail_pages_attempted}/{detail_limit}); "
            f"{deferred_detail_limit} detail pages deferred"
        )
    if not complete and not blocked and not detail_limit_reached:
        summary.append("index scan did not complete")
    finish_crawl_run(
        run_id,
        status=status,
        pages_scanned=1 if index_exhausted else 0,
        titles_found=found,
        error="; ".join(summary) or (" | ".join(errors) if errors else None),
    )

    if complete and not index_only:
        reconciliation = reconcile_provider_availability(
            provider.provider_id, media_type, run_id
        )
    else:
        reconciliation = {
            "skipped": True,
            "reason": (
                "index_only_preflight"
                if index_only and complete
                else (
                    "detail_page_budget_reached"
                    if detail_limit_reached
                    else "crawl_not_complete_or_guarded"
                )
            ),
            "provider_entries_checked": 0,
            "provider_entries_marked_missing": 0,
            "titles_deactivated": 0,
        }

    return {
        "provider": provider.provider_id,
        "run_id": run_id,
        "status": status,
        "titles_recorded": found,
        "known_urls_skipped": skipped_known,
        "new_urls_deferred_index_only": deferred_index_only,
        "index_only": index_only,
        "max_details_per_run": detail_limit,
        "detail_pages_attempted": detail_pages_attempted,
        "detail_pages_deferred_by_limit": deferred_detail_limit,
        "detail_limit_reached": detail_limit_reached,
        "index_exhausted": index_exhausted,
        "index_complete": index_complete,
        "unresolved_retry_skipped": skipped_backoff,
        "unresolved_retried": retried_unresolved,
        "unresolved_imdb": unresolved,
        "title_errors": failed,
        "requests_made": requests_made,
        "blocked": blocked,
        "availability_reconciliation": reconciliation,
        "errors": errors,
    }


def crawl_named_provider(
    provider_id: str,
    *,
    index_only: bool = False,
    max_details: int | None = None,
) -> dict[str, Any]:
    provider_class = PROVIDERS.get(provider_id.strip().lower())
    if provider_class is None:
        raise ValueError(f"Unknown provider: {provider_id}")
    return crawl_provider(
        provider_class(), index_only=index_only, max_details=max_details
    )


def crawl_all_catalog_providers(
    *, index_only: bool = False, max_details: int | None = None
) -> list[dict[str, Any]]:
    """Run providers sequentially; a per-provider detail cap is not a fatal stop."""
    results = []
    for provider_id in ("aniworld", "serienstream"):
        result = crawl_named_provider(
            provider_id, index_only=index_only, max_details=max_details
        )
        results.append(result)
        if result.get("status") != "completed":
            # A budget stop is intentional and safe: continue with the next
            # provider, which receives its own independent detail-page budget.
            if (
                result.get("detail_limit_reached")
                and result.get("index_complete")
                and not result.get("blocked")
            ):
                continue
            break
    return results
