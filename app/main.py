"""FastAPI application: temporary-event radio spectrum requests.

Endpoints
---------
POST   /api/applications                 submit (whole-request validation)
GET    /api/applications                 list submissions
GET    /api/applications/{id}            fetch one submission
POST   /api/applications/{id}/preview    conflicts + current revision
POST   /api/applications/{id}/available-ranges
                                       maximal free lower-bound ranges
POST   /api/applications/{id}/approve    conditional approval (expected rev.)
POST   /api/applications/batch-approve   atomic group approval (expected rev.)
POST   /api/applications/{id}/suspend    temporary outage: leave effective set
POST   /api/applications/{id}/resume     re-adjudicate and restore the license
POST   /api/applications/{id}/revoke     revoke (changes future effective set)
POST   /api/applications/{id}/waitlist   register a pending app as a standby
POST   /api/applications/{id}/waitlist/cancel
                                         withdraw a standby (no revision used)
GET    /api/waitlist                     the FIFO standby queue
GET    /api/applications/{id}/waitlist   one standby position
GET    /api/revision/current             current revision + effective set
GET    /api/revision/{revision}          as-of snapshot (all event types)
GET    /api/config/guard-band            live guard band + current revision
POST   /api/config/guard-band/preview    dry-run a proposed band (non-mutating)
POST   /api/config/guard-band            confirm a new band (expected rev.)
GET    /health                           liveness probe

Concurrency
-----------
Approval locks the singleton ``revision_meta`` row (``SELECT ... FOR UPDATE``)
for the duration of the transaction.  Concurrent single approvals, batch
approvals, suspensions, resumptions, revocations and guard-band changes
therefore serialise on the same revision arbitration: losers re-adjudicate
against the winner's commit and are refused with ``stale_revision`` or
``interference``.  A batch either consumes one consecutive revision per
member in input order, or nothing at all; a guard-band change, a suspension
or a resumption consumes exactly one revision.

Temporary outages
-----------------
A suspension immediately removes an approved license from the effective set
without touching its original approval.  A resumption re-adjudicates the
license against the live guard band and all currently effective licenses; a
stale revision or renewed interference is refused (returning the current
revision or the conflicting licenses) while the license stays suspended and
no revision is consumed.  A suspended license may also be permanently
revoked with the ordinary revoke operation; a revoked license can never be
resumed.  Both operations append immutable events so historical snapshots
restore the suspended/effective status and the effective set at every
revision.

Guard band
----------
The live guard band is stored on the ``revision_meta`` row and seeded once
from the ``GUARD_BAND`` environment variable (restarts never overwrite it).
Every adjudication (preview, single/batch approval, resumption) uses the
live value; each confirmed change is appended as an immutable
``guard_band_events`` record so historical revision snapshots restore the
band in force at the time.  Approval and resumption events keep their own
copy of the band they were decided under and are never rewritten.

Waitlist
--------
A pending application may be registered as a standby while the band is
busy.  Registration and cancellation consume no revision and never change
the application's status; an application is queued at most once.  When a
suspension or a revoke releases an effective license, the same transaction
drains the queue in registration order: each entry is re-adjudicated under
the live guard band against the licenses effective so far (including the
automatic approvals just granted), passers are approved immediately and
consume the following consecutive revisions, blockers keep their place and
the drain continues.  A manual approval removes the queue entry first, so
a manually decided application is never auto-approved.  Every automatic
approval is an ordinary ``ApprovalEvent`` flagged ``auto_approved`` with the
release revision, which lets revision snapshots restore the automatic
decisions one revision at a time.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from . import adjudication
from .adjudication import Circle, Conflict, License, PairConflict
from .config import settings
from .db import SessionLocal, engine, init_db, wait_for_database
from .errors import APIError, api_error_handler
from .models import (
    Application,
    ApprovalEvent,
    GuardBandEvent,
    ResumptionEvent,
    RevisionMeta,
    RevocationEvent,
    SuspensionEvent,
    WaitlistEntry,
)
from .schemas import (
    ApplicationCreate,
    ApprovalRequest,
    AvailableRangesRequest,
    BatchApprovalRequest,
    GuardBandPreviewRequest,
    GuardBandUpdateRequest,
    ResumptionRequest,
    RevocationRequest,
    SuspensionRequest,
    WaitlistRegistrationRequest,
)
from .semantic_validation import (
    validate_application,
    validate_candidate_low_range,
    validate_guard_band,
)

log = logging.getLogger("radio.api")

# Error codes
E_VALIDATION = "validation_failed"
E_NOT_FOUND = "not_found"
E_NOT_PENDING = "not_pending"
E_ALREADY_REVOKED = "already_revoked"
E_NOT_APPROVED = "not_approved"
E_ALREADY_SUSPENDED = "already_suspended"
E_NOT_SUSPENDED = "not_suspended"
E_REVISION_OUT_OF_RANGE = "revision_out_of_range"
E_STALE_REVISION = "stale_revision"
E_INTERFERENCE = "interference"
E_DUPLICATE_APPLICATION = "duplicate_application_id"
E_ALREADY_WAITLISTED = "already_waitlisted"
E_NOT_WAITLISTED = "not_waitlisted"


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def _utc(value: datetime) -> datetime:
    """Coerce a possibly naive timestamp (e.g. under SQLite) to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def serialize_application(app: Application) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "circle": {"cx": app.cx, "cy": app.cy, "radius": app.radius},
        "frequency": {"low": app.freq_low, "high": app.freq_high},
        "time_window": {"start": iso(app.start_time), "end": iso(app.end_time)},
        "status": app.status,
        "approval_revision": app.approval_revision,
        "suspended_revision": app.suspended_revision,
        "created_at": iso(app.created_at),
    }


