from datetime import datetime, timedelta, timezone

from app.adjudication import (
    REASON_FREQUENCY_INTERSECTION,
    REASON_WITHIN_GUARD_BAND,
    Circle,
    License,
    available_ranges,
    circles_intersect,
    closed_intervals_intersect,
    evaluate,
    evaluate_group,
    find_interfering_pairs,
    frequency_conflicts,
    half_open_intervals_intersect,
    minimum_frequency_gap,
)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=2)
T2 = T1
T3 = T0 + timedelta(hours=4)


def lic(
    app_id,
    cx=0.0,
    cy=0.0,
    r=10.0,
    flo=100.0,
    fhi=110.0,
    start=T0,
    end=T1,
):
    return License(
        application_id=app_id,
        circle=Circle(cx, cy, r),
        freq_low=flo,
        freq_high=fhi,
        start_time=start,
        end_time=end,
    )


class TestCircles:
    def test_nested_circles(self):
        assert circles_intersect(Circle(0, 0, 10), Circle(1, 1, 2))

    def test_touching_at_one_point_counts(self):
        # centre distance exactly 10 = radii sum 5+5
        assert circles_intersect(Circle(0, 0, 5), Circle(10, 0, 5))

    def test_disjoint(self):
        assert not circles_intersect(Circle(0, 0, 5), Circle(20, 0, 5))

    def test_two_dimensional_distance(self):
        # 3-4-5 triangle, radii 2+2=4 < 5 -> disjoint
        assert not circles_intersect(Circle(0, 0, 2), Circle(3, 4, 2))


class TestIntervals:
    def test_closed_endpoints_touching_intersect(self):
        assert closed_intervals_intersect(100, 110, 110, 120)
        assert closed_intervals_intersect(110, 120, 100, 110)

    def test_closed_disjoint(self):
        assert not closed_intervals_intersect(100, 109, 110, 120)

    def test_half_open_touching_does_not_overlap(self):
        assert not half_open_intervals_intersect(T0, T1, T2, T3)
        assert half_open_intervals_intersect(T0, T3, T1, T3 + timedelta(hours=1))

    def test_half_open_partial(self):
        assert half_open_intervals_intersect(
            T0, T3, T0 + timedelta(hours=1), T3 + timedelta(hours=1)
        )


class TestFrequency:
    def test_overlapping_bands(self):
        conflicts, intersects, gap, reason = frequency_conflicts(
            100, 110, 105, 115, guard_band=2.0
        )
        assert conflicts and intersects and gap == 0
        assert reason == REASON_FREQUENCY_INTERSECTION

    def test_endpoint_touch_intersects_closed_band(self):
        conflicts, intersects, gap, _ = frequency_conflicts(
            100, 110, 110, 120, guard_band=2.0
        )
        assert conflicts and intersects and gap == 0

    def test_gap_smaller_than_guard(self):
        # gap 1.5 < 2.0
        conflicts, intersects, gap, reason = frequency_conflicts(
            100, 110, 111.5, 120, guard_band=2.0
        )
        assert conflicts and not intersects and gap == 1.5
        assert reason == REASON_WITHIN_GUARD_BAND

    def test_gap_equal_to_guard_is_allowed(self):
        # Strictly smaller: gap == guard band must NOT conflict.
        conflicts, intersects, gap, reason = frequency_conflicts(
            100, 110, 112, 120, guard_band=2.0
        )
        assert not conflicts and not intersects and gap == 2.0 and reason is None

    def test_gap_larger_than_guard(self):
        conflicts, *_ = frequency_conflicts(100, 110, 120, 130, guard_band=2.0)
        assert not conflicts

    def test_zero_guard_band_disjoint_is_ok(self):
        assert minimum_frequency_gap(100, 110, 112, 120) == 2.0
        conflicts, *_ = frequency_conflicts(100, 110, 110.0001, 120, 0.0)
        assert not conflicts


class TestEvaluate:
    def test_requires_time_and_area_and_frequency(self):
        # same place/freq but adjacent (non-overlapping) times -> no conflict
        later = lic(2, start=T1, end=T3)
        assert evaluate(lic(1, end=T1), [later], guard_band=5.0) == []

        # same time/freq but disjoint areas -> no conflict
        far = lic(2, cx=100.0, cy=0.0, r=1.0)
        assert evaluate(lic(1), [far], guard_band=5.0) == []

        # same time/place, separated frequencies beyond guard -> no conflict
        separated = lic(2, flo=200, fhi=210)
        assert evaluate(lic(1), [separated], guard_band=2.0) == []

    def test_full_conflict(self):
        conflicts = evaluate(lic(1), [lic(2, flo=105, fhi=115)], 2.0)
        assert len(conflicts) == 1
        assert conflicts[0].other.application_id == 2
        assert conflicts[0].reason == REASON_FREQUENCY_INTERSECTION

    def test_guard_band_conflict_through_all_gates(self):
        other = lic(2, flo=111, fhi=112)  # gap 1 < guard 2
        (conflict,) = evaluate(lic(1), [other], 2.0)
        assert conflict.reason == REASON_WITHIN_GUARD_BAND
        assert conflict.minimum_frequency_gap == 1.0
        assert conflict.time_overlap and conflict.area_overlap
        assert not conflict.frequency_intersects


