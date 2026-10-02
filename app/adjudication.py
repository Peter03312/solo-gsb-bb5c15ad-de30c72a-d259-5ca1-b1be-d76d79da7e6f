"""Pure adjudication rules for radio applications.

An application grants:

* a coverage **circle** in planar coordinates ``(cx, cy, r)``;
* a **closed** frequency interval ``[freq_low, freq_high]``;
* a **half-open** UTC time interval ``[start_time, end_time)``.

Two *effective* (approved and not later revoked) applications interfere iff
ALL of the following hold:

1. the time intervals intersect;
2. the coverage circles intersect;
3. the closed frequency bands intersect, OR the minimum gap between them is
   strictly smaller than the non-negative guard band.

The module is free of framework / database code so the rules can be unit
tested directly.  ``available_ranges`` inverts the same rules: given a
candidate whose band width is fixed, it computes every maximal interval of
band lower bounds that would preview conflict-free.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

# Conflict reason codes (also documented in the README).
REASON_FREQUENCY_INTERSECTION = "frequency_intersection"
REASON_WITHIN_GUARD_BAND = "within_guard_band"


@dataclass(frozen=True)
class Circle:
    cx: float
    cy: float
    radius: float


@dataclass(frozen=True)
class License:
    """An effective approved application, as seen by the adjudicator."""

    application_id: int
    circle: Circle
    freq_low: float
    freq_high: float
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class Conflict:
    other: License
    time_overlap: bool
    area_overlap: bool
    frequency_intersects: bool
    minimum_frequency_gap: float
    reason: str  # REASON_*; only set when all three trigger conditions hold


@dataclass(frozen=True)
class AvailableRange:
    """One maximal continuous range of attainable candidate lower bounds.

    ``min``/``max`` bound the candidate band's *lower* bound ``x``; the band
    itself is ``[x, x + width]`` with the width fixed by the application.
    ``min_inclusive``/``max_inclusive`` tell whether the endpoint itself is
    attainable.  A range may degenerate to a single attainable point
    (``min == max``, both inclusive).
    """

    min: float
    max: float
    min_inclusive: bool
    max_inclusive: bool


@dataclass(frozen=True)
class PairConflict:
    """An interfering pair inside a set of licenses (symmetric view).

    Used when a whole set is checked against itself, e.g. previewing a
    proposed guard band against the current effective set.
    """

    first: License
    second: License
    time_overlap: bool
    area_overlap: bool
    frequency_intersects: bool
    minimum_frequency_gap: float
    reason: str  # REASON_*


def circles_intersect(a: Circle, b: Circle) -> bool:
    """Planar disks intersect iff centre distance <= sum of radii.

    Touching at one point (distance exactly equal to the sum) counts as an
    intersection.
    """
    distance = math.hypot(a.cx - b.cx, a.cy - b.cy)
    return distance <= a.radius + b.radius


def closed_intervals_intersect(
    low_a: float, high_a: float, low_b: float, high_b: float
) -> bool:
    """Closed intervals: touching endpoints intersect (a1 <= b0 and b1 <= a0)."""
    return low_a <= high_b and low_b <= high_a


def half_open_intervals_intersect(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> bool:
    """[start_a, end_a) vs [start_b, end_b): end == start does NOT overlap."""
    return start_a < end_b and start_b < end_a


def minimum_frequency_gap(
    low_a: float, high_a: float, low_b: float, high_b: float
) -> float:
    """0 when the closed bands intersect, otherwise the gap between them."""
    if closed_intervals_intersect(low_a, high_a, low_b, high_b):
        return 0.0
    return low_b - high_a if high_a < low_b else low_a - high_b


def frequency_conflicts(
    low_a: float,
    high_a: float,
    low_b: float,
    high_b: float,
    guard_band: float,
) -> tuple[bool, bool, float, str | None]:
    """Return (conflicts, intersects, minimum_gap, reason).

    ``guard_band`` must be non-negative.  A gap exactly equal to the guard
    band is allowed ("strictly smaller than" triggers the conflict).
    """
    if closed_intervals_intersect(low_a, high_a, low_b, high_b):
        return True, True, 0.0, REASON_FREQUENCY_INTERSECTION
    gap = minimum_frequency_gap(low_a, high_a, low_b, high_b)
    if gap < guard_band:
        return True, False, gap, REASON_WITHIN_GUARD_BAND
    return False, False, gap, None


def _pair_trigger(
    a: License, b: License, guard_band: float
) -> tuple[bool, float, str] | None:
    """Shared interference gate for one pair of licenses.

    Returns ``(frequency_intersects, minimum_gap, reason)`` when the pair
    interferes under ``guard_band``, else ``None``.  Time and area
    intersection are necessary conditions; frequency overlap (or a
    guard-band violation) is the third.
    """
    t_overlap = half_open_intervals_intersect(
        a.start_time, a.end_time, b.start_time, b.end_time
    )
    a_overlap = circles_intersect(a.circle, b.circle)
    if not (t_overlap and a_overlap):
        return None
    freq_conflicts, freq_intersects, gap, reason = frequency_conflicts(
        a.freq_low, a.freq_high, b.freq_low, b.freq_high, guard_band
    )
    if not freq_conflicts:
        return None
    return freq_intersects, gap, (reason or REASON_FREQUENCY_INTERSECTION)


def evaluate(
    candidate: License, effective: list[License], guard_band: float
) -> list[Conflict]:
    """All interference conflicts between ``candidate`` and effective grants.

    Every checked grant is returned with its trigger flags so callers can
    preview exactly which conditions fired.
    """
    conflicts: list[Conflict] = []
    for other in effective:
        trigger = _pair_trigger(candidate, other, guard_band)
        if trigger is None:
            continue
        freq_intersects, gap, reason = trigger
        conflicts.append(
            Conflict(
                other=other,
                time_overlap=True,
                area_overlap=True,
                frequency_intersects=freq_intersects,
                minimum_frequency_gap=gap,
                reason=reason,
            )
        )
    return conflicts


def find_interfering_pairs(
    licenses: list[License], guard_band: float
) -> list[PairConflict]:
    """Every interfering pair *within* ``licenses`` under ``guard_band``.

    Used to (pre-)check a proposed guard band against the current effective
    set: the change may only be confirmed when this returns an empty list.
    Each interfering combination is reported once, in input order.
    """
    pairs: list[PairConflict] = []
    for i in range(len(licenses)):
        for j in range(i + 1, len(licenses)):
            trigger = _pair_trigger(licenses[i], licenses[j], guard_band)
            if trigger is None:
                continue
            freq_intersects, gap, reason = trigger
            pairs.append(
                PairConflict(
                    first=licenses[i],
                    second=licenses[j],
                    time_overlap=True,
                    area_overlap=True,
                    frequency_intersects=freq_intersects,
                    minimum_frequency_gap=gap,
                    reason=reason,
                )
            )
    return pairs


def evaluate_group(
    candidates: list[License], effective: list[License], guard_band: float
) -> tuple[dict[int, list[Conflict]], dict[int, list[Conflict]]]:
    """Adjudicate an ordered group as one atomic decision.

    Returns ``(against_effective, within_group)``, each keyed by the
    candidate's ``application_id`` (every candidate gets a key, possibly
    mapped to an empty list):

    * ``against_effective[id]`` - clashes with the currently effective grants,
      in effective-set order;
    * ``within_group[id]``      - clashes with other members of this group.

    Interference is symmetric, so an intra-group clash between two members is
    reported on *both* members (each listing the other as the conflict
    object).  Input order determines only the order in which a successful
    group consumes its consecutive revisions; it never hides a clash.
    """
    against_effective: dict[int, list[Conflict]] = {
        candidate.application_id: [] for candidate in candidates
    }
    within_group: dict[int, list[Conflict]] = {
        candidate.application_id: [] for candidate in candidates
    }

    for candidate in candidates:
        against_effective[candidate.application_id].extend(
            evaluate(candidate, effective, guard_band)
        )

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            # evaluate() with a singleton other returns 0 or 1 conflict;
            # run both directions so each side gets itself as the subject.
            forward = evaluate(candidates[i], [candidates[j]], guard_band)
            if not forward:
                continue
            backward = evaluate(candidates[j], [candidates[i]], guard_band)
            within_group[candidates[i].application_id].append(forward[0])
            within_group[candidates[j].application_id].append(backward[0])

    return against_effective, within_group


def available_ranges(
    candidate: License,
    effective: list[License],
    guard_band: float,
    low_min: float,
    low_max: float,
) -> list[AvailableRange]:
    """Maximal lower-bound ranges in ``[low_min, low_max]`` free of conflicts.

    The candidate keeps its circle, UTC time window and band *width*
    (``freq_high - freq_low``, possibly zero for a single frequency); only
    the lower bound ``x`` of its band varies over the closed candidate
    interval ``[low_min, low_max]``.

    A grant intersecting the candidate in time *and* area forbids exactly
    the lower bounds ``(freq_low - width - guard_band, freq_high +
    guard_band)``: inside it the shifted bands intersect or sit strictly
    within the guard band.  With a zero guard band the endpoints stay
    forbidden (touching closed bands interfere), so the forbidden interval
    is closed; with a positive guard band a gap exactly equal to the band
    is allowed, so it is open.  Grants disjoint in time or area forbid
    nothing.

    The result is the complement of the merged forbidden intervals inside
    ``[low_min, low_max]``: every maximal continuous available range, in
    ascending order, each endpoint flagged attainable or not; empty when no
    lower bound in the candidate interval is usable.  Consistent with
    :func:`evaluate`: a lower bound is attainable exactly when the shifted
    candidate would preview with no conflicts.  Attainable endpoints are
    nudged by ulps until they pass the very frequency predicate
    :func:`evaluate` applies, so a preview at a reported endpoint agrees
    with the range even under floating-point rounding.
    """
    width = candidate.freq_high - candidate.freq_low
    # Endpoints of a forbidden interval are forbidden themselves iff the
    # guard band is zero (endpoint touch counts as interference).
    forbidden_closed = guard_band == 0
    intervals: list[tuple[float, float]] = []
    for other in effective:
        if not half_open_intervals_intersect(
            candidate.start_time,
            candidate.end_time,
            other.start_time,
            other.end_time,
        ):
            continue
        if not circles_intersect(candidate.circle, other.circle):
            continue
        start = other.freq_low - width - guard_band
        end = other.freq_high + guard_band
        if not forbidden_closed:
            # The endpoints are reported as attainable (a gap exactly equal
            # to the guard band is allowed).  Rounding may leave the closed
            # form an ulp inside the forbidden zone, so nudge outward until
            # the shifted band passes evaluate()'s frequency predicate.
            while frequency_conflicts(
                start, start + width,
                other.freq_low, other.freq_high, guard_band,
            )[0]:
                start = math.nextafter(start, -math.inf)
            while frequency_conflicts(
                end, end + width,
                other.freq_low, other.freq_high, guard_band,
            )[0]:
                end = math.nextafter(end, math.inf)
        intervals.append((start, end))
    intervals.sort()

    # Merge connected forbidden intervals.  Closed intervals (zero guard
    # band) connect when they touch at an endpoint; open ones leave a
    # shared endpoint available and stay separate.
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged:
            last = merged[-1]
            if start < last[1] or (start == last[1] and forbidden_closed):
                last[1] = max(last[1], end)
                continue
        merged.append([start, end])

    # Complement of the merged intervals inside [low_min, low_max],
    # tracking endpoint attainability.
    ranges: list[AvailableRange] = []

    def emit(lo: float, hi: float, lo_incl: bool, hi_incl: bool) -> None:
        if lo < hi or (lo == hi and lo_incl and hi_incl):
            ranges.append(AvailableRange(lo, hi, lo_incl, hi_incl))

    cursor = low_min
    cursor_inclusive = True  # the candidate interval is closed at low_min
    for start, end in merged:
        if end < low_min or start > low_max:
            continue
        if cursor <= start:
            emit(cursor, start, cursor_inclusive, not forbidden_closed)
        if end >= cursor:
            cursor = end
            cursor_inclusive = not forbidden_closed
    if cursor <= low_max:
        emit(cursor, low_max, cursor_inclusive, True)
    return ranges