def _license_view(lic: License) -> dict[str, Any]:
    """The public shape of a license as a conflict/snapshot party."""
    return {
        "id": lic.application_id,
        "circle": {
            "cx": lic.circle.cx,
            "cy": lic.circle.cy,
            "radius": lic.circle.radius,
        },
        "frequency": {"low": lic.freq_low, "high": lic.freq_high},
        "time_window": {
            "start": iso(lic.start_time),
            "end": iso(lic.end_time),
        },
    }


def serialize_conflict(conflict: Conflict, guard_band: float) -> dict[str, Any]:
    """One conflicting effective grant with the trigger conditions."""
    return {
        "application": _license_view(conflict.other),
        "trigger": {
            "time_intersect": conflict.time_overlap,
            "area_intersect": conflict.area_overlap,
            "frequency_intersect": conflict.frequency_intersects,
            "minimum_frequency_gap": conflict.minimum_frequency_gap,
            "within_guard_band": (
                conflict.minimum_frequency_gap < guard_band
            ),
        },
        "reason": conflict.reason,
    }


def serialize_pair_conflict(
    pair: PairConflict, guard_band: float
) -> dict[str, Any]:
    """One interfering pair of effective licenses with trigger conditions.

    Same trigger shape as a preview conflict so existing clients can reuse
    their rendering.
    """
    return {
        "first": _license_view(pair.first),
        "second": _license_view(pair.second),
        "trigger": {
            "time_intersect": pair.time_overlap,
            "area_intersect": pair.area_overlap,
            "frequency_intersect": pair.frequency_intersects,
            "minimum_frequency_gap": pair.minimum_frequency_gap,
            "within_guard_band": (
                pair.minimum_frequency_gap < guard_band
            ),
        },
        "reason": pair.reason,
    }


def to_license(app: Application) -> License:
    return License(
        application_id=app.id,
        circle=Circle(app.cx, app.cy, app.radius),
        freq_low=app.freq_low,
        freq_high=app.freq_high,
        start_time=_utc(app.start_time),
        end_time=_utc(app.end_time),
    )


def serialize_group_conflict(
    conflict: Conflict, scope: str, guard_band: float
) -> dict[str, Any]:
    """A conflict inside a group decision, tagged with where it came from.

    ``scope`` is ``"effective"`` when the other side is a currently valid
    license, or ``"group"`` when it is another application in the same batch.
    The body is the same shape as a preview conflict so existing clients can
    reuse their rendering.
    """
    body = serialize_conflict(conflict, guard_band)
    body["scope"] = scope
    return body


def load_effective_licenses(session) -> list[License]:
    """Approved and neither suspended nor revoked = the effective set."""
    rows = (
        session.query(Application)
        .filter(Application.status == "approved")
        .order_by(Application.id)
        .all()
    )
    return [to_license(row) for row in rows]


# --------------------------------------------------------------------------- #
# Waitlist (standby queue for pending applications while the band is busy)
# --------------------------------------------------------------------------- #
def serialize_waitlist_entry(
    session, entry: WaitlistEntry, position: int
) -> dict[str, Any]:
    """Public shape of one queue position; the application stays ``pending``."""
    app = session.get(Application, entry.application_id)
    return {
        "position": position,
        "application_id": entry.application_id,
        "reason": entry.reason,
        "registered_at": iso(entry.created_at),
        "application": serialize_application(app) if app else None,
    }


def load_waitlist(session) -> list[WaitlistEntry]:
    """The current standby queue in registration (FIFO) order."""
    return (
        session.query(WaitlistEntry)
        .order_by(WaitlistEntry.id)
        .all()
    )


def drain_waitlist(
    session,
    meta: RevisionMeta,
    release_revision: int,
) -> dict[str, Any]:
    """Re-adjudicate the standby queue after an effective license was released.

    Runs inside the suspend/revoke transaction, after that operation has
    already consumed its single revision (``release_revision``).  Entries are
    visited strictly in registration order and re-adjudicated against the
    current guard band and the licenses effective *so far*:

    * an entry with no conflicts is approved immediately, consumes the next
      consecutive revision, leaves the queue and joins the effective set, so
      later entries are also checked against it;
    * an entry that still interferes keeps its place in the queue and the
      drain continues with the following entries.

    Manually approved applications have had their queue entries removed by
    the approve/batch handlers, so a queued application is never
    auto-approved twice.  Returns the structured result embedded in the
    suspend/revoke response; every created event is a normal
    ``ApprovalEvent`` flagged ``auto_approved`` with ``release_revision`` so
    historical revision snapshots restore the automatic decisions.
    """
    guard_band = meta.guard_band
    effective = load_effective_licenses(session)

    approved: list[dict[str, Any]] = []
    pending_details: list[dict[str, Any]] = []
    for entry in load_waitlist(session):
        row = session.get(Application, entry.application_id)
        # Defensive: only genuinely pending applications can be queued; skip
        # anything that has since changed state without its entry being
        # removed.
        if row is None or row.status != "pending":
            continue
        candidate = to_license(row)
        conflicts = adjudication.evaluate(candidate, effective, guard_band)
        if conflicts:
            # Still blocked: keep the queue entry and carry on with the rest.
            pending_details.append(
                {
                    "application_id": row.id,
                    "conflicts": [
                        serialize_conflict(c, guard_band) for c in conflicts
                    ],
                }
            )
            continue

        # Passes now: approve immediately, one more revision, same band.
        meta.revision += 1
        session.add(
            ApprovalEvent(
                revision=meta.revision,
                application_id=row.id,
                guard_band=guard_band,
                auto_approved=True,
                release_revision=release_revision,
            )
        )
        row.status = "approved"
        row.approval_revision = meta.revision
        session.delete(entry)
        effective.append(candidate)
        approved.append(
            {
                "application_id": row.id,
                "revision": meta.revision,
                "guard_band": guard_band,
                "release_revision": release_revision,
                "application": serialize_application(row),
            }
        )

    return {
        "auto_approved_application_ids": [a["application_id"] for a in approved],
        "auto_approvals": approved,
        "remaining_waitlist_application_ids": [
            d["application_id"] for d in pending_details
        ],
        "waitlist_pending": pending_details,
        "final_revision": meta.revision,
    }