class TestEvaluateGroup:
    def test_clean_group_has_no_conflicts_anywhere(self):
        # Disjoint frequencies, well beyond the 2 MHz guard band.
        members = [lic(1, flo=100, fhi=101), lic(2, flo=200, fhi=201)]
        against, within = evaluate_group(members, [lic(9, flo=300, fhi=301)], 2.0)
        assert against == {1: [], 2: []}
        assert within == {1: [], 2: []}

    def test_each_member_checked_against_effective_set(self):
        members = [lic(1, flo=100, fhi=110), lic(2, flo=200, fhi=210)]
        against, within = evaluate_group(members, [lic(9, flo=105, fhi=106)], 2.0)
        assert len(against[1]) == 1
        assert against[1][0].other.application_id == 9
        assert within == {1: [], 2: []}

    def test_intra_group_clash_reported_on_both_sides(self):
        # Two members overlap each other in band but not the effective set.
        members = [lic(1, flo=100, fhi=110), lic(2, flo=105, fhi=115)]
        against, within = evaluate_group(members, [lic(9, flo=300, fhi=301)], 2.0)
        assert against == {1: [], 2: []}
        assert within[1][0].other.application_id == 2
        assert within[2][0].other.application_id == 1
        assert within[1][0].reason == REASON_FREQUENCY_INTERSECTION

    def test_ordering_does_not_hide_intra_group_clash(self):
        # The clash is between members at positions 1 and 3; every candidate
        # still gets a key, and the middle member is clean.
        members = [
            lic(1, flo=100, fhi=110),
            lic(2, flo=200, fhi=210),
            lic(3, flo=105, fhi=106),
        ]
        against, within = evaluate_group(members, [], 2.0)
        assert set(against) == {1, 2, 3}
        assert within[1][0].other.application_id == 3
        assert within[3][0].other.application_id == 1
        assert within[2] == []

    def test_guard_band_applies_within_group(self):
        # gap 1 < guard 2 between two otherwise disjoint members
        members = [lic(1, flo=100, fhi=110), lic(2, flo=111, fhi=112)]
        _, within = evaluate_group(members, [], 2.0)
        assert within[1][0].reason == REASON_WITHIN_GUARD_BAND
        assert within[1][0].minimum_frequency_gap == 1.0


class TestFindInterferingPairs:
    def test_no_pairs_when_clean(self):
        members = [lic(1, flo=100, fhi=110), lic(2, flo=200, fhi=210)]
        assert find_interfering_pairs(members, 2.0) == []

    def test_pair_detected_with_trigger_details(self):
        a, b = lic(1, flo=100, fhi=110), lic(2, flo=111, fhi=112)
        (pair,) = find_interfering_pairs([a, b], 2.0)
        assert pair.first is a and pair.second is b
        assert pair.reason == REASON_WITHIN_GUARD_BAND
        assert pair.minimum_frequency_gap == 1.0
        assert pair.time_overlap and pair.area_overlap
        assert not pair.frequency_intersects

    def test_frequency_intersection_pair(self):
        pairs = find_interfering_pairs(
            [lic(1, flo=100, fhi=110), lic(2, flo=105, fhi=115)], 2.0
        )
        assert len(pairs) == 1
        assert pairs[0].reason == REASON_FREQUENCY_INTERSECTION
        assert pairs[0].frequency_intersects

    def test_pair_requires_time_and_area(self):
        # frequencies overlap but areas are disjoint -> no pair
        assert find_interfering_pairs(
            [lic(1), lic(2, cx=100.0, r=1.0)], 5.0
        ) == []
        # half-open time windows merely touching -> no pair
        assert find_interfering_pairs(
            [lic(1, end=T1), lic(2, start=T1, end=T3)], 5.0
        ) == []

    def test_every_interfering_combination_reported_once_in_order(self):
        members = [
            lic(1, flo=100, fhi=110),
            lic(2, flo=105, fhi=115),
            lic(3, flo=300, fhi=301),
            lic(4, flo=108, fhi=109),
        ]
        pairs = find_interfering_pairs(members, 2.0)
        assert [(p.first.application_id, p.second.application_id) for p in pairs] == [
            (1, 2),
            (1, 4),
            (2, 4),
        ]

    def test_gap_equal_to_proposed_band_is_allowed(self):
        # Strictly-smaller rule: gap 2 == proposed band 2 -> no pair.
        members = [lic(1, flo=100, fhi=110), lic(2, flo=112, fhi=120)]
        assert find_interfering_pairs(members, 2.0) == []
        (pair,) = find_interfering_pairs(members, 2.5)
        assert pair.minimum_frequency_gap == 2.0


