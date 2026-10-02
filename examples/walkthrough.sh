#!/usr/bin/env bash
# End-to-end curl walkthrough for the Temporary Event Radio Spectrum API.
# Usage: ./examples/walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. invalid submission (422, whole request rejected) =="
post /api/applications '{
  "name":"bad",
  "circle":{"cx":0,"cy":0,"radius":-5},
  "frequency":{"low":200,"high":100},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J

echo "== 2. submit A =="
A=$(post /api/applications '{
  "name":"Festival A",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T12:00:00Z"}
}')
echo "$A" | J
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 3. preview A =="
post "/api/applications/$A_ID/preview" | J

echo "== 4. approve A at revision 0 =="
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 5. submit B (1 MHz gap < 2 MHz guard band) =="
B=$(post /api/applications '{
  "name":"Marathon B",
  "circle":{"cx":100,"cy":0,"radius":1000},
  "frequency":{"low":111,"high":112},
  "time_window":{"start":"2026-10-01T09:00:00Z","end":"2026-10-01T11:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$B_ID/preview" | J

echo "== 6. stale approval (expected 0, current 1) =="
post "/api/applications/$B_ID/approve" '{"expected_revision":0}' | J || true

echo "== 7. current revision but interference =="
post "/api/applications/$B_ID/approve" '{"expected_revision":1}' | J || true

echo "== 8. revoke A =="
post "/api/applications/$A_ID/revoke" '{"reason":"postponed"}' | J

echo "== 9. approve B at revision 2 =="
post "/api/applications/$B_ID/approve" '{"expected_revision":2}' | J

echo "== 10. history snapshots =="
curl -s "$BASE/api/revision/1" | J
curl -s "$BASE/api/revision/2" | J
curl -s "$BASE/api/revision/current" | J

echo "== 11. submit three more devices for one event (disjoint bands) =="
mk() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}"; }
C=$(mk "Device C" 200 210); C_ID=$(echo "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
D=$(mk "Device D" 300 310); D_ID=$(echo "$D" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
E=$(mk "Device E" 400 410); E_ID=$(echo "$E" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "ids: $C_ID $D_ID $E_ID"

echo "== 12. batch approve at revision 3, input order D,C,E -> revisions 4,5,6 =="
post /api/applications/batch-approve \
  "{\"expected_revision\":3,\"application_ids\":[$D_ID,$C_ID,$E_ID]}" | J

echo "== 13. snapshot as of the second item (revision 5) has D and C effective =="
curl -s "$BASE/api/revision/5" | J

echo "== 14. failing batch: two members clash with each other -> whole group rejected =="
F=$(mk "Device F" 500 510); F_ID=$(echo "$F" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G=$(mk "Device G" 505 515); G_ID=$(echo "$G" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
H=$(mk "Device H" 600 610); H_ID=$(echo "$H" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$G_ID,$H_ID]}" | J || true
echo "revision is still 6 and all three are pending:"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print("current_revision =", json.load(sys.stdin)["current_revision"])'

echo "== 15. malformed batches (duplicate ids / missing id) =="
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$F_ID]}" | J || true
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[999999]}" | J || true

echo "== 16. guard band: current config (live value, initial value, revision) =="
curl -s "$BASE/api/config/guard-band" | J

echo "== 17. preview a wider band (100): pairs among the effective grants appear =="
echo "    (devices C/D/E sit 90 MHz apart; B's time window does not overlap theirs)"
post /api/config/guard-band/preview '{"guard_band":100}' | J

echo "== 18. confirming it is refused (409 interference); nothing changes =="
post /api/config/guard-band '{"guard_band":100,"expected_revision":6}' | J || true
curl -s "$BASE/api/config/guard-band" | J

echo "== 19. tighten to 1.0: accepted, exactly one new revision (7) =="
post /api/config/guard-band '{"guard_band":1.0,"expected_revision":6}' | J

echo "== 20. the change shares revision arbitration: stale confirms/approvals lose =="
post /api/config/guard-band '{"guard_band":0.5,"expected_revision":6}' | J || true
post "/api/applications/$F_ID/approve" '{"expected_revision":6}' | J || true

echo "== 21. invalid values are rejected with 422 (no revision consumed) =="
post /api/config/guard-band '{"guard_band":-5,"expected_revision":7}' | J || true

echo "== 22. history restores the band in force at each revision =="
curl -s "$BASE/api/revision/6" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"])'
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"], "changes =", [(c["revision"], c["previous_guard_band"], c["guard_band"]) for c in d["guard_band_changes"]])'
echo "    approval events keep the band they were decided under (not rewritten):"
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("approval guard_bands =", [(a["revision"], a["guard_band"]) for a in d["approvals"]])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "guard_band =", d["guard_band"])'

echo "== 23. available-ranges search for pending device F (band [500,510], width 10) =="
echo "    effective grants in its 13:00-18:00 window: C [200,210], D [300,310], E [400,410]; guard band is now 1.0"
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J
echo "    forbidden by C: (200-10-1, 210+1) = (189,211); by D: (289,311); by E: (389,411)"
echo "    -> free lower bounds: [180,189], [211,289], [311,389], [411,430] (endpoints attainable)"

echo "== 24. a reported endpoint previews conflict-free; a forbidden point does not =="
SHIFTED=$(post /api/applications "{
  \"name\":\"F shifted to 211\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":211,\"high\":221},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
SHIFTED_ID=$(echo "$SHIFTED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$SHIFTED_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 211 (gap == guard): has_conflicts =", d["has_conflicts"])'
CLASH=$(post /api/applications "{
  \"name\":\"F shifted to 200\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":200,\"high\":210},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
CLASH_ID=$(echo "$CLASH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CLASH_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 200 (inside forbidden): has_conflicts =", d["has_conflicts"])'

echo "== 25. the search is non-mutating and validates its inputs =="
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":430,"candidate_low_max":180}' | J || true
post "/api/applications/999999/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"])'

echo "== 26. suspend C (temporary device outage): leaves the effective set, one revision (8) =="
post "/api/applications/$C_ID/suspend" \
  '{"expected_revision":7,"reason":"device downtime"}' | J
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'

echo "== 27. while C is suspended, a device I overlapping C's band [205,208] can be approved (9) =="
I=$(post /api/applications '{
  "name":"Device I",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":205,"high":208},
  "time_window":{"start":"2026-10-01T13:00:00Z","end":"2026-10-01T18:00:00Z"}
}')
I_ID=$(echo "$I" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$I_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("has_conflicts =", d["has_conflicts"], "(C is suspended)")'
post "/api/applications/$I_ID/approve" '{"expected_revision":8}' | J

echo "== 28. resume C re-adjudicates: clashes with I -> 409 interference, C stays suspended =="
post "/api/applications/$C_ID/resume" '{"expected_revision":9}' | J || true
post "/api/applications/$C_ID/resume" '{"expected_revision":9}' >/dev/null || true
echo "    a stale resume returns the current revision and changes nothing either:"
post "/api/applications/$C_ID/resume" '{"expected_revision":8}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"], "; C status =", [a["status"] for a in d["suspended_applications"]])'

echo "== 29. permanently revoke I (revision 10), then resume C succeeds (revision 11) =="
post "/api/applications/$I_ID/revoke" '{"reason":"obsolete"}' | J
post "/api/applications/$C_ID/resume" \
  '{"expected_revision":10,"reason":"fault cleared"}' | J

echo "== 30. history restores suspend/resume records and each effective set =="
curl -s "$BASE/api/revision/8" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 8: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"], "suspensions =", [(s["revision"], s["application_id"]) for s in d["suspensions"]])'
curl -s "$BASE/api/revision/9" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 9: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'
curl -s "$BASE/api/revision/11" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 11: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"]); print("        suspensions =", [(s["revision"], s["application_id"]) for s in d["suspensions"]], "resumptions =", [(s["revision"], s["application_id"], s["guard_band"]) for s in d["resumptions"]], "revocations =", [(s["revision"], s["application_id"]) for s in d["revocations"]])'

echo "== 31. unknown ids and illegal states are refused without consuming a revision =="
post /api/applications/999999/suspend '{"expected_revision":11}' | J || true
post "/api/applications/$H_ID/suspend" '{"expected_revision":11}' | J || true
post "/api/applications/$D_ID/resume" '{"expected_revision":11}' | J || true
post "/api/applications/$C_ID/suspend" '{"expected_revision":11}' >/dev/null
post "/api/applications/$C_ID/suspend" '{"expected_revision":12}' | J || true
post "/api/applications/$C_ID/resume" '{"expected_revision":12}' >/dev/null
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "(the rejections consumed none)")'

echo "== 32. waitlist: register pending apps while the band is busy (no revision used) =="
echo "    effective in the 13:00-18:00 window: C [200,210], D [300,310], E [400,410]; guard band is 1.0"
X=$(mk "Standby X" 205 215); X_ID=$(echo "$X" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Y=$(mk "Standby Y" 213 220); Y_ID=$(echo "$Y" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Z=$(mk "Standby Z" 550 560); Z_ID=$(echo "$Z" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$X_ID/waitlist" '{"reason":"band busy"}' | J
post "/api/applications/$Y_ID/waitlist" | J
post "/api/applications/$Z_ID/waitlist" | J
echo "ids: X=$X_ID Y=$Y_ID Z=$Z_ID"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"], "; queue =", d["waitlist_application_ids"])'

echo "== 33. duplicate / non-pending registration, lookup and cancel =="
post "/api/applications/$X_ID/waitlist" '{}' | J || true
post "/api/applications/$C_ID/waitlist" '{}' | J || true
post /api/applications/999999/waitlist '{}' | J || true
curl -s "$BASE/api/waitlist" | J
curl -s "$BASE/api/applications/$Y_ID/waitlist" | J
echo "    register F and cancel again (still no revision; application stays pending):"
post "/api/applications/$F_ID/waitlist" '{}' | J
post "/api/applications/$F_ID/waitlist/cancel" | J
curl -s "$BASE/api/applications/$F_ID" | python3 -c 'import json,sys; print("F status =", json.load(sys.stdin)["status"])'

echo "== 34. suspend C (revision 14) releases the band: queue drains in order =="
echo "    X [205,215] passes -> auto rev 15; Y [213,220] clashes with the just-approved X -> stays; Z [550,560] passes -> auto rev 16"
post "/api/applications/$C_ID/suspend" '{"expected_revision":13,"reason":"re-tune for standbys"}' | J

echo "== 35. history restores each automatic approval revision by revision =="
curl -s "$BASE/api/revision/15" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 15: effective =", d["effective_application_ids"]); print("        autos =", [(a["application_id"], a["auto_approved"], a["release_revision"]) for a in d["approvals"]])'
curl -s "$BASE/api/revision/16" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 16: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'

echo "== 36. revoke X (revision 17) releases again: Y now passes -> auto rev 18 =="
post "/api/applications/$X_ID/revoke" '{"reason":"moved away"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d[k] for k in ("revision","auto_approved_application_ids","remaining_waitlist_application_ids","final_revision")})'

echo "== 37. resume C (revision 19): ordinary resume, never drains a queue =="
post "/api/applications/$C_ID/resume" '{"expected_revision":18,"reason":"fault cleared"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("resumed revision", d["revision"], "; waitlist keys present:", "auto_approved_application_ids" in d)'
curl -s "$BASE/api/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("queue is now", d["waitlist_application_ids"], "at revision", d["current_revision"])'