# --------------------------------------------------------------------------- #
# Revision snapshots
# --------------------------------------------------------------------------- #
def guard_band_at(session, revision: int) -> float:
    """Guard band in force at ``revision`` (latest change at or before it)."""
    event = (
        session.query(GuardBandEvent)
        .filter(GuardBandEvent.revision <= revision)
        .order_by(GuardBandEvent.revision.desc())
        .first()
    )
    if event is not None:
        return event.guard_band
    # Defensive: the revision-0 seed guarantees an event; fall back to the
    # live setting if it is somehow missing.
    meta = session.get(RevisionMeta, 1)
    return meta.guard_band if meta is not None else settings.guard_band


def build_snapshot(session, revision: int) -> dict[str, Any]:
    """Reconstruct the state of the world *at* ``revision``.

    Returns every approval event with revision <= the target and the
    application snapshot, replays the approval/suspension/resumption/
    revocation timeline of the period to flag each grant's status as of the
    target revision, and includes all suspension, resumption and revocation
    events of the period, plus the guard band in force at that revision and
    the guard-band records behind it.  Original approval events are never
    rewritten: a resumption is a separate event carrying its own band
    snapshot.
    """
    approvals = (
        session.query(ApprovalEvent)
        .filter(ApprovalEvent.revision <= revision)
        .order_by(ApprovalEvent.revision)
        .all()
    )
    revocations = (
        session.query(RevocationEvent)
        .filter(RevocationEvent.revision <= revision)
        .order_by(RevocationEvent.revision)
        .all()
    )
    suspensions = (
        session.query(SuspensionEvent)
        .filter(SuspensionEvent.revision <= revision)
        .order_by(SuspensionEvent.revision)
        .all()
    )
    resumptions = (
        session.query(ResumptionEvent)
        .filter(ResumptionEvent.revision <= revision)
        .order_by(ResumptionEvent.revision)
        .all()
    )
    guard_events = (
        session.query(GuardBandEvent)
        .filter(GuardBandEvent.revision <= revision)
        .order_by(GuardBandEvent.revision)
        .all()
    )

    # Replay every lifecycle event up to the target revision in revision
    # order; the last event of each application decides its as-of state.
    # (revision, kind, application_id, marker_revision); approval precedes
    # any other event of the same revision because every revision has exactly
    # one event.
    lifecycle: list[tuple[int, str, int]] = []
    lifecycle.extend((ev.revision, "approved", ev.application_id) for ev in approvals)
    lifecycle.extend(
        (ev.revision, "suspended", ev.application_id) for ev in suspensions
    )
    lifecycle.extend(
        (ev.revision, "approved", ev.application_id) for ev in resumptions
    )
    lifecycle.extend((ev.revision, "revoked", ev.application_id) for ev in revocations)
    lifecycle.sort(key=lambda item: item[0])
    # state[app_id] = (state, latest marker revision): marker is the
    # suspension revision while suspended, the revocation revision once
    # revoked, and irrelevant while approved.
    state: dict[int, tuple[str, int]] = {}
    for rev, kind, app_id in lifecycle:
        if kind == "suspended":
            state[app_id] = ("suspended", rev)
        elif kind == "revoked":
            state[app_id] = ("revoked", rev)
        else:
            state[app_id] = ("approved", rev)

    approval_records: list[dict[str, Any]] = []
    effective_ids: list[int] = []
    suspended_ids: list[int] = []
    for ev in approvals:
        app = session.get(Application, ev.application_id)
        status_as_of, marker = state.get(
            ev.application_id, ("approved", ev.revision)
        )
        record = {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "guard_band": ev.guard_band,
            # Automatic approvals from a waitlist release share the ordinary
            # approval event stream; the flags let history restore which
            # decisions were automatic and which release triggered them.
            "auto_approved": bool(ev.auto_approved),
            "release_revision": ev.release_revision,
            "created_at": iso(ev.created_at),
            # Status of this grant *as of the queried revision*.
            "status_as_of": status_as_of,
            "application": serialize_application(app) if app else None,
        }
        if status_as_of == "revoked":
            record["revoked_at_revision"] = marker
        elif status_as_of == "suspended":
            record["suspended_at_revision"] = marker
            suspended_ids.append(ev.application_id)
        else:
            effective_ids.append(ev.application_id)
        approval_records.append(record)

    revocation_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        for ev in revocations
    ]
    suspension_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        for ev in suspensions
    ]
    resumption_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "guard_band": ev.guard_band,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        for ev in resumptions
    ]

    return {
        "revision": revision,
        "guard_band": guard_band_at(session, revision),
        "guard_band_changes": [
            {
                "revision": ev.revision,
                "previous_guard_band": ev.previous_guard_band,
                "guard_band": ev.guard_band,
                "created_at": iso(ev.created_at),
            }
            for ev in guard_events
        ],
        "effective_application_ids": effective_ids,
        "suspended_application_ids": suspended_ids,
        "approvals": approval_records,
        "suspensions": suspension_records,
        "resumptions": resumption_records,
        "revocations": revocation_records,
    }


