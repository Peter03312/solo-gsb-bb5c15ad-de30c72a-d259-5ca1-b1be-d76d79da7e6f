"""End-to-end API tests against an in-file SQLite database (see conftest.py).

SQLite ignores ``SELECT ... FOR UPDATE``; the test client serialises
requests in-process, which is sufficient to verify revision logic (two
sequential approvals sharing one stale revision reproduce the concurrent
outcome).
"""
from fastapi.testclient import TestClient  # noqa: F401  (kept for type hints)


def body(
    circle=(0, 0, 10),
    freq=(100, 110),
    start="2026-10-01T08:00:00Z",
    end="2026-10-01T10:00:00Z",
    name="Event",
):
    from factories import make_payload

    return make_payload(circle, freq, start, end, name)


class TestValidation:
    def test_non_positive_radius_rejected(self, client):
        r = client.post("/api/applications", json=body(circle=(0, 0, 0)))
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "radius_not_positive" in codes

    def test_negative_radius_rejected(self, client):
        r = client.post("/api/applications", json=body(circle=(0, 0, -3)))
        assert r.status_code == 422

    def test_inverted_band_rejected(self, client):
        r = client.post("/api/applications", json=body(freq=(120, 100)))
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "frequency_low_above_high" in codes

    def test_empty_time_window_rejected(self, client):
        r = client.post(
            "/api/applications",
            json=body(
                start="2026-10-01T08:00:00Z", end="2026-10-01T08:00:00Z"
            ),
        )
        assert r.status_code == 422
        codes = [d["code"] for d in r.json()["error"]["details"]]
        assert "time_start_not_before_end" in codes

    def test_multiple_violations_all_reported(self, client):
        r = client.post(
            "/api/applications",
            json=body(
                circle=(0, 0, -1),
                freq=(200, 100),
                start="2026-10-01T09:00:00Z",
                end="2026-10-01T08:00:00Z",
            ),
        )
        assert r.status_code == 422
        codes = {d["code"] for d in r.json()["error"]["details"]}
        assert {
            "radius_not_positive",
            "frequency_low_above_high",
            "time_start_not_before_end",
        } <= codes

    def test_naive_timestamp_rejected(self, client):
        r = client.post(
            "/api/applications",
            json=body(start="2026-10-01T08:00:00", end="2026-10-01T10:00:00Z"),
        )
        assert r.status_code == 422

    def test_boundary_values_accepted(self, client):
        # single-frequency closed band [100,100] is legal
        r = client.post("/api/applications", json=body(freq=(100, 100)))
        assert r.status_code == 201, r.text