def ranges_tuples(ranges):
    return [(r.min, r.max, r.min_inclusive, r.max_inclusive) for r in ranges]


class TestAvailableRanges:
    def test_single_blocking_grant_splits_the_candidate_interval(self):
        # candidate width 5, grant [110,120], guard 2 -> forbidden (103,122)
        ranges = available_ranges(lic(1, flo=100, fhi=105), [lic(9, flo=110, fhi=120)], 2.0, 90, 130)
        assert ranges_tuples(ranges) == [
            (90, 103, True, True),  # gap exactly 2 at 103: allowed
            (122, 130, True, True),  # gap exactly 2 at 122: allowed
        ]

    def test_zero_guard_band_makes_touching_endpoints_unattainable(self):
        # guard 0 -> forbidden [105,120] closed, endpoints excluded
        ranges = available_ranges(lic(1, flo=100, fhi=105), [lic(9, flo=110, fhi=120)], 0.0, 90, 130)
        assert ranges_tuples(ranges) == [
            (90, 105, True, False),
            (120, 130, False, True),
        ]

    def test_single_frequency_candidate(self):
        # width 0 (single frequency): forbidden (108,122) under guard 2
        ranges = available_ranges(lic(1, flo=100, fhi=100), [lic(9, flo=110, fhi=120)], 2.0, 100, 130)
        assert ranges_tuples(ranges) == [
            (100, 108, True, True),
            (122, 130, True, True),
        ]

    def test_grants_disjoint_in_time_or_area_do_not_block(self):
        far = lic(9, cx=1000.0, r=1.0)
        later = lic(10, start=T1, end=T3)
        ranges = available_ranges(lic(1), [far, later], 2.0, 100, 130)
        assert ranges_tuples(ranges) == [(100, 130, True, True)]

    def test_overlapping_forbidden_intervals_merge(self):
        grants = [lic(9, flo=110, fhi=120), lic(10, flo=115, fhi=125)]
        ranges = available_ranges(lic(1, flo=100, fhi=100), grants, 2.0, 100, 140)
        assert ranges_tuples(ranges) == [
            (100, 108, True, True),
            (127, 140, True, True),
        ]

    def test_touching_forbidden_intervals_leave_a_single_point(self):
        # forbidden (108,122) and (122,136): only 122 sits exactly at gap 2
        # from both grants, a degenerate both-inclusive range.
        grants = [lic(9, flo=110, fhi=120), lic(10, flo=124, fhi=134)]
        ranges = available_ranges(lic(1, flo=100, fhi=100), grants, 2.0, 100, 140)
        assert ranges_tuples(ranges) == [
            (100, 108, True, True),
            (122, 122, True, True),
            (136, 140, True, True),
        ]

    def test_fully_covered_candidate_interval_returns_empty(self):
        ranges = available_ranges(lic(1, flo=100, fhi=100), [lic(9, flo=110, fhi=120)], 2.0, 110, 115)
        assert ranges == []

    def test_degenerate_candidate_interval(self):
        # a single candidate position, available ...
        ranges = available_ranges(lic(1, flo=100, fhi=100), [lic(9, flo=110, fhi=120)], 2.0, 100, 100)
        assert ranges_tuples(ranges) == [(100, 100, True, True)]
        # ... or forbidden
        assert available_ranges(lic(1, flo=100, fhi=100), [lic(9, flo=110, fhi=120)], 2.0, 115, 115) == []

    def test_candidate_window_clips_forbidden_interval(self):
        # forbidden (108,122) extends beyond the window on both sides
        ranges = available_ranges(lic(1, flo=100, fhi=100), [lic(9, flo=110, fhi=120)], 2.0, 105, 125)
        assert ranges_tuples(ranges) == [
            (105, 108, True, True),
            (122, 125, True, True),
        ]

    def test_reported_points_match_evaluate_verdicts(self):
        # For every reported range endpoint and a dense sweep, evaluate()
        # on the shifted band must agree: attainable <=> conflict-free.
        grants = [
            lic(9, flo=110, fhi=120),
            lic(10, flo=124, fhi=134),
            lic(11, flo=200, fhi=201, cx=1000.0, r=1.0),  # disjoint area
        ]
        candidate = lic(1, flo=0, fhi=5)
        for guard in (0.0, 2.0):
            ranges = available_ranges(candidate, grants, guard, 90, 140)
            points = {90, 140, 103, 107, 122, 129, 136}
            for r in ranges:
                points.update({r.min, r.max})
            for x in sorted(points):
                shifted = lic(1, flo=x, fhi=x + 5)
                conflict_free = not evaluate(shifted, grants, guard)
                attainable = any(
                    r.min <= x <= r.max
                    and (x != r.min or r.min_inclusive)
                    and (x != r.max or r.max_inclusive)
                    for r in ranges
                )
                assert conflict_free == attainable, (guard, x, ranges)