# --------------------------------------------------------------------------- #
# Application factory / lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_database()
    init_db()
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        live_band = meta.guard_band if meta is not None else settings.guard_band
    log.info(
        "startup complete; guard_band=%s (GUARD_BAND seed=%s) listening on %s:%s",
        live_band,
        settings.guard_band,
        settings.app_host,
        settings.app_port,
    )
    yield
    engine.dispose()


app = FastAPI(
    title="Temporary Event Radio Spectrum API",
    version="1.0.0",
    description=(
        "Submit, preview, approve and revoke temporary radio applications. "
        "Frequencies are closed intervals, UTC times are half-open intervals, "
        "coverage is a planar circle."
    ),
    lifespan=lifespan,
)
app.add_exception_handler(APIError, api_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Normalise FastAPI structural errors into the standard error envelope."""
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": E_VALIDATION,
                "message": "request validation failed; the whole request was rejected",
                "details": details,
            }
        },
    )


# --------------------------------------------------------------------------- #
# Health / revisions
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/revision/current", tags=["revision"])
def current_revision() -> dict[str, Any]:
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        revision = meta.revision if meta else 0
        guard_band = meta.guard_band if meta else settings.guard_band
        snapshot = build_snapshot(session, revision)
        effective_applications = [
            serialize_application(app)
            for app_id in snapshot["effective_application_ids"]
            if (app := session.get(Application, app_id)) is not None
        ]
        suspended_applications = [
            serialize_application(app)
            for app_id in snapshot["suspended_application_ids"]
            if (app := session.get(Application, app_id)) is not None
        ]
        waitlist_entries = [
            serialize_waitlist_entry(session, entry, position)
            for position, entry in enumerate(load_waitlist(session), start=1)
        ]
    return {
        "current_revision": revision,
        "guard_band": guard_band,
        "effective_application_ids": snapshot["effective_application_ids"],
        "effective_applications": effective_applications,
        "suspended_application_ids": snapshot["suspended_application_ids"],
        "suspended_applications": suspended_applications,
        "waitlist_application_ids": [e["application_id"] for e in waitlist_entries],
        "waitlist_entries": waitlist_entries,
        "approvals": snapshot["approvals"],
        "suspensions": snapshot["suspensions"],
        "resumptions": snapshot["resumptions"],
        "revocations": snapshot["revocations"],
        "guard_band_changes": snapshot["guard_band_changes"],
    }


@app.get("/api/revision/{revision}", tags=["revision"])
def revision_snapshot(revision: int) -> dict[str, Any]:
    if revision < 0:
        raise APIError(
            E_REVISION_OUT_OF_RANGE,
            "revision must be >= 0",
            status_code=422,
        )
    with SessionLocal() as session:
        current = session.get(RevisionMeta, 1).revision
        if revision > current:
            raise APIError(
                E_REVISION_OUT_OF_RANGE,
                f"revision {revision} is in the future; current revision is {current}",
                status_code=404,
                details={"current_revision": current},
            )
        return build_snapshot(session, revision)


# --------------------------------------------------------------------------- #
# Applications: create / list / get
# --------------------------------------------------------------------------- #
@app.post("/api/applications", status_code=201, tags=["applications"])
def create_application(payload: ApplicationCreate) -> dict[str, Any]:
    errors = validate_application(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the whole application was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )

    app_row = Application(
        name=payload.name,
        cx=payload.circle.cx,
        cy=payload.circle.cy,
        radius=payload.circle.radius,
        freq_low=payload.frequency.low,
        freq_high=payload.frequency.high,
        start_time=payload.time_window.start.astimezone(timezone.utc),
        end_time=payload.time_window.end.astimezone(timezone.utc),
        status="pending",
    )
    with SessionLocal() as session, session.begin():
        session.add(app_row)
        session.flush()
        result = serialize_application(app_row)
    return result


@app.get("/api/applications", tags=["applications"])
def list_applications(status: str | None = None) -> dict[str, Any]:
    with SessionLocal() as session:
        query = session.query(Application).order_by(Application.id)
        if status is not None:
            if status not in {"pending", "approved", "suspended", "revoked"}:
                raise APIError(
                    E_VALIDATION,
                    "status filter must be one of pending, approved, "
                    "suspended, revoked",
                    status_code=422,
                )
            query = query.filter(Application.status == status)
        rows = query.all()
        return {"applications": [serialize_application(row) for row in rows]}


@app.get("/api/applications/{application_id}", tags=["applications"])
def get_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        return serialize_application(row)


# --------------------------------------------------------------------------- #
# Preview (non-mutating)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/preview", tags=["applications"])
def preview_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}, only pending "
                "applications can be previewed",
                status_code=409,
                details={"status": row.status},
            )

        meta = session.get(RevisionMeta, 1)
        current = meta.revision
        guard_band = meta.guard_band
        effective = load_effective_licenses(session)
        conflicts = adjudication.evaluate(to_license(row), effective, guard_band)

    return {
        "application_id": application_id,
        "current_revision": current,
        "guard_band": guard_band,
        "has_conflicts": bool(conflicts),
        "conflicts": [serialize_conflict(c, guard_band) for c in conflicts],
    }


# --------------------------------------------------------------------------- #
# Available ranges (non-mutating search over candidate band lower bounds)
# --------------------------------------------------------------------------- #
@app.post(
    "/api/applications/{application_id}/available-ranges",
    tags=["applications"],
)
def find_available_ranges(
    application_id: int, payload: AvailableRangesRequest
) -> dict[str, Any]:
    """Every maximal interference-free range of candidate band lower bounds.

    The application keeps its coverage circle, UTC time window and band
    *width* (possibly zero for a single frequency); only the lower bound of
    its band varies over the closed candidate interval
    ``[candidate_low_min, candidate_low_max]``.  Adjudication uses the
    effective licenses and the live guard band of the current revision,
    exactly like ``preview``: only grants intersecting in space and time
    block, touching band endpoints interfere, and a separation strictly
    smaller than the guard band interferes.  Each returned range flags
    whether its endpoints are attainable; the list is empty when no
    candidate lower bound is usable.  The query never mutates applications,
    licenses or the revision.
    """
    errors = validate_candidate_low_range(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the available-ranges query was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}, only pending "
                "applications can be searched for available ranges",
                status_code=409,
                details={"status": row.status},
            )

        meta = session.get(RevisionMeta, 1)
        current = meta.revision
        guard_band = meta.guard_band
        bandwidth = row.freq_high - row.freq_low
        effective = load_effective_licenses(session)
        ranges = adjudication.available_ranges(
            to_license(row),
            effective,
            guard_band,
            payload.candidate_low_min,
            payload.candidate_low_max,
        )

    return {
        "application_id": application_id,
        "current_revision": current,
        "guard_band": guard_band,
        "bandwidth": bandwidth,
        "candidate_low_min": payload.candidate_low_min,
        "candidate_low_max": payload.candidate_low_max,
        "available_ranges": [
            {
                "min": r.min,
                "max": r.max,
                "min_inclusive": r.min_inclusive,
                "max_inclusive": r.max_inclusive,
            }
            for r in ranges
        ],
    }


# --------------------------------------------------------------------------- #
# Approve (conditional on expected revision, row-lock serialised)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/approve", tags=["applications"])
def approve_application(
    application_id: int, payload: ApprovalRequest
) -> dict[str, Any]:
    with SessionLocal() as session:
        try:
            with session.begin():
                # Serialise against every other approve/revoke/guard-band
                # change on the singleton row (a real row lock on
                # PostgreSQL).
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked",
                        status_code=409,
                    )
                if row.status == "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is already approved at "
                        f"revision {row.approval_revision}",
                        status_code=409,
                        details={"approval_revision": row.approval_revision},
                    )

                # Optimistic revision check against the locked, latest revision.
                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and re-decide",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock against the latest effective
                # set and the live guard band.
                effective = load_effective_licenses(session)
                conflicts = adjudication.evaluate(
                    to_license(row), effective, guard_band
                )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"application {application_id} interferes with "
                        f"{len(conflicts)} effective grant(s); frequency not granted",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )

                meta.revision = current + 1
                session.add(
                    ApprovalEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        guard_band=guard_band,
                    )
                )
                row.status = "approved"
                row.approval_revision = meta.revision
                # A manually approved standby leaves the queue so it can
                # never be auto-approved afterwards.
                entry = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.application_id == application_id)
                    .one_or_none()
                )
                if entry is not None:
                    session.delete(entry)
                session.flush()
                response = {
                    "approved": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "guard_band": guard_band,
                    "application": serialize_application(row),
                }
        except IntegrityError:
            # Last line of defence (e.g. backends without row locks such as
            # SQLite's tests): the revision slot was taken concurrently.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Batch approve (all-or-nothing group approval under one revision lock)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/batch-approve", tags=["applications"])
def batch_approve_applications(payload: BatchApprovalRequest) -> dict[str, Any]:
    # Structural shape (non-empty list, non-negative revision) is enforced by
    # Pydantic; the uniqueness rule goes through the standard error envelope.
    seen: set[int] = set()
    duplicates: list[int] = []
    for app_id in payload.application_ids:
        if app_id in seen:
            duplicates.append(app_id)
        seen.add(app_id)
    if duplicates:
        raise APIError(
            E_VALIDATION,
            "the whole batch was rejected: application_ids must be distinct",
            status_code=422,
            details=[
                {
                    "field": "application_ids",
                    "code": E_DUPLICATE_APPLICATION,
                    "message": (
                        "application_ids must not contain duplicates; "
                        f"repeated: {duplicates}"
                    ),
                    "duplicates": duplicates,
                }
            ],
        )

    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as single approve/revoke/guard-band
                # change: all writers share one revision arbitration and
                # serialise here.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                rows_by_id = {
                    row.id: row
                    for row in (
                        session.query(Application)
                        .filter(Application.id.in_(payload.application_ids))
                        .all()
                    )
                }

                # Failure type 1: unknown IDs (batch is not approved at all).
                missing = [
                    app_id
                    for app_id in payload.application_ids
                    if app_id not in rows_by_id
                ]
                if missing:
                    raise APIError(
                        E_NOT_FOUND,
                        f"{len(missing)} application(s) do not exist; the "
                        "whole batch was rejected and no revision was consumed",
                        status_code=404,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "missing_application_ids": missing,
                        },
                    )

                # Failure type 2: any member no longer pending (same precedence
                # as single approval: revoked outranks already-approved).
                revoked = [
                    app_id
                    for app_id in payload.application_ids
                    if rows_by_id[app_id].status == "revoked"
                ]
                if revoked:
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application(s) {revoked} have been revoked; the "
                        "whole batch was rejected and no revision was consumed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "applications": [
                                {
                                    "application_id": app_id,
                                    "status": rows_by_id[app_id].status,
                                    "approval_revision": rows_by_id[
                                        app_id
                                    ].approval_revision,
                                }
                                for app_id in payload.application_ids
                                if rows_by_id[app_id].status != "pending"
                            ],
                        },
                    )
                not_pending = [
                    app_id
                    for app_id in payload.application_ids
                    if rows_by_id[app_id].status != "pending"
                ]
                if not_pending:
                    raise APIError(
                        E_NOT_PENDING,
                        f"application(s) {not_pending} are not pending; the "
                        "whole batch was rejected and no revision was consumed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "applications": [
                                {
                                    "application_id": app_id,
                                    "status": rows_by_id[app_id].status,
                                    "approval_revision": rows_by_id[
                                        app_id
                                    ].approval_revision,
                                }
                                for app_id in not_pending
                            ],
                        },
                    )

                # Failure type 3: stale revision (a concurrent decision won).
                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the whole batch was rejected",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Failure type 4: interference, re-adjudicated under the lock.
                # Every member is checked both against the current effective
                # set and against every other member of the group.
                ordered_rows = [rows_by_id[i] for i in payload.application_ids]
                candidates = [to_license(row) for row in ordered_rows]
                effective = load_effective_licenses(session)
                against_effective, within_group = adjudication.evaluate_group(
                    candidates, effective, guard_band
                )
                interference_details = []
                for candidate in candidates:
                    eff_conflicts = [
                        serialize_group_conflict(c, "effective", guard_band)
                        for c in against_effective[candidate.application_id]
                    ]
                    group_conflicts = [
                        serialize_group_conflict(c, "group", guard_band)
                        for c in within_group[candidate.application_id]
                    ]
                    if eff_conflicts or group_conflicts:
                        interference_details.append(
                            {
                                "application_id": candidate.application_id,
                                "conflicts": eff_conflicts + group_conflicts,
                            }
                        )
                if interference_details:
                    raise APIError(
                        E_INTERFERENCE,
                        f"{len(interference_details)} application(s) in the "
                        "batch interfere with effective grants or with each "
                        "other; the whole batch was rejected and no "
                        "frequency was granted",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "applications": interference_details,
                        },
                    )

                # All clear: one ApprovalEvent per member, consecutive
                # revisions assigned in the submitted order, one commit.
                start_revision = current
                results = []
                for row in ordered_rows:
                    meta.revision += 1
                    session.add(
                        ApprovalEvent(
                            revision=meta.revision,
                            application_id=row.id,
                            guard_band=guard_band,
                        )
                    )
                    row.status = "approved"
                    row.approval_revision = meta.revision
                    # Manually approved standbys leave the queue: they must
                    # not be auto-approved a second time during a later
                    # release.
                    entry = (
                        session.query(WaitlistEntry)
                        .filter(WaitlistEntry.application_id == row.id)
                        .one_or_none()
                    )
                    if entry is not None:
                        session.delete(entry)
                    results.append(
                        {
                            "application_id": row.id,
                            "revision": meta.revision,
                            "application": serialize_application(row),
                        }
                    )
                session.flush()
                response = {
                    "approved": True,
                    "expected_revision": payload.expected_revision,
                    "from_revision": start_revision,
                    "to_revision": meta.revision,
                    # Headline final revision, same field name as single
                    # approve/revoke responses.
                    "revision": meta.revision,
                    "guard_band": guard_band,
                    "application_ids": payload.application_ids,
                    "results": results,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests) still have the
            # unique revision constraint: a concurrent writer won, so the
            # whole batch is rejected and nothing was partially approved.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the whole batch was rejected",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Revoke (only affects the effective set used by later adjudications)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/revoke", tags=["applications"])
def revoke_application(
    application_id: int, payload: RevocationRequest
) -> dict[str, Any]:
    """Permanently revoke a license.

    Works on both effective licenses and licenses temporarily suspended for
    an equipment outage; a revoked license can never be resumed.  Only the
    effective set used by later adjudications changes; history is kept.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} is already revoked",
                        status_code=409,
                    )
                if row.status not in ("approved", "suspended"):
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is {row.status} and "
                        "cannot be revoked",
                        status_code=409,
                        details={"status": row.status},
                    )

                was_suspended = row.status == "suspended"
                meta.revision += 1
                release_revision = meta.revision
                session.add(
                    RevocationEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        reason=payload.reason,
                    )
                )
                row.status = "revoked"

                # A released license may free spectrum: re-adjudicate the
                # standby queue in registration order, in this same
                # transaction and revision sequence.  The revoke keeps its
                # one revision; each automatic approval consumes the next.
                waitlist_result = drain_waitlist(
                    session, meta, release_revision
                )
                session.flush()
                response = {
                    "revoked": True,
                    "application_id": application_id,
                    # The revoke's own revision (compatibility field).
                    "revision": release_revision,
                    "previous_approval_revision": row.approval_revision,
                    "was_suspended": was_suspended,
                    "reason": payload.reason,
                    **waitlist_result,
                }
        except IntegrityError:
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); retry",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Suspend / resume (temporary equipment outage, revision-arbitrated)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/suspend", tags=["applications"])
def suspend_application(
    application_id: int, payload: SuspensionRequest
) -> dict[str, Any]:
    """Temporarily take an approved license out of the effective set.

    The original approval is kept and no re-application is needed; the
    license immediately stops participating in later adjudications.  Shares
    the singleton revision lock with every other writer: a stale expected
    revision returns the current revision and consumes nothing.  Pending
    applications must be approved first; a suspended application must not
    be suspended again; revoked licenses are terminal.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as approve/batch/revoke/guard-band
                # change: all writers share one revision arbitration.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked and "
                        "cannot be suspended",
                        status_code=409,
                    )
                if row.status == "suspended":
                    raise APIError(
                        E_ALREADY_SUSPENDED,
                        f"application {application_id} is already suspended "
                        f"since revision {row.suspended_revision}",
                        status_code=409,
                        details={
                            "suspended_at_revision": row.suspended_revision
                        },
                    )
                if row.status != "approved":
                    raise APIError(
                        E_NOT_APPROVED,
                        f"application {application_id} is {row.status}; only "
                        "approved licenses can be suspended",
                        status_code=409,
                        details={"status": row.status},
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-decide; the "
                        "license was not suspended",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                meta.revision = current + 1
                release_revision = meta.revision
                session.add(
                    SuspensionEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        reason=payload.reason,
                    )
                )
                row.status = "suspended"
                row.suspended_revision = meta.revision

                # The license just left the effective set: re-adjudicate the
                # standby queue in registration order within the same
                # transaction and revision sequence.  The suspension keeps
                # its one revision; every automatic approval consumes the
                # next consecutive one.
                waitlist_result = drain_waitlist(
                    session, meta, release_revision
                )
                session.flush()
                response = {
                    "suspended": True,
                    "application_id": application_id,
                    # The suspension's own revision (compatibility field).
                    "revision": release_revision,
                    "previous_approval_revision": row.approval_revision,
                    "reason": payload.reason,
                    "application": serialize_application(row),
                    **waitlist_result,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-decide; the license was not suspended",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


@app.post("/api/applications/{application_id}/resume", tags=["applications"])
def resume_application(
    application_id: int, payload: ResumptionRequest
) -> dict[str, Any]:
    """Restore a suspended license after re-adjudication.

    The license is re-checked under the revision lock against the live guard
    band and every currently effective license.  A stale expected revision
    returns the current revision and a renewed interference returns the
    conflicting licenses; in both cases the license stays suspended and no
    revision is consumed.  A successful resume consumes exactly one
    revision; the original approval event is left untouched.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as every other writer.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked and "
                        "cannot be resumed",
                        status_code=409,
                    )
                if row.status != "suspended":
                    raise APIError(
                        E_NOT_SUSPENDED,
                        f"application {application_id} is {row.status}; only "
                        "suspended licenses can be resumed",
                        status_code=409,
                        details={"status": row.status},
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-decide; the "
                        "license stays suspended",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock against the latest effective
                # set and the live guard band.  The resuming license itself
                # is still suspended, so it is not part of that set.
                effective = load_effective_licenses(session)
                conflicts = adjudication.evaluate(
                    to_license(row), effective, guard_band
                )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"application {application_id} would interfere with "
                        f"{len(conflicts)} effective grant(s); the license "
                        "stays suspended and no revision was consumed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )

                meta.revision = current + 1
                session.add(
                    ResumptionEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        guard_band=guard_band,
                        reason=payload.reason,
                    )
                )
                row.status = "approved"
                row.suspended_revision = None
                session.flush()
                response = {
                    "resumed": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "original_approval_revision": row.approval_revision,
                    "guard_band": guard_band,
                    "reason": payload.reason,
                    "application": serialize_application(row),
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; the license stays suspended.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-decide; the license stays suspended",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Waitlist (standby queue; registration/cancellation consume no revision)
# --------------------------------------------------------------------------- #
@app.post(
    "/api/applications/{application_id}/waitlist",
    tags=["waitlist"],
)
def register_waitlist(
    application_id: int,
    payload: WaitlistRegistrationRequest | None = None,
) -> dict[str, Any]:
    """Register a pending application as a standby for the busy band.

    The application must exist and still be ``pending``; each application can
    be queued at most once.  Registration consumes **no** revision and never
    changes the application's status; the response gives the FIFO queue
    position (1-based) and the current revision.  When a later suspension or
    revoke releases an effective license, the queue is re-adjudicated in
    registration order inside that transaction; a manually approved standby
    leaves the queue first.
    """
    reason = payload.reason if payload is not None else ""
    with SessionLocal() as session:
        try:
            with session.begin():
                # Take the singleton lock so a concurrent suspend/revoke
                # drain cannot race the registration; registration itself
                # does not touch the revision number.
                session.get(RevisionMeta, 1, with_for_update=True)

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                existing = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.application_id == application_id)
                    .one_or_none()
                )
                if existing is not None:
                    raise APIError(
                        E_ALREADY_WAITLISTED,
                        f"application {application_id} is already registered in "
                        "the waitlist; the same application cannot be "
                        "registered twice",
                        status_code=409,
                        details={
                            "position": load_waitlist(session).index(existing)
                            + 1
                        },
                    )
                if row.status != "pending":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is {row.status}; only "
                        "pending applications can be registered in the waitlist",
                        status_code=409,
                        details={"status": row.status},
                    )

                entry = WaitlistEntry(
                    application_id=application_id, reason=reason
                )
                session.add(entry)
                session.flush()
                current = session.get(RevisionMeta, 1).revision
                # Position equals the insertion sequence: every earlier entry
                # has a smaller id, and the lock guarantees no concurrent
                # insert.
                position = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.id <= entry.id)
                    .count()
                )
                response = {
                    "waitlisted": True,
                    "application_id": application_id,
                    "queue_position": position,
                    "current_revision": current,
                    "reason": reason,
                    "registered_at": iso(entry.created_at),
                }
        except IntegrityError:
            # Backends without row locks (SQLite): a concurrent registration
            # of the same application won the unique constraint.
            session.rollback()
            raise APIError(
                E_ALREADY_WAITLISTED,
                f"application {application_id} is already registered in the "
                "waitlist; the same application cannot be registered twice",
                status_code=409,
            )
    return response


