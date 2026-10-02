"""End-to-end tests for the standby waitlist feature.

Registration and cancellation consume no revision; a suspend/revoke release
drains the queue in registration order inside the same transaction: passers
are auto-approved on consecutive revisions, blockers keep their place and the
drain continues.  Guard band is the test default (2.0); every application in
these tests shares the default coverage circle and UTC window, so only the
frequency bands decide interference.
"""
from factories import make_payload


def submit(client, low, high, name="Event", **overrides):
    payload = make_payload(freq=(low, high), name=name, **overrides)
    r = client.post("/api/applications", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def approve(client, app_id, expected):
    r = client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": expected},
    )
    assert r.status_code == 200, r.text
    return r.json()


def register(client, app_id, reason=""):
    return client.post(
        f"/api/applications/{app_id}/waitlist", json={"reason": reason}
    )


def cancel(client, app_id):
    return client.post(f"/api/applications/{app_id}/waitlist/cancel")


def queue_ids(client):
    return client.get("/api/waitlist").json()["waitlist_application_ids"]


class TestRegistration:
    def test_register_pending_returns_position_and_current_revision(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")

        r = register(client, b, reason="band busy")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["waitlisted"] is True
        assert body["application_id"] == b
        assert body["queue_position"] == 1
        # Registration reports the current revision but consumes none.
        assert body["current_revision"] == 1
        assert body["reason"] == "band busy"
        assert body["registered_at"]

        # The application stays pending and the revision is unchanged.
        app = client.get(f"/api/applications/{b}").json()
        assert app["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )

    def test_empty_body_is_accepted(self, client):
        b = submit(client, 100, 110, "B")
        r = client.post(f"/api/applications/{b}/waitlist")
        assert r.status_code == 200, r.text
        assert r.json()["reason"] == ""

    def test_positions_follow_registration_order(self, client):
        ids = [submit(client, 200 + i, 201 + i, f"P{i}") for i in range(3)]
        for position, app_id in enumerate(ids, start=1):
            body = register(client, app_id).json()
            assert body["queue_position"] == position

        listing = client.get("/api/waitlist").json()
        assert listing["queue_length"] == 3
        assert listing["waitlist_application_ids"] == ids
        assert [e["position"] for e in listing["entries"]] == [1, 2, 3]

    def test_duplicate_registration_rejected(self, client):
        b = submit(client, 100, 110, "B")
        assert register(client, b).status_code == 200
        r = register(client, b)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_waitlisted"
        # Still exactly one entry.
        assert client.get("/api/waitlist").json()["queue_length"] == 1

    def test_unknown_application_rejected(self, client):
        r = register(client, 999999)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_non_pending_application_rejected(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        r = register(client, a)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        assert r.json()["error"]["details"]["status"] == "approved"

    def test_registration_consumes_no_revision_around_writes(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        register(client, b)
        register(client, b)  # rejected, still no revision
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )


class TestQuery:
    def test_empty_queue(self, client):
        body = client.get("/api/waitlist").json()
        assert body["queue_length"] == 0
        assert body["waitlist_application_ids"] == []
        assert body["entries"] == []
        assert body["current_revision"] == 0

    def test_single_entry_lookup(self, client):
        b = submit(client, 100, 110, "B")
        register(client, b)
        r = client.get(f"/api/applications/{b}/waitlist")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["position"] == 1
        assert body["application_id"] == b
        assert body["application"]["status"] == "pending"
        assert body["current_revision"] == 0

    def test_lookup_unknown_and_non_queued(self, client):
        assert (
            client.get("/api/applications/999999/waitlist").status_code == 404
        )
        b = submit(client, 100, 110, "B")
        r = client.get(f"/api/applications/{b}/waitlist")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_waitlisted"

    def test_current_snapshot_lists_queue(self, client):
        b = submit(client, 100, 110, "B")
        register(client, b, reason="waiting")
        body = client.get("/api/revision/current").json()
        assert body["waitlist_application_ids"] == [b]
        assert body["waitlist_entries"][0]["application_id"] == b
        assert body["waitlist_entries"][0]["reason"] == "waiting"


class TestCancellation:
    def test_cancel_removes_entry_and_changes_nothing_else(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        c = submit(client, 200, 210, "C")
        register(client, b)
        register(client, c)

        r = cancel(client, b)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cancelled"] is True
        assert body["application_id"] == b
        assert body["current_revision"] == 1
        # Cancelling a standby does not change the application's status.
        assert body["application"]["status"] == "pending"

        # C moves up to position 1; no revision was consumed.
        listing = client.get("/api/waitlist").json()
        assert listing["waitlist_application_ids"] == [c]
        assert listing["entries"][0]["position"] == 1
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )

    def test_cancelled_application_can_register_again(self, client):
        b = submit(client, 100, 110, "B")
        register(client, b)
        assert cancel(client, b).status_code == 200
        r = register(client, b)
        assert r.status_code == 200
        assert r.json()["queue_position"] == 1

    def test_cancel_unknown_and_non_queued(self, client):
        assert cancel(client, 999999).status_code == 404
        b = submit(client, 100, 110, "B")
        r = cancel(client, b)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_waitlisted"
        assert r.json()["error"]["details"]["status"] == "pending"


class TestReleaseDrainsQueue:
    def test_revoke_approves_passers_in_order_and_keeps_blocker(self, client):
        # A [100,110] effective; B [105,108] and C [106,109] are blocked by A
        # and also intersect each other; D [200,210] is free right now.
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        c = submit(client, 106, 109, "C")
        d = submit(client, 200, 210, "D")
        register(client, b)
        register(client, c)
        register(client, d)
        assert queue_ids(client) == [b, c, d]

        r = client.post(f"/api/applications/{a}/revoke", json={"reason": "off"})
        assert r.status_code == 200, r.text
        body = r.json()

        # The revoke itself keeps one revision; each auto approval takes the
        # next: revoke A = 2, auto B = 3, (C blocked), auto D = 4.
        assert body["revoked"] is True
        assert body["revision"] == 2
        assert body["final_revision"] == 4
        assert body["auto_approved_application_ids"] == [b, d]
        assert body["remaining_waitlist_application_ids"] == [c]

        pending = body["waitlist_pending"]
        assert len(pending) == 1
        assert pending[0]["application_id"] == c
        # C now clashes with B, which was auto-approved just before it.
        conflict_ids = [x["application"]["id"] for x in pending[0]["conflicts"]]
        assert conflict_ids == [b]
        assert pending[0]["conflicts"][0]["reason"] == "frequency_intersection"

        auto = {x["application_id"]: x for x in body["auto_approvals"]}
        assert auto[b]["revision"] == 3
        assert auto[b]["release_revision"] == 2
        assert auto[d]["revision"] == 4
        assert auto[d]["guard_band"] == 2.0

        # Application statuses and queue membership afterwards.
        assert client.get(f"/api/applications/{b}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()[
            "approval_revision"
        ] == 3
        assert client.get(f"/api/applications/{d}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{c}").json()["status"] == "pending"
        assert queue_ids(client) == [c]

    def test_blocker_is_retried_on_the_next_release(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        c = submit(client, 106, 109, "C")
        register(client, b)
        register(client, c)
        # First release: B through (rev 3), C blocked by B and stays queued.
        first = client.post(f"/api/applications/{a}/revoke", json={}).json()
        assert first["auto_approved_application_ids"] == [b]
        assert first["remaining_waitlist_application_ids"] == [c]
        assert first["final_revision"] == 3

        # Removing B as well lets C pass on the following release.
        second = client.post(f"/api/applications/{b}/revoke", json={})
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["revision"] == 4
        assert body["auto_approved_application_ids"] == [c]
        assert body["remaining_waitlist_application_ids"] == []
        assert body["final_revision"] == 5
        assert client.get(f"/api/applications/{c}").json()["status"] == "approved"

    def test_suspend_release_drains_the_same_way(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        d = submit(client, 200, 210, "D")
        register(client, b)
        register(client, d)

        r = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1, "reason": "outage"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["suspended"] is True
        assert body["revision"] == 2
        assert body["final_revision"] == 4
        assert body["auto_approved_application_ids"] == [b, d]
        assert body["remaining_waitlist_application_ids"] == []
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"
        assert queue_ids(client) == []

    def test_other_effective_grant_keeps_entry_blocked_after_suspend(self, client):
        # A and E are disjoint effective licenses; B conflicts with E only.
        a = submit(client, 100, 110, "A")
        e = submit(client, 130, 140, "E")
        approve(client, a, 0)
        approve(client, e, 1)  # revisions 1, 2
        b = submit(client, 120, 135, "B")
        register(client, b)

        r = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 2},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Suspension keeps revision 3; nothing was auto-approved.
        assert body["revision"] == 3
        assert body["final_revision"] == 3
        assert body["auto_approved_application_ids"] == []
        assert body["remaining_waitlist_application_ids"] == [b]
        clash = body["waitlist_pending"][0]["conflicts"][0]
        assert clash["application"]["id"] == e

        # Releasing E as well approves B.
        second = client.post(f"/api/applications/{e}/revoke", json={}).json()
        assert second["auto_approved_application_ids"] == [b]
        assert second["final_revision"] == 5

    def test_release_with_empty_queue_changes_nothing_but_shape(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        body = client.post(f"/api/applications/{a}/revoke", json={}).json()
        assert body["revision"] == 2
        assert body["final_revision"] == 2
        assert body["auto_approved_application_ids"] == []
        assert body["auto_approvals"] == []
        assert body["remaining_waitlist_application_ids"] == []
        assert body["waitlist_pending"] == []


class TestManualDecisionLeavesQueue:
    def test_manual_approval_dequeues_and_is_never_auto_approved(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        # D is already conflict-free; the operator approves it manually.
        d = submit(client, 200, 210, "D")
        register(client, d)
        approve(client, d, 1)  # revision 2
        assert queue_ids(client) == []

        body = client.post(f"/api/applications/{a}/revoke", json={}).json()
        assert body["revision"] == 3
        assert body["final_revision"] == 3
        assert body["auto_approved_application_ids"] == []
        # D's approval was manual, not replayed by the drain.
        d_event = [
            x
            for x in client.get("/api/revision/current").json()["approvals"]
            if x["application_id"] == d
        ][0]
        assert d_event["auto_approved"] is False
        assert d_event["release_revision"] is None

    def test_batch_approval_dequeues_its_members(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        d = submit(client, 200, 210, "D")
        e = submit(client, 300, 310, "E")
        register(client, d)
        register(client, e)
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 1, "application_ids": [d, e]},
        )
        assert r.status_code == 200, r.text
        assert queue_ids(client) == []
        body = client.post(f"/api/applications/{a}/revoke", json={}).json()
        assert body["auto_approved_application_ids"] == []
        assert body["final_revision"] == 4  # revoke = 4, no autos after 2,3

    def test_cancelled_standby_is_not_auto_approved(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        register(client, b)
        cancel(client, b)
        body = client.post(f"/api/applications/{a}/revoke", json={}).json()
        assert body["auto_approved_application_ids"] == []
        assert body["final_revision"] == 2
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"


class TestOtherWritersDoNotDrain:
    def test_guard_band_change_does_not_drain(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # rev 1
        b = submit(client, 105, 108, "B")
        register(client, b)

        r = client.post(
            "/api/config/guard-band",
            json={"guard_band": 1.0, "expected_revision": 1},
        )
        assert r.status_code == 200, r.text
        # Exactly one revision for the band change; queue untouched.
        assert r.json()["revision"] == 2
        assert queue_ids(client) == [b]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

        # The entry is adjudicated on a later release under the live band.
        body = client.post(f"/api/applications/{a}/revoke", json={}).json()
        assert body["auto_approved_application_ids"] == [b]
        auto = body["auto_approvals"][0]
        assert auto["guard_band"] == 1.0

    def test_resume_does_not_drain(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # rev 1
        # Suspend A (empty queue) rev 2, then approve a disjoint B manually
        # while it is down (rev 3).
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1},
        )
        b = submit(client, 200, 210, "B")
        approve(client, b, 2)

        # C is queued while A is suspended; resuming A must not drain it.
        c = submit(client, 105, 108, "C")
        register(client, c)
        r = client.post(
            f"/api/applications/{a}/resume",
            json={"expected_revision": 3},
        )
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 4
        assert "auto_approved_application_ids" not in r.json()
        assert queue_ids(client) == [c]
        assert client.get(f"/api/applications/{c}").json()["status"] == "pending"

        # The later release does drain it.
        body = client.post(f"/api/applications/{a}/revoke", json={}).json()
        # A's revoke = 5; C free vs B -> auto rev 6.
        assert body["auto_approved_application_ids"] == [c]
        assert body["final_revision"] == 6


class TestRollbackAndHistory:
    def test_failed_release_leaves_queue_licenses_and_revision_untouched(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # rev 1
        b = submit(client, 105, 108, "B")
        d = submit(client, 200, 210, "D")
        register(client, b)
        register(client, d)

        # Stale expected revision: the suspension (and its drain) never
        # happens.
        r = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 0},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["effective_application_ids"] == [a]
        assert current["waitlist_application_ids"] == [b, d]
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"

    def test_history_restores_auto_approvals_revision_by_revision(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        d = submit(client, 200, 210, "D")
        register(client, b)
        register(client, d)
        client.post(f"/api/applications/{a}/revoke", json={})
        # Revisions: revoke A = 2, auto B = 3, auto D = 4.

        at2 = client.get("/api/revision/2").json()
        assert at2["revision"] == 2
        assert at2["effective_application_ids"] == []
        assert [
            x["application_id"] for x in at2["revocations"]
        ] == [a]
        # No automatic approval exists at the release revision itself.
        assert [
            x for x in at2["approvals"] if x["auto_approved"]
        ] == []

        at3 = client.get("/api/revision/3").json()
        assert at3["effective_application_ids"] == [b]
        b_record = [x for x in at3["approvals"] if x["application_id"] == b][0]
        assert b_record["auto_approved"] is True
        assert b_record["release_revision"] == 2
        assert b_record["status_as_of"] == "approved"

        at4 = client.get("/api/revision/4").json()
        assert at4["effective_application_ids"] == [b, d]
        auto_records = [x for x in at4["approvals"] if x["auto_approved"]]
        assert [(x["application_id"], x["revision"]) for x in auto_records] == [
            (b, 3),
            (d, 4),
        ]

    def test_revision_does_not_move_on_register_and_cancel(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        before = client.get("/api/config/guard-band").json()["current_revision"]
        b = submit(client, 105, 108, "B")
        register(client, b)
        cancel(client, b)
        register(client, b)
        after = client.get("/api/config/guard-band").json()["current_revision"]
        assert before == after == 1
