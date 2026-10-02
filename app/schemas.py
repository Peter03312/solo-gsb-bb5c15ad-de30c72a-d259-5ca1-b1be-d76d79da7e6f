"""Pydantic request schemas.

Structural problems (missing / wrong-typed fields) are reported by FastAPI
and normalised in main.py.  Cross-field semantic rules are checked in
semantic_validation so the whole request is rejected with explicit codes:

* ``radius`` must be positive;
* ``freq_low`` may not exceed ``freq_high`` (equal is allowed, a single
  frequency);
* ``start_time`` must be strictly earlier than ``end_time``.

``BatchApprovalRequest`` additionally requires a non-empty ID list; duplicate
IDs inside the list are checked in the handler (with the standard envelope),
not by Pydantic.

``AvailableRangesRequest`` carries the closed interval of candidate band
lower bounds for the available-ranges search (a non-mutating query).

``WaitlistRegistrationRequest`` is the optional body (``{"reason": ...}``)
when registering a pending application into the FIFO standby queue; the
waitlist cancel endpoint takes no body.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CircleIn(BaseModel):
    cx: float
    cy: float
    radius: float


class FrequencyBandIn(BaseModel):
    low: float
    high: float


class TimeWindowIn(BaseModel):
    # Timezone-aware ISO-8601 instants (UTC recommended, e.g. 2026-10-01T08:00:00Z).
    start: datetime
    end: datetime


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    circle: CircleIn
    frequency: FrequencyBandIn
    time_window: TimeWindowIn


class ApprovalRequest(BaseModel):
    # Revision the client based its preview decision on; required.
    expected_revision: int = Field(ge=0)


class BatchApprovalRequest(BaseModel):
    # Whole-group conditional approval: every listed application must still be
    # pending and every one must pass adjudication (against the current
    # effective set *and* against the rest of the group), or nothing is
    # approved.
    expected_revision: int = Field(ge=0)
    # Non-empty list of distinct application IDs, approved in this order.
    application_ids: list[int] = Field(min_length=1)


class RevocationRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)


class SuspensionRequest(BaseModel):
    # Temporary equipment downtime: take an approved, non-revoked license out
    # of the effective set.  Shares revision arbitration with every other
    # writer; the expected revision is mandatory.
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)


class ResumptionRequest(BaseModel):
    # Restore a suspended license after re-adjudication against the current
    # guard band and all effective licenses.  Shares revision arbitration;
    # a stale revision or renewed interference leaves it suspended and
    # consumes no revision.
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)


class GuardBandPreviewRequest(BaseModel):
    # Proposed guard band to dry-run against the current effective set.
    # Finite/non-negative checks run in semantic_validation so they use the
    # standard error envelope.
    guard_band: float


class GuardBandUpdateRequest(BaseModel):
    # Confirm a previously previewed value: the same proposed band plus the
    # revision the decision was based on.  Shares revision arbitration with
    # single/batch approvals and revocations.
    guard_band: float
    expected_revision: int = Field(ge=0)


class AvailableRangesRequest(BaseModel):
    # Closed interval of candidate lower bounds for the application's band;
    # the band width, coverage circle and time window stay as submitted.
    # Equal bounds query a single candidate position.  Finite/ordering
    # checks run in semantic_validation so they use the standard envelope.
    candidate_low_min: float
    candidate_low_max: float


class WaitlistRegistrationRequest(BaseModel):
    # Register a pending application into the FIFO standby queue while the
    # band is busy.  The body is optional; ``reason`` is purely informational.
    reason: str = Field(default="", max_length=2000)