@app.post(
    "/api/applications/{application_id}/waitlist/cancel",
    tags=["waitlist"],
)
def cancel_waitlist(application_id: int) -> dict[str, Any]:
    """Withdraw a pending application from the standby queue.

    Cancellation consumes no revision and does not change the application's
    status (it stays ``pending``).  Unknown applications are 404; applications
    not currently queued are 409 ``not_waitlisted``.
    """
    with SessionLocal() as session, session.begin():
        session.get(RevisionMeta, 1, with_for_update=True)

        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        entry = (
            session.query(WaitlistEntry)
            .filter(WaitlistEntry.application_id == application_id)
            .one_or_none()
        )
        if entry is None:
            raise APIError(
                E_NOT_WAITLISTED,
                f"application {application_id} is not registered in the "
                "waitlist",
                status_code=409,
                details={"status": row.status},
            )
        session.delete(entry)
        session.flush()
        current = session.get(RevisionMeta, 1).revision
        response = {
            "cancelled": True,
            "application_id": application_id,
            "current_revision": current,
            "application": serialize_application(row),
        }
    return response


@app.get("/api/waitlist", tags=["waitlist"])
def list_waitlist() -> dict[str, Any]:
    """The whole standby queue in registration (FIFO) order."""
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        entries = load_waitlist(session)
        return {
            "current_revision": meta.revision,
            "queue_length": len(entries),
            "waitlist_application_ids": [e.application_id for e in entries],
            "entries": [
                serialize_waitlist_entry(session, entry, position)
                for position, entry in enumerate(entries, start=1)
            ],
        }


