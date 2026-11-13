"""Batch orchestration for resumable provider catalog crawls.

The crawler persists each resolved or unresolved provider URL as it is processed.
This module only schedules bounded provider runs; it does not change catalog or
on-demand season/episode loading behaviour.
"""
from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterable
from typing import Any


def _print_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def run_drain(
    provider_ids: Iterable[str],
    crawl_provider: Callable[..., dict[str, Any]],
    *,
    max_details: int | None = None,
    pause_min_minutes: float = 3.0,
    pause_max_minutes: float = 8.0,
    max_batches: int | None = None,
    sleep: Callable[[float], None] | None = None,
    uniform: Callable[[float, float], float] | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Drain providers in bounded batches, pausing between batches.

    Providers are processed sequentially. A provider is removed from the work
    once its index is complete and no detail-page budget was exhausted. A
    safety-guard block or incomplete index stops the entire drain immediately.
    """
    providers = tuple(
        dict.fromkeys(
            str(provider).strip().lower()
            for provider in provider_ids
            if str(provider).strip()
        )
    )
    if not providers:
        raise ValueError("At least one provider is required")

    pause_min = float(pause_min_minutes)
    pause_max = float(pause_max_minutes)
    if pause_min < 0 or pause_max < pause_min:
        raise ValueError("pause range must satisfy 0 <= min <= max")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be at least 1")

    sleeper = sleep or time.sleep
    choose_pause = uniform or random.uniform
    emit_event = emit or _print_event

    completed: list[str] = []
    batches = 0
    stop_reason: str | None = None

    for provider_index, provider_id in enumerate(providers):
        while True:
            if max_batches is not None and batches >= max_batches:
                stop_reason = "max_batches_reached"
                break

            batches += 1
            try:
                result = crawl_provider(provider_id, max_details=max_details)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                result = {
                    "provider": provider_id,
                    "status": "failed",
                    "blocked": False,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
                emit_event({
                    "event": "batch_finished",
                    "batch": batches,
                    "provider": provider_id,
                    "result": result,
                })
                stop_reason = f"provider_error:{provider_id}"
                break

            emit_event({
                "event": "batch_finished",
                "batch": batches,
                "provider": provider_id,
                "result": result,
            })

            if result.get("blocked"):
                stop_reason = f"blocked:{provider_id}"
                break

            if result.get("status") == "completed" and result.get("index_complete"):
                completed.append(provider_id)
                needs_another_batch = False
            elif result.get("detail_limit_reached") and result.get("index_complete"):
                needs_another_batch = True
            else:
                stop_reason = f"provider_incomplete:{provider_id}"
                break

            has_more_work = (
                needs_another_batch or provider_index < len(providers) - 1
            )
            if has_more_work:
                if max_batches is not None and batches >= max_batches:
                    stop_reason = "max_batches_reached"
                    break

                pause_seconds = max(0.0, float(choose_pause(pause_min, pause_max))) * 60
                next_provider = (
                    provider_id
                    if needs_another_batch
                    else providers[provider_index + 1]
                )
                emit_event({
                    "event": "batch_pause",
                    "after_batch": batches,
                    "provider": provider_id,
                    "next_provider": next_provider,
                    "pause_seconds": round(pause_seconds, 1),
                })
                if pause_seconds > 0:
                    sleeper(pause_seconds)

            if needs_another_batch:
                continue
            break

        if stop_reason:
            break

    remaining = [provider for provider in providers if provider not in completed]
    if stop_reason is None and remaining:
        stop_reason = "incomplete"

    if stop_reason is None:
        status = "completed"
        exit_code = 0
    elif stop_reason == "max_batches_reached":
        # A batch cap is an intentional, resumable pause rather than a failure.
        status = "paused"
        exit_code = 0
    else:
        status = "stopped"
        exit_code = 1

    summary = {
        "status": status,
        "batches": batches,
        "completed_providers": completed,
        "remaining_providers": remaining,
        "stop_reason": stop_reason,
        "max_batches": max_batches,
        "exit_code": exit_code,
    }
    emit_event({"event": "drain_finished", **summary})
    return summary