class TestFlow:
    def test_full_happy_path_and_history(self, client):
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/revision/current").json()["current_revision"] == 0

        # submit A and approve it at revision 1
        a = client.post("/api/applications", json=body(name="A")).json()
        prev = client.post(f"/api/applications/{a['id']}/preview").json()
        assert prev["current_revision"] == 0
        assert prev["has_conflicts"] is False

        ok = client.post(
            f"/api/applications/{a['id']}/approve",
            json={"expected_revision": 0},
        ).json()
        assert ok["approved"] is True and ok["revision"] == 1

        # B overlaps A in time/place/frequency
        b = client.post("/api/applications", json=body(name="B")).json()
        prev_b = client.post(f"/api/applications/{b['id']}/preview").json()
        assert prev_b["current_revision"] == 1
        assert prev_b["has_conflicts"] is True
        conflict = prev_b["conflicts"][0]
        assert conflict["application"]["id"] == a["id"]
        assert conflict["reason"] == "frequency_intersection"
        assert conflict["trigger"] == {
            "time_intersect": True,
            "area_intersect": True,
            "frequency_intersect": True,
            "minimum_frequency_gap": 0.0,
            "within_guard_band": True,
        }

        # stale revision is refused
        stale = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_revision"

        # current revision with interference is refused
        blocked = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 1},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "interference"
        assert blocked.json()["error"]["details"]["conflicts"][0]["application"][
            "id"
        ] == a["id"]

        # as-of revision 0 has no events; revision 1 shows A's approval
        snap0 = client.get("/api/revision/0").json()
        assert snap0["approvals"] == [] and snap0["revocations"] == []
        snap1 = client.get("/api/revision/1").json()
        assert len(snap1["approvals"]) == 1
        assert snap1["effective_application_ids"] == [a["id"]]

        # revoke A at revision 2; history at revision 1 is unchanged
        rev = client.post(
            f"/api/applications/{a['id']}/revoke", json={"reason": "event moved"}
        ).json()
        assert rev["revision"] == 2
        snap1_again = client.get("/api/revision/1").json()
        assert snap1_again["effective_application_ids"] == [a["id"]]

        # B now sees an empty effective set and can be approved (revision 3)
        prev_b2 = client.post(f"/api/applications/{b['id']}/preview").json()
        assert prev_b2["current_revision"] == 2
        assert prev_b2["has_conflicts"] is False
        ok_b = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 2},
        ).json()
        assert ok_b["revision"] == 3

        # snapshot at 2 records both the approval and the revocation
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == []
        assert [r["application_id"] for r in snap2["revocations"]] == [a["id"]]
        assert snap2["approvals"][0]["revoked_at_revision"] == 2

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [b["id"]]

        # cannot approve twice / revoke twice
        again = client.post(
            f"/api/applications/{b['id']}/approve",
            json={"expected_revision": 3},
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "not_pending"
        rev_again = client.post(f"/api/applications/{a['id']}/revoke", json={})
        assert rev_again.status_code == 409
        assert rev_again.json()["error"]["code"] == "already_revoked"

    def test_guard_band_rule_and_touching_time(self, client):
        a = client.post(
            "/api/applications", json=body(name="A", freq=(100, 110))
        ).json()
        client.post(
            f"/api/applications/{a['id']}/approve",
            json={"expected_revision": 0},
        ).raise_for_status()

        # gap 1.0 < guard 2.0 -> conflict even though closed bands are disjoint
        near = client.post(
            "/api/applications", json=body(name="near", freq=(111, 112))
        ).json()
        r = client.post(f"/api/applications/{near['id']}/preview").json()
        assert r["has_conflicts"] is True
        c = r["conflicts"][0]
        assert c["reason"] == "within_guard_band"
        assert c["trigger"]["frequency_intersect"] is False
        assert c["trigger"]["minimum_frequency_gap"] == 1.0

        # gap exactly 2.0 -> allowed; move its window adjacent to A's end
        safe = client.post(
            "/api/applications",
            json=body(
                name="safe",
                freq=(112, 120),
                start="2026-10-01T10:00:00Z",  # == A end, half-open => no overlap
                end="2026-10-01T12:00:00Z",
            ),
        ).json()
        prev = client.post(f"/api/applications/{safe['id']}/preview").json()
        assert prev["has_conflicts"] is False
        approved = client.post(
            f"/api/applications/{safe['id']}/approve",
            json={"expected_revision": 1},
        )
        assert approved.status_code == 200, approved.text

    def test_frequency_endpoint_touch_is_conflict(self, client):
        a = client.post(
            "/api/applications", json=body(freq=(100, 110))
        ).json()
        client.post(
            f"/api/applications/{a['id']}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        b = client.post(
            "/api/applications", json=body(freq=(110, 120))
        ).json()
        r = client.post(f"/api/applications/{b['id']}/preview").json()
        assert r["has_conflicts"] is True
        assert r["conflicts"][0]["reason"] == "frequency_intersection"

    def test_disjoint_area_no_conflict(self, client):
        a = client.post(
            "/api/applications", json=body(circle=(0, 0, 5))
        ).json()
        client.post(
            f"/api/applications/{a['id']}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        b = client.post(
            "/api/applications", json=body(circle=(100, 100, 5))
        ).json()
        r = client.post(f"/api/applications/{b['id']}/preview").json()
        assert r["has_conflicts"] is False

    def test_unknown_application(self, client):
        assert client.get("/api/applications/999").status_code == 404
        assert client.post("/api/applications/999/preview").status_code == 404
        assert (
            client.post(
                "/api/applications/999/approve",
                json={"expected_revision": 0},
            ).status_code
            == 404
        )

    def test_future_revision_rejected(self, client):
        r = client.get("/api/revision/7")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "revision_out_of_range"


class TestBatchApprove:
    BATCH = "/api/applications/batch-approve"

    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def test_empty_list_rejected(self, client):
        r = client.post(
            self.BATCH, json={"expected_revision": 0, "application_ids": []}
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_failed"

    def test_duplicate_ids_rejected(self, client):
        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [1, 2, 1]},
        )
        assert r.status_code == 422
        details = r.json()["error"]["details"]
        assert details[0]["code"] == "duplicate_application_id"
        assert details[0]["duplicates"] == [1]
        # no revision consumed
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_unknown_id_rejects_whole_batch(self, client):
        a = self._submit(client, "A")
        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, 999]},
        )
        assert r.status_code == 404
        details = r.json()["error"]["details"]
        assert details["missing_application_ids"] == [999]
        # the valid member stays pending and no revision was consumed
        assert client.get(f"/api/applications/{a}").json()["status"] == "pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_happy_path_consumes_consecutive_revisions_in_order(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [c, a, b]},
        )
        assert r.status_code == 200, r.text
        body_ = r.json()
        assert body_["approved"] is True
        assert body_["from_revision"] == 0
        assert body_["revision"] == 3
        assert body_["to_revision"] == 3
        assert body_["application_ids"] == [c, a, b]
        assert [x["application_id"] for x in body_["results"]] == [c, a, b]
        assert [x["revision"] for x in body_["results"]] == [1, 2, 3]
        for result, expected in zip(body_["results"], ("approved",) * 3):
            assert result["application"]["status"] == expected

        # as-of each assigned revision restores exactly the members approved
        # up to and including that item
        snap1 = client.get("/api/revision/1").json()
        assert snap1["effective_application_ids"] == [c]
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == [c, a]
        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == [c, a, b]

    def test_stale_revision_rejects_whole_batch(self, client):
        a = self._submit(client, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [b, c]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 0, "current_revision": 1}
        # nothing partially approved, revision unchanged
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["effective_application_ids"] == [a]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert client.get(f"/api/applications/{c}").json()["status"] == "pending"

    def test_status_mismatch_rejects_whole_batch(self, client):
        a = self._submit(client, "A")
        b = self._submit(client, "B", freq=(200, 201))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()

        r = client.post(
            self.BATCH,
            json={"expected_revision": 1, "application_ids": [a, b]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "not_pending"
        assert err["details"]["applications"] == [
            {
                "application_id": a,
                "status": "approved",
                "approval_revision": 1,
            }
        ]
        # b is untouched
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 1

    def test_interference_against_effective_set_rejects_whole_batch(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        clashing = self._submit(client, "clash", freq=(105, 106))
        clean = self._submit(client, "clean", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 1, "application_ids": [clashing, clean]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        failed = err["details"]["applications"]
        assert [x["application_id"] for x in failed] == [clashing]
        (conflict,) = failed[0]["conflicts"]
        assert conflict["scope"] == "effective"
        assert conflict["application"]["id"] == a
        assert conflict["reason"] == "frequency_intersection"
        # neither member approved
        assert (
            client.get(f"/api/applications/{clashing}").json()["status"]
            == "pending"
        )
        assert client.get(f"/api/applications/{clean}").json()["status"] == "pending"
        assert client.get("/api/revision/current").json()["current_revision"] == 1

    def test_intra_group_interference_reported_for_both_sides(self, client):
        a = self._submit(client, "A", freq=(600, 610))
        b = self._submit(client, "B", freq=(605, 615))
        c = self._submit(client, "C", freq=(300, 301))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, b, c]},
        )
        assert r.status_code == 409
        failed = r.json()["error"]["details"]["applications"]
        assert [x["application_id"] for x in failed] == [a, b]
        sides = {
            x["application_id"]: [
                (k["scope"], k["application"]["id"]) for k in x["conflicts"]
            ]
            for x in failed
        }
        assert sides[a] == [("group", b)]
        assert sides[b] == [("group", a)]
        # all members still pending, no revisions consumed
        for app_id in (a, b, c):
            assert (
                client.get(f"/api/applications/{app_id}").json()["status"]
                == "pending"
            )
        assert client.get("/api/revision/current").json()["current_revision"] == 0

    def test_batch_and_single_share_revision_arbitration(self, client):
        # A single approval advances the revision after the batch was based on
        # it; the batch must then be stale, never partially approved.
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))

        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        r = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [b, c]},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"

        # re-deciding on the fresh revision succeeds and continues the sequence
        r2 = client.post(
            self.BATCH,
            json={"expected_revision": 1, "application_ids": [b, c]},
        )
        assert r2.status_code == 200, r2.text
        assert [x["revision"] for x in r2.json()["results"]] == [2, 3]

    def test_two_batches_same_revision_one_wins_one_stale(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))
        d = self._submit(client, "D", freq=(400, 401))

        first = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, b]},
        )
        assert first.status_code == 200
        # same expected revision, as if launched concurrently
        second = client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [c, d]},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "stale_revision"
        assert second.json()["error"]["details"]["current_revision"] == 2
        for app_id in (c, d):
            assert (
                client.get(f"/api/applications/{app_id}").json()["status"]
                == "pending"
            )
        # retry on the new revision succeeds and keeps revisions consecutive
        retry = client.post(
            self.BATCH,
            json={"expected_revision": 2, "application_ids": [c, d]},
        )
        assert retry.status_code == 200
        assert [x["revision"] for x in retry.json()["results"]] == [3, 4]

    def test_revoke_interleaves_with_batch_revisions(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        client.post(
            self.BATCH,
            json={"expected_revision": 0, "application_ids": [a, b]},
        ).raise_for_status()
        # revoke consumes the next shared revision
        rev = client.post(
            f"/api/applications/{a}/revoke", json={"reason": "moved"}
        ).json()
        assert rev["revision"] == 3

        c = self._submit(client, "C", freq=(100, 101))
        r = client.post(
            f"/api/applications/{c}/approve", json={"expected_revision": 3}
        )
        assert r.status_code == 200 and r.json()["revision"] == 4

    def test_revoked_member_rejected_with_already_revoked(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        client.post(
            f"/api/applications/{a}/revoke", json={"reason": "moved"}
        ).raise_for_status()
        b = self._submit(client, "B", freq=(200, 201))

        r = client.post(
            self.BATCH,
            json={"expected_revision": 2, "application_ids": [a, b]},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"


class TestAvailableRanges:
    """POST /api/applications/{id}/available-ranges (non-mutating search)."""

    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def _approve(self, client, app_id, expected):
        r = client.post(
            f"/api/applications/{app_id}/approve",
            json={"expected_revision": expected},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def _query(self, client, app_id, lo, hi):
        return client.post(
            f"/api/applications/{app_id}/available-ranges",
            json={"candidate_low_min": lo, "candidate_low_max": hi},
        )

    def test_ranges_split_by_blocking_grant(self, client):
        a = self._submit(client, "A", freq=(110, 120))
        self._approve(client, a, 0)
        b = self._submit(client, "B", freq=(100, 105))  # width 5

        r = self._query(client, b, 90, 130)
        assert r.status_code == 200, r.text
        body_ = r.json()
        assert body_["application_id"] == b
        assert body_["current_revision"] == 1
        assert body_["guard_band"] == 2.0
        assert body_["bandwidth"] == 5.0
        assert body_["candidate_low_min"] == 90
        assert body_["candidate_low_max"] == 130
        # grant [110,120] forbids lower bounds (103,122); gap == guard is ok
        assert body_["available_ranges"] == [
            {"min": 90, "max": 103, "min_inclusive": True, "max_inclusive": True},
            {"min": 122, "max": 130, "min_inclusive": True, "max_inclusive": True},
        ]

    def test_only_space_and_time_intersecting_grants_block(self, client):
        far = self._submit(client, "far", circle=(1000, 1000, 5), freq=(110, 120))
        later = self._submit(
            client,
            "later",
            freq=(110, 120),
            start="2026-10-01T10:00:00Z",  # == default end, half-open: no overlap
            end="2026-10-01T12:00:00Z",
        )
        self._approve(client, far, 0)
        self._approve(client, later, 1)
        b = self._submit(client, "B", freq=(100, 105))

        body_ = self._query(client, b, 90, 130).json()
        assert body_["available_ranges"] == [
            {"min": 90, "max": 130, "min_inclusive": True, "max_inclusive": True}
        ]

    def test_empty_when_fully_covered(self, client):
        a = self._submit(client, "A", freq=(110, 120))
        self._approve(client, a, 0)
        b = self._submit(client, "B", freq=(100, 100))
        body_ = self._query(client, b, 111, 119).json()
        assert body_["available_ranges"] == []

    def test_zero_guard_band_excludes_touching_endpoints(self, client):
        r = client.post(
            "/api/config/guard-band",
            json={"guard_band": 0.0, "expected_revision": 0},
        )
        assert r.status_code == 200
        a = self._submit(client, "A", freq=(110, 120))
        self._approve(client, a, 1)
        b = self._submit(client, "B", freq=(100, 105))

        body_ = self._query(client, b, 90, 130).json()
        assert body_["guard_band"] == 0.0
        # touching bands interfere: 105 and 120 themselves are not attainable
        assert body_["available_ranges"] == [
            {"min": 90, "max": 105, "min_inclusive": True, "max_inclusive": False},
            {"min": 120, "max": 130, "min_inclusive": False, "max_inclusive": True},
        ]

    def test_single_frequency_application(self, client):
        a = self._submit(client, "A", freq=(110, 120))
        self._approve(client, a, 0)
        b = self._submit(client, "B", freq=(100, 100))  # single frequency
        body_ = self._query(client, b, 100, 130).json()
        assert body_["bandwidth"] == 0.0
        assert body_["available_ranges"] == [
            {"min": 100, "max": 108, "min_inclusive": True, "max_inclusive": True},
            {"min": 122, "max": 130, "min_inclusive": True, "max_inclusive": True},
        ]

    def test_query_is_non_mutating(self, client):
        a = self._submit(client, "A", freq=(110, 120))
        self._approve(client, a, 0)
        b = self._submit(client, "B", freq=(100, 105))
        before = client.get(f"/api/applications/{b}").json()

        self._query(client, b, 90, 130).raise_for_status()
        self._query(client, b, 90, 130).raise_for_status()

        assert client.get(f"/api/applications/{b}").json() == before
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["effective_application_ids"] == [a]

    def test_consistent_with_preview_at_reported_bounds(self, client):
        a = self._submit(client, "A", freq=(110, 120))
        self._approve(client, a, 0)
        b = self._submit(client, "B", freq=(100, 105))
        app_b = client.get(f"/api/applications/{b}").json()
        body_ = self._query(client, b, 90, 130).json()

        # every reported inclusive endpoint previews conflict-free when the
        # band of B is shifted there; a forbidden midpoint does not
        points = []
        for r in body_["available_ranges"]:
            if r["min_inclusive"]:
                points.append((r["min"], False))
            if r["max_inclusive"]:
                points.append((r["max"], False))
        points.append((110, True))  # inside the forbidden zone
        for low, expect_conflict in points:
            shifted = client.post(
                "/api/applications",
                json=body(
                    name=f"shifted-{low}",
                    freq=(low, low + body_["bandwidth"]),
                ),
            ).json()
            prev = client.post(
                f"/api/applications/{shifted['id']}/preview"
            ).json()
            assert prev["has_conflicts"] is expect_conflict, (low, prev)
        # the original application is untouched by all of this
        assert client.get(f"/api/applications/{b}").json() == app_b

    def test_unknown_application_rejected(self, client):
        r = self._query(client, 999, 90, 130)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_non_pending_rejected(self, client):
        a = self._submit(client, "A")
        self._approve(client, a, 0)
        r = self._query(client, a, 90, 130)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        assert r.json()["error"]["details"] == {"status": "approved"}

        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()
        r2 = self._query(client, a, 90, 130)
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "not_pending"

    def test_inverted_candidate_interval_rejected(self, client):
        b = self._submit(client, "B")
        r = self._query(client, b, 130, 90)
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        codes = [d["code"] for d in err["details"]]
        assert codes == ["candidate_low_min_above_max"]
        # equal bounds are allowed (a single candidate position)
        ok = self._query(client, b, 100, 100)
        assert ok.status_code == 200
        assert ok.json()["available_ranges"] == [
            {"min": 100, "max": 100, "min_inclusive": True, "max_inclusive": True}
        ]

    def test_non_finite_candidate_interval_rejected(self, client):
        b = self._submit(client, "B")
        for raw in (
            '{"candidate_low_min": NaN, "candidate_low_max": 130}',
            '{"candidate_low_min": 90, "candidate_low_max": Infinity}',
            '{"candidate_low_min": -Infinity, "candidate_low_max": 130}',
        ):
            r = client.post(
                f"/api/applications/{b}/available-ranges",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 422, raw
            err = r.json()["error"]
            assert err["code"] == "validation_failed"
            codes = [d["code"] for d in err["details"]]
            assert codes == ["value_not_finite"]
        # nothing consumed by the rejections
        assert client.get("/api/revision/current").json()["current_revision"] == 0


class TestGuardBandConfig:
    """Online guard-band configuration: preview, confirm, history."""

    CONFIG = "/api/config/guard-band"
    PREVIEW = "/api/config/guard-band/preview"

    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def _approve(self, client, app_id, expected):
        r = client.post(
            f"/api/applications/{app_id}/approve",
            json={"expected_revision": expected},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_initial_config_seeded_from_env(self, client):
        # conftest sets GUARD_BAND=2.0; a fresh database adopts it once.
        r = client.get(self.CONFIG)
        assert r.status_code == 200
        assert r.json() == {
            "current_revision": 0,
            "guard_band": 2.0,
            "initial_guard_band": 2.0,
        }
        current = client.get("/api/revision/current").json()
        assert current["guard_band"] == 2.0

    def test_preview_reports_pairs_and_does_not_mutate(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        b = self._submit(client, "B", freq=(113, 114))  # gap 3 >= guard 2
        self._approve(client, a, 0)
        self._approve(client, b, 1)

        r = client.post(self.PREVIEW, json={"guard_band": 4.0})
        assert r.status_code == 200
        body_ = r.json()
        assert body_["current_revision"] == 2
        assert body_["current_guard_band"] == 2.0
        assert body_["proposed_guard_band"] == 4.0
        assert body_["effective_application_ids"] == [a, b]
        assert body_["has_conflicts"] is True
        assert body_["interfering_pair_count"] == 1
        (pair,) = body_["interfering_pairs"]
        assert pair["first"]["id"] == a
        assert pair["second"]["id"] == b
        assert pair["reason"] == "within_guard_band"
        assert pair["trigger"] == {
            "time_intersect": True,
            "area_intersect": True,
            "frequency_intersect": False,
            "minimum_frequency_gap": 3.0,
            "within_guard_band": True,
        }

        # preview is non-mutating: band, effective set and revision unchanged
        cfg = client.get(self.CONFIG).json()
        assert cfg["guard_band"] == 2.0 and cfg["current_revision"] == 2
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["effective_application_ids"] == [a, b]

        # tightening is always safe for the same effective set
        r2 = client.post(self.PREVIEW, json={"guard_band": 1.0})
        assert r2.json()["has_conflicts"] is False
        assert r2.json()["interfering_pairs"] == []

    def test_confirm_consumes_one_revision_and_binds_later_decisions(
        self, client
    ):
        a = self._submit(client, "A", freq=(100, 110))
        approved = self._approve(client, a, 0)  # revision 1, under band 2.0
        assert approved["guard_band"] == 2.0
        b = self._submit(client, "B", freq=(112, 120))  # gap 2 == band: ok

        r = client.post(
            self.CONFIG, json={"guard_band": 3.0, "expected_revision": 1}
        )
        assert r.status_code == 200, r.text
        body_ = r.json()
        assert body_["updated"] is True
        assert body_["revision"] == 2
        assert body_["previous_guard_band"] == 2.0
        assert body_["guard_band"] == 3.0

        # exactly one revision was produced; the live band moved
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["guard_band"] == 3.0

        # the new band now drives preview and single approval (gap 2 < 3)
        prev = client.post(f"/api/applications/{b}/preview").json()
        assert prev["guard_band"] == 3.0
        assert prev["has_conflicts"] is True
        blocked = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 2}
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "interference"

        # history restores the band in force at each revision
        snap1 = client.get("/api/revision/1").json()
        assert snap1["guard_band"] == 2.0
        snap2 = client.get("/api/revision/2").json()
        assert snap2["guard_band"] == 3.0
        assert [
            (c["revision"], c["previous_guard_band"], c["guard_band"])
            for c in snap2["guard_band_changes"]
        ] == [(0, None, 2.0), (2, 2.0, 3.0)]

        # the earlier approval record keeps the band it was decided under
        assert snap1["approvals"][0]["guard_band"] == 2.0
        assert snap2["approvals"][0]["guard_band"] == 2.0

    def test_confirm_with_stale_revision_rejected(self, client):
        a = self._submit(client, "A")
        self._approve(client, a, 0)

        r = client.post(
            self.CONFIG, json={"guard_band": 1.0, "expected_revision": 0}
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 0, "current_revision": 1}
        # nothing changed
        cfg = client.get(self.CONFIG).json()
        assert cfg["guard_band"] == 2.0 and cfg["current_revision"] == 1

    def test_confirm_that_would_break_effective_pairs_rejected(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        b = self._submit(client, "B", freq=(113, 114))  # gap 3
        self._approve(client, a, 0)
        self._approve(client, b, 1)

        r = client.post(
            self.CONFIG, json={"guard_band": 4.0, "expected_revision": 2}
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        details = err["details"]
        assert details["current_revision"] == 2
        assert details["current_guard_band"] == 2.0
        assert details["proposed_guard_band"] == 4.0
        (pair,) = details["interfering_pairs"]
        assert {pair["first"]["id"], pair["second"]["id"]} == {a, b}
        assert pair["reason"] == "within_guard_band"

        # band, effective set and revision all unchanged
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["guard_band"] == 2.0
        assert current["effective_application_ids"] == [a, b]

    def test_invalid_values_rejected(self, client):
        for bad in (-0.5, -1):
            r = client.post(
                self.CONFIG,
                json={"guard_band": bad, "expected_revision": 0},
            )
            assert r.status_code == 422, bad
            assert r.json()["error"]["code"] == "validation_failed"
            codes = [d["code"] for d in r.json()["error"]["details"]]
            assert codes == ["guard_band_negative"]
            r2 = client.post(self.PREVIEW, json={"guard_band": bad})
            assert r2.status_code == 422, bad
            assert r2.json()["error"]["code"] == "validation_failed"

        # non-finite literals (JSON allows NaN/Infinity in Python's parser)
        for raw in ("NaN", "Infinity", "-Infinity"):
            r = client.post(
                self.CONFIG,
                content=(
                    '{"guard_band": %s, "expected_revision": 0}' % raw
                ),
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 422, raw
            assert r.json()["error"]["code"] == "validation_failed"
            codes = [d["code"] for d in r.json()["error"]["details"]]
            assert codes == ["value_not_finite"]
            r2 = client.post(
                self.PREVIEW,
                content='{"guard_band": %s}' % raw,
                headers={"Content-Type": "application/json"},
            )
            assert r2.status_code == 422, raw

        missing = client.post(self.CONFIG, json={"expected_revision": 0})
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "validation_failed"

        # nothing consumed by any of the rejections
        assert client.get(self.CONFIG).json() == {
            "current_revision": 0,
            "guard_band": 2.0,
            "initial_guard_band": 2.0,
        }

    def test_restart_does_not_overwrite_effective_band(self, client, monkeypatch):
        from app import db
        from app.config import settings

        r = client.post(
            self.CONFIG, json={"guard_band": 5.0, "expected_revision": 0}
        )
        assert r.status_code == 200

        # simulate a restart with a different GUARD_BAND: init_db must not
        # overwrite the live setting
        monkeypatch.setattr(settings, "guard_band", 9.0)
        db.init_db()

        cfg = client.get(self.CONFIG).json()
        assert cfg["guard_band"] == 5.0
        assert cfg["initial_guard_band"] == 2.0
        assert cfg["current_revision"] == 1
        snap = client.get("/api/revision/1").json()
        assert snap["guard_band"] == 5.0

    def test_shares_revision_arbitration_with_approve_batch_and_revoke(
        self, client
    ):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))

        # a guard-band change consumes revision 1
        r = client.post(
            self.CONFIG, json={"guard_band": 1.5, "expected_revision": 0}
        )
        assert r.status_code == 200 and r.json()["revision"] == 1

        # a single approval based on the old revision is stale
        stale = client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_revision"

        # batch approval shares the same arbitration and the new band
        batch = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 1, "application_ids": [a, b]},
        )
        assert batch.status_code == 200, batch.text
        assert [x["revision"] for x in batch.json()["results"]] == [2, 3]
        assert batch.json()["guard_band"] == 1.5

        # a confirm based on a pre-batch revision is stale
        stale2 = client.post(
            self.CONFIG, json={"guard_band": 1.0, "expected_revision": 1}
        )
        assert stale2.status_code == 409
        assert stale2.json()["error"]["code"] == "stale_revision"

        # revoke shares the sequence too (revision 4), then confirm (5)
        rev = client.post(f"/api/applications/{a}/revoke", json={})
        assert rev.json()["revision"] == 4
        ok = client.post(
            self.CONFIG, json={"guard_band": 1.0, "expected_revision": 4}
        )
        assert ok.status_code == 200 and ok.json()["revision"] == 5

    def test_preview_ignores_suspended_licenses(self, client):
        # A and B would clash under a wider band; suspending A removes it
        # from the effective set that the guard-band preview checks.
        a = self._submit(client, "A", freq=(100, 110))
        b = self._submit(client, "B", freq=(113, 114))  # gap 3
        self._approve(client, a, 0)
        self._approve(client, b, 1)
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 2},
        ).raise_for_status()

        preview = client.post(self.PREVIEW, json={"guard_band": 4.0}).json()
        assert preview["current_revision"] == 3
        assert preview["effective_application_ids"] == [b]
        assert preview["has_conflicts"] is False
        assert preview["interfering_pairs"] == []

        # once A is resumed the pair shows up again
        client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 3}
        ).raise_for_status()
        preview2 = client.post(self.PREVIEW, json={"guard_band": 4.0}).json()
        assert preview2["effective_application_ids"] == [a, b]
        assert preview2["interfering_pair_count"] == 1

    def test_batch_approval_uses_the_new_band(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        b = self._submit(client, "B", freq=(112.5, 113))  # intra-group gap 2.5
        ok = client.post(
            self.CONFIG, json={"guard_band": 3.0, "expected_revision": 0}
        )
        assert ok.status_code == 200

        # would have passed under the old band 2.0; rejected under 3.0
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 1, "application_ids": [a, b]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        sides = {
            x["application_id"]: [c["scope"] for c in x["conflicts"]]
            for x in err["details"]["applications"]
        }
        assert sides == {a: ["group"], b: ["group"]}
        # both still pending, revision unchanged
        for app_id in (a, b):
            assert (
                client.get(f"/api/applications/{app_id}").json()["status"]
                == "pending"
            )
        assert client.get("/api/revision/current").json()["current_revision"] == 1


class TestSuspendResume:
    """POST .../suspend and .../resume: temporary equipment outages."""

    def _submit(self, client, name, **kw):
        return client.post(
            "/api/applications", json=body(name=name, **kw)
        ).json()["id"]

    def _approve(self, client, app_id, expected):
        r = client.post(
            f"/api/applications/{app_id}/approve",
            json={"expected_revision": expected},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def _suspend(self, client, app_id, expected, **kw):
        return client.post(
            f"/api/applications/{app_id}/suspend",
            json={"expected_revision": expected, **kw},
        )

    def _resume(self, client, app_id, expected, **kw):
        return client.post(
            f"/api/applications/{app_id}/resume",
            json={"expected_revision": expected, **kw},
        )

    def test_suspend_consumes_one_revision_and_leaves_effective_set(
        self, client
    ):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)

        r = self._suspend(client, a, 1, reason="device downtime")
        assert r.status_code == 200, r.text
        body_ = r.json()
        assert body_["suspended"] is True
        assert body_["revision"] == 2
        assert body_["previous_approval_revision"] == 1
        assert body_["reason"] == "device downtime"
        assert body_["application"]["status"] == "suspended"
        assert body_["application"]["suspended_revision"] == 2
        assert body_["application"]["approval_revision"] == 1

        app_a = client.get(f"/api/applications/{a}").json()
        assert app_a["status"] == "suspended"
        assert app_a["suspended_revision"] == 2

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["effective_application_ids"] == []
        assert current["suspended_application_ids"] == [a]
        assert current["suspensions"] == [
            {
                "revision": 2,
                "application_id": a,
                "reason": "device downtime",
                "created_at": current["suspensions"][0]["created_at"],
            }
        ]
        assert current["resumptions"] == []

        # the status filter finds it
        suspended = client.get("/api/applications?status=suspended").json()
        assert [x["id"] for x in suspended["applications"]] == [a]

    def test_resume_consumes_one_revision_and_restores_license(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)
        self._suspend(client, a, 1).raise_for_status()

        r = self._resume(client, a, 2, reason="fault cleared")
        assert r.status_code == 200, r.text
        body_ = r.json()
        assert body_["resumed"] is True
        assert body_["revision"] == 3
        assert body_["original_approval_revision"] == 1
        assert body_["guard_band"] == 2.0
        assert body_["reason"] == "fault cleared"
        assert body_["application"]["status"] == "approved"
        assert body_["application"]["suspended_revision"] is None

        # the original approval revision is unchanged: no new application
        app_a = client.get(f"/api/applications/{a}").json()
        assert app_a["status"] == "approved"
        assert app_a["approval_revision"] == 1
        assert app_a["suspended_revision"] is None

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [a]
        assert current["suspended_application_ids"] == []
        (resume_event,) = current["resumptions"]
        assert resume_event["revision"] == 3
        assert resume_event["application_id"] == a
        assert resume_event["guard_band"] == 2.0
        assert resume_event["reason"] == "fault cleared"

    def test_suspension_changes_preview_and_available_ranges(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        b = self._submit(client, "B", freq=(105, 108))
        self._approve(client, a, 0)

        prev = client.post(f"/api/applications/{b}/preview").json()
        assert prev["has_conflicts"] is True

        # while A is suspended, B sees an empty effective set
        self._suspend(client, a, 1).raise_for_status()
        prev2 = client.post(f"/api/applications/{b}/preview").json()
        assert prev2["current_revision"] == 2
        assert prev2["has_conflicts"] is False
        ranges = client.post(
            f"/api/applications/{b}/available-ranges",
            json={"candidate_low_min": 90, "candidate_low_max": 130},
        ).json()
        assert ranges["available_ranges"] == [
            {"min": 90, "max": 130, "min_inclusive": True, "max_inclusive": True}
        ]

        # after A is resumed B is blocked again
        self._resume(client, a, 2).raise_for_status()
        prev3 = client.post(f"/api/applications/{b}/preview").json()
        assert prev3["current_revision"] == 3
        assert prev3["has_conflicts"] is True

    def test_stale_suspension_returns_current_revision_and_changes_nothing(
        self, client
    ):
        a = self._submit(client, "A")
        b = self._submit(client, "B", freq=(200, 201))
        self._approve(client, a, 0)
        self._approve(client, b, 1)

        # expected 1 but a second approval already moved the revision to 2
        r = self._suspend(client, a, 1)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 1, "current_revision": 2}
        # still effective, revision unchanged
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["effective_application_ids"] == [a, b]
        assert current["suspensions"] == []

    def test_resume_interference_keeps_suspended_and_consumes_no_revision(
        self, client
    ):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)
        self._suspend(client, a, 1).raise_for_status()

        # a new overlapping license C is approved while A is suspended
        c = self._submit(client, "C", freq=(105, 108))
        self._approve(client, c, 2)

        r = self._resume(client, a, 3)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        assert err["details"]["current_revision"] == 3
        (conflict,) = err["details"]["conflicts"]
        assert conflict["application"]["id"] == c
        assert conflict["reason"] == "frequency_intersection"

        # A stays suspended; no revision consumed; C stays effective
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [c]
        assert current["suspended_application_ids"] == [a]
        assert current["resumptions"] == []

        # removing the blocker lets the resume succeed on the same revision
        client.post(f"/api/applications/{c}/revoke", json={}).raise_for_status()
        ok = self._resume(client, a, 4)
        assert ok.status_code == 200
        assert ok.json()["revision"] == 5

    def test_resume_uses_the_current_guard_band(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)
        self._suspend(client, a, 1).raise_for_status()
        # widen the guard band while A is suspended: gap to C is exactly 2
        client.post(
            "/api/config/guard-band",
            json={"guard_band": 3.0, "expected_revision": 2},
        ).raise_for_status()
        c = self._submit(client, "C", freq=(112, 113))
        # gap 2 >= old band 2 but < new band 3: approvable while A suspended
        self._approve(client, c, 3)

        r = self._resume(client, a, 4)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "interference"
        (conflict,) = r.json()["error"]["details"]["conflicts"]
        assert conflict["application"]["id"] == c
        assert conflict["reason"] == "within_guard_band"
        # a successful resumption keeps the band it was re-decided under
        client.post(f"/api/applications/{c}/revoke", json={}).raise_for_status()
        ok = self._resume(client, a, 5)
        assert ok.status_code == 200
        assert ok.json()["guard_band"] == 3.0
        snap = client.get(f"/api/revision/{ok.json()['revision']}").json()
        assert snap["resumptions"][0]["guard_band"] == 3.0

    def test_duplicate_suspend_and_resume_rejected(self, client):
        a = self._submit(client, "A")
        self._approve(client, a, 0)
        self._suspend(client, a, 1).raise_for_status()

        again = self._suspend(client, a, 2)
        assert again.status_code == 409
        err = again.json()["error"]
        assert err["code"] == "already_suspended"
        assert err["details"] == {"suspended_at_revision": 2}

        self._resume(client, a, 2).raise_for_status()
        again2 = self._resume(client, a, 3)
        assert again2.status_code == 409
        assert again2.json()["error"]["code"] == "not_suspended"
        assert again2.json()["error"]["details"] == {"status": "approved"}

        # neither rejection consumed a revision
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 3
        )

    def test_suspend_pending_or_revoked_and_resume_invalid_states(self, client):
        a = self._submit(client, "A")  # pending
        r = self._suspend(client, a, 0)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_approved"
        assert r.json()["error"]["details"] == {"status": "pending"}

        r2 = self._resume(client, a, 0)
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "not_suspended"
        assert r2.json()["error"]["details"] == {"status": "pending"}

        # revoke a suspended license: permanent, resume is then refused
        self._approve(client, a, 0)
        self._suspend(client, a, 1).raise_for_status()
        rv = client.post(f"/api/applications/{a}/revoke", json={"reason": "gone"})
        assert rv.status_code == 200
        assert rv.json()["was_suspended"] is True

        r3 = self._resume(client, a, 3)
        assert r3.status_code == 409
        assert r3.json()["error"]["code"] == "already_revoked"

        r4 = self._suspend(client, a, 3)
        assert r4.status_code == 409
        assert r4.json()["error"]["code"] == "already_revoked"

        # revoking twice is still refused with the original code
        r5 = client.post(f"/api/applications/{a}/revoke", json={})
        assert r5.status_code == 409
        assert r5.json()["error"]["code"] == "already_revoked"

    def test_unknown_ids_are_404(self, client):
        assert self._suspend(client, 999, 0).status_code == 404
        assert self._resume(client, 999, 0).status_code == 404

    def test_share_revision_arbitration_with_all_other_writers(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        b = self._submit(client, "B", freq=(200, 201))
        c = self._submit(client, "C", freq=(300, 301))
        self._approve(client, a, 0)  # revision 1

        # suspend based on a stale revision loses
        stale = self._suspend(client, a, 0)
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_revision"

        # a fresh suspend takes revision 2
        ok = self._suspend(client, a, 1)
        assert ok.status_code == 200 and ok.json()["revision"] == 2

        # an approval based on revision 1 is stale now; retries at 2
        stale_app = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert stale_app.json()["error"]["code"] == "stale_revision"
        self._approve(client, b, 2)  # revision 3

        # batch approval shares the sequence too (revision 4)
        batch = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 3, "application_ids": [c]},
        )
        assert batch.status_code == 200
        assert batch.json()["revision"] == 4

        # a resume based on an old revision loses; retry at 4 -> revision 5
        stale_resume = self._resume(client, a, 2)
        assert stale_resume.status_code == 409
        assert stale_resume.json()["error"]["details"] == {
            "expected_revision": 2,
            "current_revision": 4,
        }
        ok2 = self._resume(client, a, 4)
        assert ok2.status_code == 200 and ok2.json()["revision"] == 5

    def test_suspend_resume_history_snapshots(self, client):
        a = self._submit(client, "A", freq=(100, 110))
        self._approve(client, a, 0)  # 1
        self._suspend(client, a, 1, reason="down").raise_for_status()  # 2
        self._resume(client, a, 2, reason="back").raise_for_status()  # 3

        # revision 1: approved and effective, original approval not rewritten
        snap1 = client.get("/api/revision/1").json()
        assert snap1["effective_application_ids"] == [a]
        assert snap1["suspended_application_ids"] == []
        (approval,) = snap1["approvals"]
        assert approval["status_as_of"] == "approved"
        assert snap1["suspensions"] == [] and snap1["resumptions"] == []

        # revision 2: suspended, marked on the approval record itself
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == []
        assert snap2["suspended_application_ids"] == [a]
        assert snap2["approvals"][0]["status_as_of"] == "suspended"
        assert snap2["approvals"][0]["suspended_at_revision"] == 2
        assert "revoked_at_revision" not in snap2["approvals"][0]
        assert snap2["suspensions"] == [
            {
                "revision": 2,
                "application_id": a,
                "reason": "down",
                "created_at": snap2["suspensions"][0]["created_at"],
            }
        ]

        # revision 3: effective again; history at 1/2 is unchanged
        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == [a]
        assert snap3["suspended_application_ids"] == []
        assert snap3["approvals"][0]["status_as_of"] == "approved"
        assert "suspended_at_revision" not in snap3["approvals"][0]
        assert snap3["resumptions"][0]["revision"] == 3
        assert snap3["resumptions"][0]["reason"] == "back"
        assert snap3["resumptions"][0]["guard_band"] == 2.0
        # the original approval event was never rewritten
        assert snap3["approvals"][0]["revision"] == 1
        assert snap3["approvals"][0]["guard_band"] == 2.0

    def test_multiple_suspend_resume_cycles_replay(self, client):
        a = self._submit(client, "A", freq=(100, 101))
        self._approve(client, a, 0)  # 1
        self._suspend(client, a, 1).raise_for_status()  # 2
        self._resume(client, a, 2).raise_for_status()  # 3
        self._suspend(client, a, 3).raise_for_status()  # 4

        # replay shows suspended with the *latest* suspension marker
        snap4 = client.get("/api/revision/4").json()
        assert snap4["suspended_application_ids"] == [a]
        assert snap4["approvals"][0]["suspended_at_revision"] == 4
        assert [x["revision"] for x in snap4["suspensions"]] == [2, 4]
        assert [x["revision"] for x in snap4["resumptions"]] == [3]

        # snapshots at intermediate revisions are stable
        assert (
            client.get("/api/revision/2").json()["suspended_application_ids"]
            == [a]
        )
        assert client.get("/api/revision/3").json()["effective_application_ids"] == [a]

        # ending with a revocation leaves a terminal as-of state
        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()  # 5
        snap5 = client.get("/api/revision/5").json()
        assert snap5["effective_application_ids"] == []
        assert snap5["suspended_application_ids"] == []
        assert snap5["approvals"][0]["status_as_of"] == "revoked"
        assert snap5["approvals"][0]["revoked_at_revision"] == 5
        assert "suspended_at_revision" not in snap5["approvals"][0]

    def test_rejected_suspend_resume_consume_no_revision(self, client):
        a = self._submit(client, "A")
        self._approve(client, a, 0)
        # every rejection kind below must leave the revision at 1
        self._suspend(client, a, 5)  # stale
        self._suspend(client, a, 1).raise_for_status()  # -> revision 2
        self._suspend(client, a, 2)  # already suspended
        self._resume(client, a, 9)  # stale
        p = self._submit(client, "P", freq=(900, 901))
        self._resume(client, p, 2)  # pending -> not_suspended
        assert client.get("/api/revision/current").json()["current_revision"] == 2
        self._resume(client, a, 2).raise_for_status()  # -> revision 3
        assert client.get("/api/revision/current").json()["current_revision"] == 3

