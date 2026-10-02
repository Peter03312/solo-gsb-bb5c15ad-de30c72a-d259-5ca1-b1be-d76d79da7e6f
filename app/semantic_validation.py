"""Whole-request semantic validation.

Any failure rejects the entire submission; the list contains one entry per
violated rule.
"""
from __future__ import annotations

import math

from .schemas import ApplicationCreate, AvailableRangesRequest

RADIUS_NOT_POSITIVE = "radius_not_positive"
FREQUENCY_LOW_ABOVE_HIGH = "frequency_low_above_high"
TIME_START_NOT_BEFORE_END = "time_start_not_before_end"
VALUE_NOT_FINITE = "value_not_finite"
NAIVE_DATETIME = "naive_datetime"
GUARD_BAND_NEGATIVE = "guard_band_negative"
CANDIDATE_LOW_MIN_ABOVE_MAX = "candidate_low_min_above_max"


def validate_application(payload: ApplicationCreate) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []

    numbers = {
        "circle.cx": payload.circle.cx,
        "circle.cy": payload.circle.cy,
        "circle.radius": payload.circle.radius,
        "frequency.low": payload.frequency.low,
        "frequency.high": payload.frequency.high,
    }
    for field, value in numbers.items():
        if not math.isfinite(value):
            errors.append(
                {
                    "field": field,
                    "code": VALUE_NOT_FINITE,
                    "message": f"{field} must be a finite number",
                }
            )

    if payload.circle.radius <= 0:
        errors.append(
            {
                "field": "circle.radius",
                "code": RADIUS_NOT_POSITIVE,
                "message": "radius must be strictly positive",
            }
        )

    if payload.frequency.low > payload.frequency.high:
        errors.append(
            {
                "field": "frequency.low",
                "code": FREQUENCY_LOW_ABOVE_HIGH,
                "message": "frequency low bound must not exceed high bound",
            }
        )

    start = payload.time_window.start
    end = payload.time_window.end
    if start.tzinfo is None or end.tzinfo is None:
        errors.append(
            {
                "field": "time_window",
                "code": NAIVE_DATETIME,
                "message": "time_window.start and end must carry a timezone (use UTC, e.g. ...Z)",
            }
        )
    elif start >= end:
        # Equal instants are also rejected: start must be strictly earlier.
        errors.append(
            {
                "field": "time_window.start",
                "code": TIME_START_NOT_BEFORE_END,
                "message": "time_window start must be strictly earlier than end",
            }
        )

    return errors


def validate_guard_band(value: float) -> list[dict[str, str]]:
    """A guard band must be a finite, non-negative number."""
    errors: list[dict[str, str]] = []
    if not math.isfinite(value):
        errors.append(
            {
                "field": "guard_band",
                "code": VALUE_NOT_FINITE,
                "message": "guard_band must be a finite number",
            }
        )
    elif value < 0:
        errors.append(
            {
                "field": "guard_band",
                "code": GUARD_BAND_NEGATIVE,
                "message": "guard_band must be non-negative",
            }
        )
    return errors


def validate_candidate_low_range(
    payload: AvailableRangesRequest,
) -> list[dict[str, str]]:
    """Candidate lower-bound interval: finite numbers, min <= max.

    Equal bounds are allowed (a single candidate position); only an
    inverted interval is rejected.
    """
    errors: list[dict[str, str]] = []
    numbers = {
        "candidate_low_min": payload.candidate_low_min,
        "candidate_low_max": payload.candidate_low_max,
    }
    for field, value in numbers.items():
        if not math.isfinite(value):
            errors.append(
                {
                    "field": field,
                    "code": VALUE_NOT_FINITE,
                    "message": f"{field} must be a finite number",
                }
            )

    if payload.candidate_low_min > payload.candidate_low_max:
        errors.append(
            {
                "field": "candidate_low_min",
                "code": CANDIDATE_LOW_MIN_ABOVE_MAX,
                "message": "candidate_low_min must not exceed candidate_low_max",
            }
        )
    return errors
