"""Pydantic models for the museum lighting orchestration API."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


class Priority(str, Enum):
    NORMAL = "normal"
    EMERGENCY = "emergency"


# Brightness is expressed in percent, 0..100 inclusive (0-100 maps onto 0-255 DMX).
Brightness = Annotated[float, Field(ge=0, le=100)]
FadeSeconds = Annotated[float, Field(ge=0, strict=False)]


def as_utc(dt: datetime) -> datetime:
    """Normalise every timestamp to timezone-aware UTC so intervals are comparable."""
    if dt.tzinfo is None:
        # The contract of the API is ISO 8601; a naive value is treated as UTC.
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class LampIn(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=64, examples=["lamp-A1"])]
    name: str | None = Field(default=None, max_length=128)
    # A lamp owns exactly one output channel (channel id == lamp id).
    default_level: Brightness = 0


class SceneItemIn(BaseModel):
    lamp_id: Annotated[str, Field(min_length=1, max_length=64)]
    level: Brightness
    fade_seconds: FadeSeconds = 0
    # Optional duration of the transition used when an emergency releases this
    # channel back to the scene that was running before preemption.
    restore_fade_seconds: FadeSeconds | None = Field(default=None, ge=0)


class SceneIn(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    name: str | None = Field(default=None, max_length=128)
    priority: Priority = Priority.NORMAL
    start_at: datetime = Field(description="ISO 8601 timestamp, inclusive.")
    end_at: datetime = Field(description="ISO 8601 timestamp, exclusive. Must be > start_at.")
    items: list[SceneItemIn] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=512)

    @field_validator("start_at", "end_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return as_utc(v)


class HallIn(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    name: str | None = Field(default=None, max_length=128)
    lamps: list[LampIn] = Field(default_factory=list)
    scenes: list[SceneIn] = Field(default_factory=list)

    def normalized(self) -> "HallIn":
        return self.model_validate(self.model_dump(mode="json"))


class BatchHallItem(BaseModel):
    """One hall in a batch publish: target id, optimistic-lock version, full spec.

    ``expected_version`` is the version the caller believes it is replacing;
    ``None`` (null) means the hall does not exist yet and this call creates it.
    """

    hall_id: Annotated[str, Field(min_length=1, max_length=64, examples=["bronze-gallery"])]
    expected_version: Annotated[
        int | None,
        Field(
            ge=1,
            default=None,
            description="Current version being replaced; null for a not-yet-saved hall.",
        ),
    ] = None
    orchestration: HallIn


class BatchPublishIn(BaseModel):
    # A batch must contain at least one hall; duplicate hall ids are rejected
    # by the endpoint (both occurrences are reported back).
    halls: Annotated[list[BatchHallItem], Field(min_length=1)]


class DispatchClaimIn(BaseModel):
    """Gateway claim request: which channel (optional), how many tasks, lease.

    ``channel`` narrows the claim to one channel; when omitted, the next due
    task of every channel is eligible (still at most one per channel, capped
    by ``limit``). ``lease_seconds`` is the hold duration after which an
    unacknowledged task may be re-claimed under a fresh token.
    """

    channel: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=64,
            default=None,
            description="Claim only this channel; omit to claim across all channels.",
            examples=["lamp-case-1"],
        ),
    ] = None
    limit: Annotated[int, Field(ge=1, description="Maximum number of tasks to claim in one call.")]
    lease_seconds: Annotated[
        float,
        Field(ge=0, le=31_536_000, description="Lease duration in seconds; 0 expires immediately."),
    ]


class DispatchAckIn(BaseModel):
    """Gateway acknowledgement: the task id and the lease token it was claimed with."""

    task_id: Annotated[str, Field(min_length=1, max_length=256)]
    lease_token: Annotated[str, Field(min_length=1, max_length=256)]