@app.get(
    "/api/applications/{application_id}/waitlist",
    tags=["waitlist"],
)
def get_waitlist_entry(application_id: int) -> dict[str, Any]:
    """Look up one application's standby position (404 when not queued)."""
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        entries = load_waitlist(session)
        for position, entry in enumerate(entries, start=1):
            if entry.application_id == application_id:
                meta = session.get(RevisionMeta, 1)
                body = serialize_waitlist_entry(session, entry, position)
                body["current_revision"] = meta.revision
                return body
        raise APIError(
            E_NOT_WAITLISTED,
            f"application {application_id} is not registered in the waitlist",
            status_code=404,
            details={"status": row.status},
        )


# --------------------------------------------------------------------------- #
# Guard-band configuration (online, revision-arbitrated)
# --------------------------------------------------------------------------- #
@app.get("/api/config/guard-band", tags=["config"])
def get_guard_band() -> dict[str, Any]:
    """Live guard band, the initial seeded value and the current revision."""
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        seed = (
            session.query(GuardBandEvent)
            .filter(GuardBandEvent.revision == 0)
            .first()
        )
        guard_band = meta.guard_band if meta else settings.guard_band
        return {
            "current_revision": meta.revision if meta else 0,
            "guard_band": guard_band,
            "initial_guard_band": seed.guard_band if seed else guard_band,
        }


