"""End-to-end tests for two-revision comparison snapshots."""
from factories import make_payload


def submit(client, low, high, name="Event"):
    r = client.post(
        "/api/applications", json=make_payload(freq=(low, high), name=name)
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def approve(client, application_id, expected_revision):
    r = client.post(
        f"/api/applications/{application_id}/approve",
        json={"expected_revision": expected_revision},
    )
    assert r.status_code == 200, r.text
    return r.json()


def suspend(client, application_id, expected_revision):
    r = client.post(
        f"/api/applications/{application_id}/suspend",
        json={"expected_revision": expected_revision, "reason": "outage"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, application_id, expected_revision):
    r = client.post(
        f"/api/applications/{application_id}/resume",
        json={"expected_revision": expected_revision, "reason": "fixed"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def waitlist(client, application_id):
    r = client.post(
        f"/api/applications/{application_id}/waitlist",
        json={"reason": "standby"},
    )
    assert r.status_code == 200, r.text


def compare(client, base_revision, target_revision, application_id=None):
    params = {
        "base_revision": base_revision,
        "target_revision": target_revision,
    }
    if application_id is not None:
        params["application_id"] = application_id
    r = client.get("/api/revision/compare", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def event_refs(body):
    return [(event["revision"], event["type"], event.get("application_id"))
            for event in body["differences"]]


class TestRevisionComparison:
    def test_compares_band_sets_status_changes_and_auto_approval(self, client):
        a = submit(client, 100, 110, "A")
        standby = submit(client, 105, 108, "standby")
        approve(client, a, 0)  # revision 1
        waitlist(client, standby)

        suspend(client, a, 1)  # revision 2, standby auto-approved revision 3

        config = client.post(
            "/api/config/guard-band",
            json={"guard_band": 1.0, "expected_revision": 3},
        )
        assert config.status_code == 200, config.text  # revision 4

        before = client.get("/api/revision/current").json()
        body = compare(client, 1, 4)
        after = client.get("/api/revision/current").json()
        assert before == after

        assert body["direction"] == "forward"
        assert body["guard_band"] == {"base": 2.0, "target": 1.0, "changed": True}
        assert body["effective_application_ids"] == {
            "base": [a],
            "target": [standby],
            "added": [standby],
            "removed": [a],
        }
        assert body["suspended_application_ids"] == {
            "base": [],
            "target": [a],
            "added": [a],
            "removed": [],
        }
        assert body["status_changes"] == [
            {
                "revision": 2,
                "application_id": a,
                "from_status": "approved",
                "to_status": "suspended",
            },
            {
                "revision": 3,
                "application_id": standby,
                "from_status": "pending",
                "to_status": "approved",
            },
        ]
        assert event_refs(body) == [
            (2, "suspension", a),
            (3, "approval", standby),
            (4, "guard_band_change", None),
        ]

        auto_approval = body["approvals"][0]
        assert auto_approval["revision"] == 3
        assert auto_approval["application_id"] == standby
        assert auto_approval["auto_approved"] is True
        assert auto_approval["release_revision"] == 2
        assert auto_approval["guard_band"] == 2.0

        guard_change = body["guard_band_changes"][0]
        assert guard_change["revision"] == 4
        assert guard_change["previous_guard_band"] == 2.0
        assert guard_change["guard_band"] == 1.0

    def test_resumption_and_other_approval_are_replayed_in_order(self, client):
        a = submit(client, 100, 110, "A")
        distant = submit(client, 130, 140, "distant")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2
        approve(client, distant, 2)  # 3
        resume(client, a, 3)  # 4

        body = compare(client, 2, 4)
        assert event_refs(body) == [
            (3, "approval", distant),
            (4, "resumption", a),
        ]
        assert body["status_changes"] == [
            {
                "revision": 3,
                "application_id": distant,
                "from_status": "pending",
                "to_status": "approved",
            },
            {
                "revision": 4,
                "application_id": a,
                "from_status": "suspended",
                "to_status": "approved",
            },
        ]
        assert body["effective_application_ids"]["target"] == [a, distant]
        assert body["suspended_application_ids"]["target"] == []

    def test_application_filter_scopes_license_events_but_keeps_band_events(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        other = submit(client, 130, 140, "other")
        approve(client, a, 0)  # 1
        approve(client, other, 1)  # 2
        suspend(client, a, 2)  # 3

        body = compare(client, 0, 3, application_id=a)
        assert body["application_id"] == a
        assert body["base"]["application_status"] == "pending"
        assert body["target"]["application_status"] == "suspended"
        assert body["target"]["is_effective"] is False
        assert body["target"]["is_suspended"] is True
        assert event_refs(body) == [
            (1, "approval", a),
            (3, "suspension", a),
        ]
        assert body["status_changes"] == [
            {
                "revision": 1,
                "application_id": a,
                "from_status": "pending",
                "to_status": "approved",
            },
            {
                "revision": 3,
                "application_id": a,
                "from_status": "approved",
                "to_status": "suspended",
            },
        ]

    def test_equal_revisions_return_no_differences(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)

        body = compare(client, 1, 1)
        assert body["differences"] == []
        assert body["status_changes"] == []
        assert body["effective_application_ids"] == {
            "base": [a],
            "target": [a],
            "added": [],
            "removed": [],
        }

    def test_backward_comparison_keeps_named_base_and_target_views(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)

        body = compare(client, 1, 0)
        assert body["direction"] == "backward"
        assert body["base_revision"] == 1
        assert body["target_revision"] == 0
        assert event_refs(body) == [
            (1, "approval", a),
        ]
        assert body["base"]["effective_application_ids"] == [a]
        assert body["target"]["effective_application_ids"] == []

    def test_invalid_revisions_and_unknown_application_are_rejected(self, client):
        r = client.get(
            "/api/revision/compare",
            params={"base_revision": 0, "target_revision": 1},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "revision_out_of_range"
        assert r.json()["error"]["details"] == {
            "current_revision": 0,
            "out_of_range_revisions": [1],
        }

        r = client.get(
            "/api/revision/compare",
            params={"base_revision": -1, "target_revision": 0},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "revision_out_of_range"

        r = client.get(
            "/api/revision/compare",
            params={
                "base_revision": 0,
                "target_revision": 0,
                "application_id": 999999,
            },
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"
