"""Thread-safe in-memory store of validated hall orchestrations."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import HallIn
from .engine import compile_hall


@dataclass
class StoredHall:
    spec: HallIn
    plan: dict
    created_at: datetime
    updated_at: datetime
    version: int
    # Mutation generation of this hall. Unlike ``version`` it never resets:
    # a delete followed by a re-create still yields a fresh epoch, so the
    # dispatch board can tell generations apart even when version numbers
    # coincide (re-created halls start again at version 1).
    epoch: int


class HallStore:
    def __init__(self) -> None:
        self._halls: dict[str, StoredHall] = {}
        # Per-hall mutation generation; survives deletion on purpose.
        self._epochs: dict[str, int] = {}
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """The store-wide lock.

        The dispatch board (``app/dispatch.py``) shares it so that claims and
        acknowledgements serialise against single/batch publishes and deletes:
        a task can never be confirmed against a version that has already been
        replaced.
        """
        return self._lock

    def _bump_epoch(self, hall_id: str) -> int:
        """Advance the hall's mutation generation. Caller holds the lock."""
        epoch = self._epochs.get(hall_id, 0) + 1
        self._epochs[hall_id] = epoch
        return epoch

    def epoch_of(self, hall_id: str) -> int | None:
        """Current mutation generation of a hall (``None`` if never published)."""
        with self._lock:
            return self._epochs.get(hall_id)

    @staticmethod
    def compile(spec: HallIn) -> dict:
        return compile_hall(spec)

    def put(self, spec: HallIn) -> tuple[StoredHall | None, dict]:
        """Validate and persist. Returns ``(stored, error_body)`` — exactly one is set."""
        plan = compile_hall(spec)
        if not plan["ok"]:
            return None, plan
        now = datetime.now(timezone.utc)
        with self._lock:
            prev = self._halls.get(spec.id)
            stored = StoredHall(
                spec=spec,
                plan=plan,
                created_at=prev.created_at if prev else now,
                updated_at=now,
                version=(prev.version + 1) if prev else 1,
                epoch=self._bump_epoch(spec.id),
            )
            self._halls[spec.id] = stored
        return stored, None

    @staticmethod
    def version_conflict(hall_id: str, expected: int | None, current: int | None) -> dict:
        if expected is None:
            message = (
                f"Expected hall '{hall_id}' to be unsaved (expected_version=null), "
                f"but it currently exists at version {current}."
            )
        elif current is None:
            message = (
                f"Expected hall '{hall_id}' at version {expected}, but it does not exist."
            )
        else:
            message = (
                f"Expected hall '{hall_id}' at version {expected}, but current version is {current}."
            )
        return {
            "code": "version_mismatch",
            "hall_id": hall_id,
            "message": message,
            "expected_version": expected,
            "current_version": current,
        }

    def batch_check_versions(self, items: list[tuple[str, int | None]]) -> list[dict]:
        """Read-only optimistic-lock check for ``(hall_id, expected_version)``.

        Returns one ``version_mismatch`` conflict per item whose expected
        version differs from the current store state. Takes the lock so the
        snapshot is consistent; it never mutates anything.
        """
        conflicts: list[dict] = []
        with self._lock:
            for hall_id, expected in items:
                prev = self._halls.get(hall_id)
                current = prev.version if prev else None
                if expected != current:
                    conflicts.append(self.version_conflict(hall_id, expected, current))
        return conflicts

    def batch_commit(
        self, items: list[tuple[str, int | None, HallIn, dict]]
    ) -> tuple[list[StoredHall] | None, list[dict]]:
        """Atomically replace a batch of already-compiled halls.

        Each item is ``(hall_id, expected_version, spec, compiled_plan)`` and the
        caller guarantees every orchestration compiled successfully.

        Version comparison and every write happen in one critical section, so
        either all halls are replaced (each version incremented once) or nothing
        is written. Concurrent batches that expect the same version serialise
        here and at most the first one wins; later callers see the bumped version
        and are rejected.

        Returns ``(stored_halls, [])`` on success or ``(None, conflicts)`` with a
        ``version_mismatch`` per offending item when nothing was committed.
        """
        with self._lock:
            conflicts: list[dict] = []
            for hall_id, expected, _spec, _plan in items:
                prev = self._halls.get(hall_id)
                current = prev.version if prev else None
                if expected != current:
                    conflicts.append(self.version_conflict(hall_id, expected, current))

            # Re-check here (after the read-only pre-check) closes the race with
            # a concurrent single/batch publish between check and commit.
            if conflicts or not items:
                return None, conflicts

            now = datetime.now(timezone.utc)
            stored_list: list[StoredHall] = []
            for hall_id, _expected, spec, plan in items:
                prev = self._halls.get(hall_id)
                stored = StoredHall(
                    spec=spec,
                    plan=plan,
                    created_at=prev.created_at if prev else now,
                    updated_at=now,
                    version=(prev.version + 1) if prev else 1,
                    epoch=self._bump_epoch(hall_id),
                )
                self._halls[hall_id] = stored
                stored_list.append(stored)
        return stored_list, []

    def get(self, hall_id: str) -> StoredHall | None:
        with self._lock:
            return self._halls.get(hall_id)

    def list(self) -> list[StoredHall]:
        with self._lock:
            return list(self._halls.values())

    def delete(self, hall_id: str) -> bool:
        with self._lock:
            removed = self._halls.pop(hall_id, None) is not None
            if removed:
                # Deleting is a mutation too: outstanding leases of the removed
                # version must become unconfirmable (reported version_expired).
                self._bump_epoch(hall_id)
            return removed


store = HallStore()