@app.post("/api/config/guard-band/preview", tags=["config"])
def preview_guard_band(payload: GuardBandPreviewRequest) -> dict[str, Any]:
    """Dry-run a proposed guard band against the current effective set.

    Returns the current revision (to be carried into the confirm call as
    ``expected_revision``) and every pair of currently effective licenses
    that would interfere under the proposed band.  Never mutates state.
    """
    errors = validate_guard_band(payload.guard_band)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the guard band preview was rejected due to an invalid value",
            status_code=422,
            details=errors,
        )
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        effective = load_effective_licenses(session)
        pairs = adjudication.find_interfering_pairs(
            effective, payload.guard_band
        )
        return {
            "current_revision": meta.revision,
            "current_guard_band": meta.guard_band,
            "proposed_guard_band": payload.guard_band,
            "effective_application_ids": [
                lic.application_id for lic in effective
            ],
            "has_conflicts": bool(pairs),
            "interfering_pair_count": len(pairs),
            "interfering_pairs": [
                serialize_pair_conflict(p, payload.guard_band) for p in pairs
            ],
        }


@app.post("/api/config/guard-band", tags=["config"])
def update_guard_band(payload: GuardBandUpdateRequest) -> dict[str, Any]:
    """Confirm a new guard band (conditional on the expected revision).

    Shares the singleton revision lock with single/batch approvals and
    revocations.  The change is refused when the revision is stale or when
    the proposed band would make any pair of currently effective licenses
    interfere; in both cases the band, the effective set and the revision
    are left untouched.  On success exactly one revision is consumed and
    every later adjudication uses the new band.
    """
    errors = validate_guard_band(payload.guard_band)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the guard band change was rejected due to an invalid value",
            status_code=422,
            details=errors,
        )
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as approve/batch/revoke: one revision
                # arbitration for every writer.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the guard band was not changed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-check under the lock: the new band must not make any
                # pair of currently effective licenses interfere.
                effective = load_effective_licenses(session)
                pairs = adjudication.find_interfering_pairs(
                    effective, payload.guard_band
                )
                if pairs:
                    raise APIError(
                        E_INTERFERENCE,
                        f"proposed guard band {payload.guard_band} would make "
                        f"{len(pairs)} effective license pair(s) interfere; "
                        "the guard band was not changed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "current_guard_band": meta.guard_band,
                            "proposed_guard_band": payload.guard_band,
                            "interfering_pairs": [
                                serialize_pair_conflict(p, payload.guard_band)
                                for p in pairs
                            ],
                        },
                    )

                previous = meta.guard_band
                meta.revision = current + 1
                meta.guard_band = payload.guard_band
                session.add(
                    GuardBandEvent(
                        revision=meta.revision,
                        previous_guard_band=previous,
                        guard_band=payload.guard_band,
                    )
                )
                session.flush()
                response = {
                    "updated": True,
                    "revision": meta.revision,
                    "previous_guard_band": previous,
                    "guard_band": payload.guard_band,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the guard band was not changed",
                status_code=409,
                details={"current_revision": latest},
            )
    return response
