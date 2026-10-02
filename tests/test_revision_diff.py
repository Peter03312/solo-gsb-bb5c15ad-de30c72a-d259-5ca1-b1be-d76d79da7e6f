"""End-to-end tests for GET /api/revision/diff.

The diff endpoint reconstructs two revision snapshots with the same rules as
``GET /api/revision/{revision}`` and compares them; these tests build a
lifecycle covering approval, suspension, resumption, revocation, guard-band
changes and waitlist auto-approvals, then verify every section of the diff.
"""
from factories import make_payload


def submit(client, name, freq=(100, 110), circle=(0, 0, 10), **kw):
    return client.post(
        "/api/applications",
        json=make_payload(freq=freq, circle=circle, name=name, **kw),
    ).json()


def approve(client, app_id, expected):
    return client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": expected},
    ).json()


class TestRevisionDiffErrors:
    def test_base_revision_in_future_is_404(self, client):
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 0, "target_revision": 1},
        )
        assert r.status_code == 404
        err = r.json()["error"]
        assert err["code"] == "revision_out_of_range"
        assert err["details"]["target_revision"] == 1
        assert err["details"]["current_revision"] == 0

    def test_target_revision_in_future_is_404(self, client):
        a = submit(client, "A")
        approve(client, a["id"], 0)
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 5, "target_revision": 1},
        )
        assert r.status_code == 404
        err = r.json()["error"]
        assert err["code"] == "revision_out_of_range"
        assert err["details"]["base_revision"] == 5
        assert err["details"]["current_revision"] == 1

    def test_negative_revision_rejected(self, client):
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": -1, "target_revision": 0},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "revision_out_of_range"

    def test_unknown_application_id_rejected(self, client):
        r = client.get(
            "/api/revision/diff",
            params={
                "base_revision": 0,
                "target_revision": 0,
                "application_id": 999,
            },
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


class TestRevisionDiff:
    def _build_lifecycle(self, client):
        # revision 0: empty, guard band 2.0
        a = submit(client, "A")  # [100,110]
        b = submit(client, "B", freq=(200, 210), circle=(500, 500, 1))
        approve(client, a["id"], 0)   # revision 1: A effective
        approve(client, b["id"], 1)   # revision 2: B effective (far away)
        # suspend A -> revision 3 (A leaves effective set, joins suspended)
        client.post(
            f"/api/applications/{a['id']}/suspend",
            json={"expected_revision": 2, "reason": "outage"},
        )
        # resume A -> revision 4
        client.post(
            f"/api/applications/{a['id']}/resume",
            json={"expected_revision": 3, "reason": "fixed"},
        )
        # guard band change 2.0 -> 1.0 -> revision 5
        gb = client.post(
            "/api/config/guard-band",
            json={"guard_band": 1.0, "expected_revision": 4},
        ).json()
        assert gb["revision"] == 5
        # revoke B -> revision 6
        client.post(
            f"/api/applications/{b['id']}/revoke", json={"reason": "gone"}
        )
        return a, b

    def test_diff_full_forward(self, client):
        a, b = self._build_lifecycle(client)
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 0, "target_revision": 6},
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["base_revision"] == 0
        assert body["target_revision"] == 6
        assert body["current_revision"] == 6
        assert body["direction"] == "forward"
        assert body["filtered"] is False

        # sides
        assert body["base"]["effective_application_ids"] == []
        assert body["base"]["suspended_application_ids"] == []
        assert body["base"]["guard_band"] == 2.0
        assert body["target"]["effective_application_ids"] == [a["id"]]
        assert body["target"]["suspended_application_ids"] == []
        assert body["target"]["guard_band"] == 1.0

        # guard band
        assert body["guard_band"] == {
            "base": 2.0,
            "target": 1.0,
            "changed": True,
        }
        assert [g["revision"] for g in body["guard_band_changes"]] == [5]
        assert body["guard_band_changes"][0]["previous_guard_band"] == 2.0
        assert body["guard_band_changes"][0]["guard_band"] == 1.0

        # effective set diff is target-minus-base regardless of semantics
        assert body["effective_set_diff"] == {
            "added_application_ids": [a["id"]],
            "removed_application_ids": [],
            "shared_application_ids": [],
        }

        # status changes (A: pending -> approved; B: pending -> approved ->
        # revoked, ends revoked)
        by_app = {c["application_id"]: c for c in body["status_changes"]}
        assert by_app[a["id"]] == {
            "application_id": a["id"],
            "base_status": "pending",
            "target_status": "approved",
        }
        assert by_app[b["id"]]["base_status"] == "pending"
        assert by_app[b["id"]]["target_status"] == "revoked"
        assert by_app[b["id"]]["target_marker_revision"] == 6

        # windowed event lists (revision 0 seed event excluded; half-open)
        assert [x["revision"] for x in body["approvals"]] == [1, 2]
        assert [x["revision"] for x in body["suspensions"]] == [3]
        assert [x["revision"] for x in body["resumptions"]] == [4]
        assert [x["revision"] for x in body["revocations"]] == [6]
        assert body["suspensions"][0]["application_id"] == a["id"]
        assert body["suspensions"][0]["type"] == "suspension"
        assert body["resumptions"][0]["guard_band"] == 2.0

        # merged timeline strictly ordered, with presence annotations
        revisions = [e["revision"] for e in body["events"]]
        assert revisions == [1, 2, 3, 4, 5, 6]
        assert body["event_count"] == 6
        assert [e["type"] for e in body["events"]] == [
            "approval",
            "approval",
            "suspension",
            "resumption",
            "guard_band_change",
            "revocation",
        ]
        assert all(e["present_in"] == "target" for e in body["events"])
        assert all(e["absent_in"] == "base" for e in body["events"])

    def test_diff_window_is_half_open(self, client):
        a, b = self._build_lifecycle(client)
        # window (2, 6]: excludes the approval at revision 2 itself
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 2, "target_revision": 6},
        )
        body = r.json()
        assert [x["revision"] for x in body["approvals"]] == []
        assert [x["revision"] for x in body["events"]] == [3, 4, 5, 6]
        # at revision 2 both A and B are effective; at 6 only A
        assert body["base"]["effective_application_ids"] == [
            a["id"],
            b["id"],
        ]
        assert body["effective_set_diff"] == {
            "added_application_ids": [],
            "removed_application_ids": [b["id"]],
            "shared_application_ids": [a["id"]],
        }
        # A went approved -> suspended -> approved within the window: no net
        # status change, but its suspension/resumption events are listed
        assert all(
            c["application_id"] != a["id"] for c in body["status_changes"]
        )
        assert body["suspended_set_diff"] == {
            "added_application_ids": [],
            "removed_application_ids": [],
            "shared_application_ids": [],
        }

    def test_diff_captures_suspended_set_and_markers(self, client):
        a, _b = self._build_lifecycle(client)
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 2, "target_revision": 3},
        )
        body = r.json()
        assert body["effective_set_diff"]["removed_application_ids"] == [
            a["id"]
        ]
        assert body["suspended_set_diff"]["added_application_ids"] == [
            a["id"]
        ]
        (change,) = body["status_changes"]
        assert change == {
            "application_id": a["id"],
            "base_status": "approved",
            "target_status": "suspended",
            "target_marker_revision": 3,
        }
        assert body["target"]["suspended_applications"][0]["id"] == a["id"]

    def test_diff_same_revision_is_empty(self, client):
        self._build_lifecycle(client)
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 3, "target_revision": 3},
        )
        body = r.json()
        assert body["direction"] == "forward"
        assert body["events"] == []
        assert body["event_count"] == 0
        assert body["status_changes"] == []
        assert body["guard_band"]["changed"] is False
        assert body["effective_set_diff"]["shared_application_ids"] == [
            body["base"]["effective_application_ids"][0]
        ] if body["base"]["effective_application_ids"] else True

    def test_diff_backward_annotates_presence(self, client):
        a, b = self._build_lifecycle(client)
        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 6, "target_revision": 2},
        )
        body = r.json()
        assert body["direction"] == "backward"
        assert [e["revision"] for e in body["events"]] == [3, 4, 5, 6]
        # events were applied after the target, so they are present at base
        assert all(e["present_in"] == "base" for e in body["events"])
        assert all(e["absent_in"] == "target" for e in body["events"])
        # set diffs stay target-minus-base: B is effective at target but not
        # at base (it was revoked in between)
        assert body["effective_set_diff"]["added_application_ids"] == [
            b["id"]
        ]
        assert body["effective_set_diff"]["removed_application_ids"] == []
        # B's status change is expressed base -> target
        b_change = next(
            c for c in body["status_changes"] if c["application_id"] == b["id"]
        )
        assert b_change["base_status"] == "revoked"
        assert b_change["target_status"] == "approved"

    def test_diff_filtered_by_application(self, client):
        a, b = self._build_lifecycle(client)
        r = client.get(
            "/api/revision/diff",
            params={
                "base_revision": 0,
                "target_revision": 6,
                "application_id": a["id"],
            },
        )
        body = r.json()
        assert body["filtered"] is True
        focused = body["application_diff"]
        assert focused["application_id"] == a["id"]
        assert focused["application"]["id"] == a["id"]
        assert focused["base_status"] == "pending"
        assert focused["target_status"] == "approved"
        assert focused["base_effective"] is False
        assert focused["target_effective"] is True
        assert focused["base_suspended"] is False
        assert focused["target_suspended"] is False
        assert focused["status_change"]["target_status"] == "approved"
        # only A's own lifecycle events ...
        assert [
            (e["type"], e["revision"]) for e in focused["events"]
        ] == [("approval", 1), ("suspension", 3), ("resumption", 4)]
        # ... top-level filtered sections likewise
        assert [x["application_id"] for x in body["approvals"]] == [a["id"]]
        assert [x["application_id"] for x in body["suspensions"]] == [a["id"]]
        assert [x["application_id"] for x in body["resumptions"]] == [a["id"]]
        assert body["revocations"] == []
        # guard-band changes are global and remain in the merged timeline
        assert [e["revision"] for e in body["events"]] == [1, 3, 4, 5]
        assert body["event_count"] == 4
        assert [c["application_id"] for c in body["status_changes"]] == [
            a["id"]
        ]
        # set diffs remain global even when filtered
        assert body["effective_set_diff"]["added_application_ids"] == [
            a["id"]
        ]

    def test_diff_filtered_pending_application(self, client):
        a, _b = self._build_lifecycle(client)
        c = submit(client, "C", freq=(300, 301))
        r = client.get(
            "/api/revision/diff",
            params={
                "base_revision": 0,
                "target_revision": 6,
                "application_id": c["id"],
            },
        )
        assert r.status_code == 200, r.text
        focused = r.json()["application_diff"]
        assert focused["base_status"] == "pending"
        assert focused["target_status"] == "pending"
        assert focused["status_change"] is None
        assert focused["events"] == []

    def test_diff_does_not_mutate_state(self, client):
        self._build_lifecycle(client)
        before = client.get("/api/revision/current").json()
        client.get(
            "/api/revision/diff",
            params={"base_revision": 1, "target_revision": 6},
        )
        client.get(
            "/api/revision/diff",
            params={
                "base_revision": 0,
                "target_revision": 6,
                "application_id": 1,
            },
        )
        after = client.get("/api/revision/current").json()
        assert before["current_revision"] == after["current_revision"] == 6
        assert (
            before["effective_application_ids"]
            == after["effective_application_ids"]
        )
        assert before["approvals"] == after["approvals"]


