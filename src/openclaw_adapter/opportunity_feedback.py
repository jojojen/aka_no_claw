"""Telegram inline-button feedback for Opportunity recommendations.

Three reactions on each Mercari notification:
- 👍 up      → promote the candidate to 🎯 Target (lenient threshold from now on)
- 👎 down    → 24h cooldown on the candidate; 3 in 7d auto-dismisses it
- 💰 bought  → 👍 + record purchase timestamp (signal for future analytics)
               + if the candidate is an official_store_preorder with a
               collab_case_id in metadata, schedules a CollabProfitBackfiller task

The store layer persists feedback_kind + feedback_at on the recommendation row;
cooldown_until and is_target live on the candidate row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from assistant_runtime import AssistantSettings, get_settings

from .opportunity_store import OpportunityStore

if TYPE_CHECKING:
    from .collab_profit_backfiller import CollabProfitBackfiller

logger = logging.getLogger(__name__)


FEEDBACK_KINDS: frozenset[str] = frozenset({"up", "down", "bought"})

# How long a single 👎 silences the candidate.
DEFAULT_DOWN_COOLDOWN_HOURS: int = 24
# 👎 count threshold within DOWN_COUNT_WINDOW_DAYS that triggers auto-dismiss.
DEFAULT_DOWN_DISMISS_THRESHOLD: int = 3
DEFAULT_DOWN_COUNT_WINDOW_DAYS: int = 7


def record_opportunity_feedback(
    *,
    recommendation_id: str,
    kind: str,
    settings: AssistantSettings | None = None,
    down_cooldown_hours: int = DEFAULT_DOWN_COOLDOWN_HOURS,
    down_dismiss_threshold: int = DEFAULT_DOWN_DISMISS_THRESHOLD,
    down_count_window_days: int = DEFAULT_DOWN_COUNT_WINDOW_DAYS,
    collab_backfiller: "CollabProfitBackfiller | None" = None,
) -> dict[str, object]:
    """Apply a feedback reaction to a recommendation.

    Returns a dict describing the side effects taken so the caller (Telegram
    callback handler) can render a useful toast.

    `settings` is optional — if omitted, falls back to the process-wide
    AssistantSettings. Tests can pass a stub.

    `collab_backfiller` is optional — when provided and kind is "bought", if
    the candidate is an official_store_preorder with a collab_case_id in its
    metadata, :meth:`CollabProfitBackfiller.record_purchase` is called to
    schedule 30d / 180d backfill tasks.
    """
    if kind not in FEEDBACK_KINDS:
        return {"status": "rejected", "reason": f"unknown kind: {kind}"}

    settings = settings or get_settings()
    store = OpportunityStore(settings.opportunity_db_path)
    store.bootstrap()

    candidate_id = store.record_feedback(recommendation_id, kind)
    if candidate_id is None:
        return {"status": "rejected", "reason": "recommendation not found"}

    result: dict[str, object] = {
        "status": "ok",
        "kind": kind,
        "candidate_id": candidate_id,
        "side_effects": [],
    }

    if kind in {"up", "bought"}:
        promoted = store.set_is_target(candidate_id, True)
        if promoted:
            result["side_effects"].append("promoted_to_target")
        logger.info(
            "Opportunity feedback %s candidate_id=%s promoted_to_target=%s",
            kind, candidate_id, promoted,
        )

        # 💰 bought + official_store_preorder → schedule collab backfill
        if kind == "bought" and collab_backfiller is not None:
            _maybe_schedule_collab_backfill(
                store=store,
                candidate_id=candidate_id,
                backfiller=collab_backfiller,
                result=result,
            )

        return result

    # kind == "down"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cooldown_until = (now + timedelta(hours=down_cooldown_hours)).isoformat()
    store.set_cooldown(candidate_id, cooldown_until)
    result["side_effects"].append("cooldown_started")
    result["cooldown_until"] = cooldown_until

    window_start = (now - timedelta(days=down_count_window_days)).isoformat()
    down_count = store.count_recent_feedback(
        candidate_id, "down", since_iso=window_start
    )
    result["down_count_in_window"] = down_count
    if down_count >= down_dismiss_threshold:
        dismissed = store.dismiss_candidate(candidate_id)
        if dismissed:
            result["side_effects"].append("auto_dismissed")
        store.set_cooldown(candidate_id, None)  # cooldown irrelevant once dismissed
        logger.info(
            "Opportunity feedback auto-dismissed candidate_id=%s down_count=%d",
            candidate_id, down_count,
        )
    else:
        logger.info(
            "Opportunity feedback down candidate_id=%s down_count=%d cooldown_until=%s",
            candidate_id, down_count, cooldown_until,
        )
    return result


# ── Collab backfill helper ──────────────────────────────────────────────────

def _maybe_schedule_collab_backfill(
    *,
    store: OpportunityStore,
    candidate_id: str,
    backfiller: "CollabProfitBackfiller",
    result: dict[str, object],
) -> None:
    """If the candidate is an official_store_preorder with a collab_case_id,
    schedule profit backfill tasks via *backfiller*.

    Modifies *result* in-place to append ``"collab_backfill_scheduled"`` to
    ``side_effects`` when a backfill is scheduled.
    """
    try:
        candidate = store.get_candidate(candidate_id)
    except Exception:
        logger.exception(
            "_maybe_schedule_collab_backfill: get_candidate failed candidate_id=%s",
            candidate_id,
        )
        return

    if candidate is None:
        return

    if candidate.source_kind != "official_store_preorder":
        return

    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    case_id = metadata.get("collab_case_id")
    if not case_id:
        logger.debug(
            "_maybe_schedule_collab_backfill: no collab_case_id in metadata "
            "candidate_id=%s",
            candidate_id,
        )
        return

    release_date = metadata.get("release_date") or None
    try:
        scheduled = backfiller.record_purchase(str(case_id), release_date=release_date)
    except Exception:
        logger.exception(
            "_maybe_schedule_collab_backfill: record_purchase failed case_id=%s",
            case_id,
        )
        return

    if scheduled:
        side_effects = result.get("side_effects")
        if isinstance(side_effects, list):
            side_effects.append("collab_backfill_scheduled")
        logger.info(
            "Opportunity bought feedback scheduled collab backfill "
            "candidate_id=%s case_id=%s",
            candidate_id, case_id,
        )
