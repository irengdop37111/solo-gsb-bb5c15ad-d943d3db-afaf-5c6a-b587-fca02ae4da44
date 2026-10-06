"""Per-channel dispatch of due timeline commands to venue gateways.

A venue gateway claims due commands of a published hall version, executes them
and acknowledges each one with the lease token it was issued. The board
guarantees:

* per channel, commands are handed out strictly in execution-time order and
  the next command is claimable only after the previous one is acknowledged;
* a claimed (leased) task is never issued twice within its lease; once the
  lease expires the same task (same stable ``task_id``) may be re-claimed and
  is issued under a fresh token;
* an acknowledgement must carry the task's current token — replaying the same
  acknowledgement is idempotent, while a stale or foreign token is a conflict
  that changes nothing;
* re-publishing or deleting a hall invalidates every unacknowledged task of
  the old version: late acknowledgements are rejected as ``version_expired``
  and the new version never re-issues commands scheduled before its publish
  time. Failed publishes change nothing because the store itself is untouched.

All mutable state is guarded by the store's lock, so claims, acknowledgements
and publishes (single-hall or batch) serialise against each other and a task
can never be confirmed across versions.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .store import HallStore, StoredHall, store


@dataclass
class _TaskRecord:
    """One issued (claimed) task.

    Records are kept for the process lifetime; records of a superseded
    generation are unreachable in practice because acknowledgements compare
    the task's epoch against the hall's current epoch before anything else.
    """

    task_id: str
    hall_id: str
    epoch: int
    version: int
    channel: str
    index: int
    command: dict
    token: str
    expires_at: datetime
    acked: bool = False


@dataclass
class _ChannelQueue:
    """Delivery cursor of one (hall, epoch, channel) queue."""

    cursor: int = 0  # index of the next command to issue
    issued_task_id: str | None = None  # task currently leased at the cursor


class DispatchBoard:
    """Issues and tracks leased dispatch tasks for the current hall versions."""

    def __init__(self, hall_store: HallStore, clock: Callable[[], datetime] | None = None) -> None:
        self._store = hall_store
        # Injectable so tests can move time past a command's scheduled instant.
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._tasks: dict[str, _TaskRecord] = {}
        self._queues: dict[tuple[str, int, str], _ChannelQueue] = {}

    def reset(self) -> None:
        """Drop all dispatch state (paired with a store wipe in tests)."""
        with self._store.lock:
            self._tasks.clear()
            self._queues.clear()

    # ------------------------------------------------------------------ #
    # Schedule
    # ------------------------------------------------------------------ #
    @staticmethod
    def _dispatchable(stored: StoredHall) -> dict[str, list[dict]]:
        """Commands of this hall version that may be dispatched, per channel.

        A re-published version never re-issues commands scheduled before its
        publish time; the first version of a hall serves its whole timeline.
        """
        schedule: dict[str, list[dict]] = {}
        for channel, commands in stored.plan["timeline"].items():
            if stored.version > 1:
                published_at = stored.updated_at
                commands = [c for c in commands if datetime.fromisoformat(c["at"]) >= published_at]
            schedule[channel] = commands
        return schedule

    # ------------------------------------------------------------------ #
    # Claim
    # ------------------------------------------------------------------ #
    def claim(self, hall_id: str, channel: str | None, limit: int, lease_seconds: float) -> dict:
        """Issue up to ``limit`` due tasks (at most one per channel, in order)."""
        with self._store.lock:
            stored = self._store.get(hall_id)
            if stored is None:
                return {
                    "ok": False,
                    "status": 404,
                    "code": "hall_not_found",
                    "message": f"Hall '{hall_id}' does not exist.",
                }
            schedule = self._dispatchable(stored)
            if channel is not None and channel not in schedule:
                return {
                    "ok": False,
                    "status": 404,
                    "code": "unknown_channel",
                    "message": f"Hall '{hall_id}' has no channel '{channel}'.",
                }

            now = self.clock()
            expires_at = now + timedelta(seconds=lease_seconds)
            tasks: list[dict] = []
            channels = [channel] if channel is not None else sorted(schedule)
            for ch in channels:
                if len(tasks) >= limit:
                    break
                record = self._claim_next(hall_id, stored.epoch, stored.version, ch, schedule[ch], now, expires_at)
                if record is not None:
                    tasks.append(self._public(record))
            return {"ok": True, "stored": stored, "tasks": tasks}

    def _claim_next(
        self,
        hall_id: str,
        epoch: int,
        version: int,
        channel: str,
        commands: list[dict],
        now: datetime,
        expires_at: datetime,
    ) -> _TaskRecord | None:
        queue = self._queues.setdefault((hall_id, epoch, channel), _ChannelQueue())
        if queue.cursor >= len(commands):
            return None  # channel fully delivered and acknowledged
        command = commands[queue.cursor]
        if datetime.fromisoformat(command["at"]) > now:
            return None  # head command not due yet; strict order blocks the rest
        if queue.issued_task_id is not None:
            record = self._tasks[queue.issued_task_id]
            if record.expires_at > now:
                return None  # lease still active: never issued twice within a lease
            # Lease expired: re-issue the same task under a fresh token.
            record.token = secrets.token_urlsafe(16)
            record.expires_at = expires_at
            return record
        task_id = f"{hall_id}:g{epoch}:v{version}:{channel}:{queue.cursor:04d}"
        record = _TaskRecord(
            task_id=task_id,
            hall_id=hall_id,
            epoch=epoch,
            version=version,
            channel=channel,
            index=queue.cursor,
            command=command,
            token=secrets.token_urlsafe(16),
            expires_at=expires_at,
        )
        self._tasks[task_id] = record
        queue.issued_task_id = task_id
        return record

    @staticmethod
    def _public(record: _TaskRecord) -> dict:
        return {
            "task_id": record.task_id,
            "hall_id": record.hall_id,
            "version": record.version,
            "channel": record.channel,
            "command": record.command,
            "lease_token": record.token,
            "lease_expires_at": record.expires_at.isoformat(),
        }

    # ------------------------------------------------------------------ #
    # Acknowledge
    # ------------------------------------------------------------------ #
    def ack(self, hall_id: str, task_id: str, token: str) -> dict:
        """Confirm a claimed task. Exactly one of ``ok``/error keys is set."""
        with self._store.lock:
            record = self._tasks.get(task_id)
            if record is None or record.hall_id != hall_id:
                return {
                    "ok": False,
                    "status": 404,
                    "code": "unknown_task",
                    "message": f"Task '{task_id}' is not an issued task of hall '{hall_id}'.",
                }

            stored = self._store.get(hall_id)
            current_epoch = self._store.epoch_of(hall_id)
            current_version = stored.version if stored is not None else None
            if current_epoch != record.epoch:
                # Re-published, deleted, or deleted-then-recreated since the
                # task was issued: the generation it belongs to is gone.
                state = (
                    "no longer exists"
                    if stored is None
                    else f"is now at version {current_version}"
                )
                return {
                    "ok": False,
                    "status": 409,
                    "code": "version_expired",
                    "message": (
                        f"Task '{task_id}' was issued for hall '{hall_id}' version "
                        f"{record.version}, but the hall {state}; the outstanding "
                        "task is invalidated and was not confirmed."
                    ),
                    "task_version": record.version,
                    "current_version": current_version,
                }

            conflict = {
                "ok": False,
                "status": 409,
                "code": "token_conflict",
                "message": (
                    f"Lease token does not match the current token of task "
                    f"'{task_id}'; nothing was changed."
                ),
            }
            if record.acked:
                # Replaying the acknowledgement that already succeeded is
                # idempotent; any other token is a stale/foreign conflict.
                if record.token == token:
                    return {"ok": True, "record": record, "duplicate": True}
                return conflict

            if record.token != token:
                return conflict

            record.acked = True
            queue = self._queues[(record.hall_id, record.epoch, record.channel)]
            queue.cursor = record.index + 1
            queue.issued_task_id = None
            return {"ok": True, "record": record, "duplicate": False}


board = DispatchBoard(store)