class TestRevisionDiffWaitlist:
    def test_auto_approval_flags_visible_in_diff(self, client):
        # A effective at revision 1; B is a conflicting pending standby.
        a = submit(client, "A")
        b = submit(client, "B", freq=(105, 108))
        approve(client, a["id"], 0)  # revision 1
        client.post(f"/api/applications/{b['id']}/waitlist", json={})
        # revoke A at revision 2 -> waitlist drains, B auto-approved at 3
        client.post(f"/api/applications/{a['id']}/revoke", json={})

        r = client.get(
            "/api/revision/diff",
            params={"base_revision": 1, "target_revision": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        types = [(e["type"], e["revision"]) for e in body["events"]]
        assert types == [("revocation", 2), ("approval", 3)]
        auto = body["approvals"][0]
        assert auto["application_id"] == b["id"]
        assert auto["auto_approved"] is True
        assert auto["release_revision"] == 2
        assert auto["guard_band"] == 2.0
        assert auto["status_as_of"] == "approved"

        # filtered view on the released application shows only its revocation
        r2 = client.get(
            "/api/revision/diff",
            params={
                "base_revision": 1,
                "target_revision": 3,
                "application_id": a["id"],
            },
        )
        focused = r2.json()["application_diff"]
        assert [
            (e["type"], e["revision"]) for e in focused["events"]
        ] == [("revocation", 2)]
        assert focused["base_effective"] is True
        assert focused["target_effective"] is False
        assert focused["target_status"] == "revoked"
        assert focused["status_change"]["target_marker_revision"] == 2

    def test_diff_includes_batch_approvals_in_order(self, client):
        ids = []
        for i, freq in enumerate(((100, 101), (200, 201), (300, 301))):
            ids.append(submit(client, f"D{i}", freq=freq)["id"])
        client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 0, "application_ids": ids},
        )
        body = client.get(
            "/api/revision/diff",
            params={"base_revision": 0, "target_revision": 3},
        ).json()
        assert [
            (e["application_id"], e["revision"]) for e in body["approvals"]
        ] == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]
        assert all(e["auto_approved"] is False for e in body["approvals"])
        assert all(e["release_revision"] is None for e in body["approvals"])
