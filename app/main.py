"""HTTP API for scheduled museum lighting scene orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from .dispatch import board
from .models import BatchPublishIn, DispatchAckIn, DispatchClaimIn, HallIn, as_utc
from .simulator import simulate
from .store import StoredHall, store

app = FastAPI(
    title="Museum Lighting Orchestration API",
    version="1.1.0",
    description=(
        "Compile scheduled light scenes into an executable per-channel timeline. "
        "Validates lamp references, brightness ranges and non-overlapping fade intervals; "
        "emergency scenes preempt normal scenes and release only to scenes still in window. "
        "Venue gateways claim due commands per channel under a lease and acknowledge them "
        "with the lease token; re-publishing invalidates unacknowledged tasks of the old version."
    ),
)


def _stored_or_404(hall_id: str) -> StoredHall:
    stored = store.get(hall_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "hall_not_found", "message": f"Hall '{hall_id}' does not exist."},
        )
    return stored


def _plan_response(stored: StoredHall) -> dict[str, Any]:
    return {
        "hall_id": stored.spec.id,
        "name": stored.spec.name,
        "version": stored.version,
        "created_at": stored.created_at.isoformat(),
        "updated_at": stored.updated_at.isoformat(),
        **{k: stored.plan[k] for k in ("summary", "warnings", "timeline", "preemptions", "skipped_activations")},
    }


def _reject_invalid(plan: dict, hall_id: str) -> JSONResponse:
    # 422 with concrete channel / interval / reason; nothing was written.
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "hall_id": hall_id,
            "message": "Orchestration rejected; no timeline was saved.",
            "errors": plan["errors"],
            "warnings": plan["warnings"],
        },
    )


def _batch_failure_reason(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"code": reason, "message": message, **extra}
    return out


def _batch_rejected(failures: list[dict[str, Any]]) -> JSONResponse:
    # Any optimistic-lock conflict is a 409; pure content errors are 422.
    # Nothing in the batch was written and every stored version is unchanged.
    status_code = 409 if any(f["reason"]["code"] == "version_mismatch" for f in failures) else 422
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "message": "Batch publish rejected; no hall was written and all versions are unchanged.",
            "failures": failures,
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "halls": len(store.list())}


@app.put(
    "/halls/{hall_id}",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Validate and persist a hall orchestration (full replace)",
)
def put_hall(hall_id: str, hall: HallIn) -> Any:
    if hall.id != hall_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "id_mismatch",
                "message": f"Path hall id '{hall_id}' does not match body hall id '{hall.id}'.",
            },
        )
    stored, plan = store.put(hall)
    if stored is None:
        return _reject_invalid(plan, hall_id)
    return _plan_response(stored)


@app.post(
    "/halls/batch-publish",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Atomically validate and publish multiple full hall orchestrations",
)
def batch_publish(batch: BatchPublishIn) -> Any:
    """Publish a whole batch of halls or nothing.

    Each item carries the target ``hall_id``, the ``expected_version`` it is
    replacing (``null`` for a hall that has never been saved) and the full
    ``orchestration``. Items reuse the exact single-hall validation and
    timeline synthesis. Duplicate ids inside one request, an id/orchestration
    mismatch, an invalid orchestration, or an expected-version mismatch causes
    the whole batch to be rejected with per-hall reasons; no version changes.
    On success every hall is atomically replaced and its version increments by 1.
    """
    items = batch.halls

    # Phase 1 (no lock, pure functions): content checks that never depend on
    # current store state — duplicate ids, id/orchestration mismatch, compile.
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        (duplicates if item.hall_id in seen else seen).add(item.hall_id)

    failures: list[dict[str, Any]] = []
    content_failed: set[int] = set()
    # Every non-duplicate item still goes through the read-only version check,
    # so a batch where hall A is invalid and hall B is stale reports both at
    # once. Each item contributes at most one reason (a content error takes
    # precedence); only items that compiled successfully are ever committed.
    version_checks: list[tuple[str, int | None]] = []
    committable: list[tuple[str, int | None, HallIn, dict]] = []
    for index, item in enumerate(items):
        entry_base = {"index": index, "hall_id": item.hall_id}
        if item.hall_id in duplicates:
            failures.append(
                {
                    **entry_base,
                    "reason": _batch_failure_reason(
                        "duplicate_in_batch",
                        f"Hall id '{item.hall_id}' appears more than once in the same batch.",
                    ),
                }
            )
            content_failed.add(index)
            continue
        version_checks.append((item.hall_id, item.expected_version))
        if item.orchestration.id != item.hall_id:
            failures.append(
                {
                    **entry_base,
                    "reason": _batch_failure_reason(
                        "id_mismatch",
                        f"Item hall id '{item.hall_id}' does not match orchestration id "
                        f"'{item.orchestration.id}'.",
                    ),
                }
            )
            content_failed.add(index)
            continue
        plan = store.compile(item.orchestration)
        if not plan["ok"]:
            failures.append(
                {
                    **entry_base,
                    "reason": _batch_failure_reason(
                        "orchestration_invalid",
                        "Orchestration rejected; no timeline was saved.",
                        errors=plan["errors"],
                        warnings=plan["warnings"],
                    ),
                }
            )
            content_failed.add(index)
            continue
        committable.append((item.hall_id, item.expected_version, item.orchestration, plan))

    # Read-only optimistic-lock snapshot for every addressable item. A version
    # conflict is reported only when the item has no content failure already.
    conflicts = store.batch_check_versions(version_checks)
    for c in conflicts:
        hall_id = c["hall_id"]
        index = next(i for i, it in enumerate(items) if it.hall_id == hall_id)
        if index in content_failed:
            continue
        failures.append({"index": index, "hall_id": hall_id, "reason": c})

    if failures:
        failures.sort(key=lambda f: f["index"])
        return _batch_rejected(failures)

    # All content checks and versions match — replace the whole batch atomically.
    # batch_commit re-checks versions under the write lock, closing the race with
    # a concurrent publish between the pre-check above and this commit.
    stored_list, commit_conflicts = store.batch_commit(committable)
    if commit_conflicts:
        # Lost an optimistic-lock race (same expected version) to another writer.
        for c in commit_conflicts:
            hall_id = c["hall_id"]
            failures.append(
                {
                    "index": next(i for i, it in enumerate(items) if it.hall_id == hall_id),
                    "hall_id": hall_id,
                    "reason": c,
                }
            )
        failures.sort(key=lambda f: f["index"])
        return _batch_rejected(failures)

    # Preserve the order the caller submitted; each hall bumped its version once.
    assert stored_list is not None
    by_id = {s.spec.id: s for s in stored_list}
    return {
        "ok": True,
        "count": len(stored_list),
        "published": [_plan_response(by_id[it.hall_id]) for it in items],
    }


@app.post(
    "/validate",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Validate/compile without persisting (dry run)",
)
def validate(hall: HallIn) -> Any:
    plan = store.compile(hall)
    if not plan["ok"]:
        return _reject_invalid(plan, hall.id)
    return {"ok": True, "hall_id": hall.id, **{k: plan[k] for k in ("summary", "warnings", "timeline", "preemptions", "skipped_activations")}}


@app.get("/halls", tags=["halls"], summary="List persisted halls")
def list_halls() -> dict:
    items = [
        {
            "hall_id": s.spec.id,
            "name": s.spec.name,
            "version": s.version,
            "updated_at": s.updated_at.isoformat(),
            **s.plan["summary"],
        }
        for s in store.list()
    ]
    return {"halls": items, "count": len(items)}


@app.get("/halls/{hall_id}", tags=["halls"], summary="Get the compiled plan/timeline")
def get_hall(hall_id: str) -> dict:
    return _plan_response(_stored_or_404(hall_id))


@app.get(
    "/halls/{hall_id}/timeline",
    tags=["halls"],
    summary="Get just the executable per-channel command timeline",
)
def get_timeline(hall_id: str) -> dict:
    stored = _stored_or_404(hall_id)
    return {"hall_id": hall_id, "timeline": stored.plan["timeline"], "summary": stored.plan["summary"]}


@app.get(
    "/halls/{hall_id}/state",
    tags=["halls"],
    summary="Simulate effective channel levels at a given time",
)
def get_state(
    hall_id: str,
    at: datetime | None = Query(default=None, description="ISO 8601 time; defaults to now (UTC)."),
) -> dict:
    stored = _stored_or_404(hall_id)
    t = as_utc(at) if at is not None else datetime.now(timezone.utc)
    return simulate(stored.plan, t)


@app.delete(
    "/halls/{hall_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["halls"],
    summary="Delete a hall orchestration",
)
def delete_hall(hall_id: str) -> Response:
    if not store.delete(hall_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "hall_not_found", "message": f"Hall '{hall_id}' does not exist."},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Gateway dispatch: claim due commands per channel, acknowledge with the lease
# --------------------------------------------------------------------------- #
@app.post(
    "/halls/{hall_id}/dispatch/claim",
    status_code=status.HTTP_200_OK,
    tags=["dispatch"],
    summary="Gateway claims due commands (per channel, in order, leased)",
)
def claim_tasks(hall_id: str, claim: DispatchClaimIn) -> Any:
    """Hand out due commands of the hall's current version to a venue gateway.

    Per channel, commands are delivered strictly in execution-time order and
    the next one is claimable only after the previous one is acknowledged, so
    one claim returns at most one task per channel (up to ``limit`` in total;
    pass ``channel`` to claim a single channel). A leased task is never issued
    twice within its lease; after the lease expires the same ``task_id`` is
    re-issued under a fresh ``lease_token``. Commands scheduled before the
    current version's publish time are never re-issued.
    """
    result = board.claim(hall_id, claim.channel, claim.limit, claim.lease_seconds)
    if not result["ok"]:
        raise HTTPException(
            status_code=result["status"],
            detail={"code": result["code"], "message": result["message"]},
        )
    stored = result["stored"]
    return {
        "hall_id": hall_id,
        "version": stored.version,
        "count": len(result["tasks"]),
        "tasks": result["tasks"],
    }


@app.post(
    "/halls/{hall_id}/dispatch/ack",
    status_code=status.HTTP_200_OK,
    tags=["dispatch"],
    summary="Gateway acknowledges a claimed task with its lease token",
)
def ack_task(hall_id: str, ack: DispatchAckIn) -> Any:
    """Confirm a claimed task.

    The acknowledgement must carry the task's current lease token. Replaying
    the same acknowledgement is idempotent; a stale or foreign token is a 409
    conflict that changes nothing. If the hall was re-published or deleted
    since the task was issued, the acknowledgement is rejected as
    ``version_expired`` (409) — tasks are never confirmed across versions.
    """
    result = board.ack(hall_id, ack.task_id, ack.lease_token)
    if not result["ok"]:
        if result["status"] == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": result["code"], "message": result["message"]},
            )
        return JSONResponse(
            status_code=result["status"],
            content={
                "ok": False,
                "code": result["code"],
                "message": result["message"],
                "task_id": ack.task_id,
                **{k: result[k] for k in ("task_version", "current_version") if k in result},
            },
        )
    record = result["record"]
    return {
        "ok": True,
        "hall_id": hall_id,
        "task_id": record.task_id,
        "version": record.version,
        "channel": record.channel,
        "acknowledged": True,
        "duplicate": result["duplicate"],
    }
