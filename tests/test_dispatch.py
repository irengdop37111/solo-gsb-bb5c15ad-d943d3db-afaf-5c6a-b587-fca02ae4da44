from datetime import datetime, timedelta, timezone
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store
from app.dispatch import board


client = TestClient(app)


def iso(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).isoformat()


def real_clock():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clear():
    store._halls.clear()
    store._epochs.clear()
    board.reset()
    yield
    store._halls.clear()
    store._epochs.clear()
    board.reset()
    board.clock = real_clock


def lamp(lid, default=0):
    return {"id": lid, "default_level": default}


def scene(sid, start, end, items, priority="normal", **kw):
    return {"id": sid, "priority": priority, "start_at": start, "end_at": end, "items": items, **kw}


def item(lid, level, fade=0):
    return {"lamp_id": lid, "level": level, "fade_seconds": fade}


# Two channels x two due commands each (all scheduled in the past).
HALL = {
    "id": "hall-d",
    "lamps": [lamp("L1"), lamp("L2")],
    "scenes": [
        scene("s1", iso(2026, 10, 5, 8), iso(2026, 10, 5, 12), [item("L1", 50), item("L2", 40)]),
        scene("s2", iso(2026, 10, 5, 12), iso(2026, 10, 5, 20), [item("L1", 80), item("L2", 70)]),
    ],
}


def put_hall(body=HALL):
    return client.put(f"/halls/{body['id']}", json=body)


def claim(hid="hall-d", **kw):
    return client.post(f"/halls/{hid}/dispatch/claim", json=kw)


def ack(hid, task_id, token):
    return client.post(f"/halls/{hid}/dispatch/ack", json={"task_id": task_id, "lease_token": token})


def claim_one(hid="hall-d", channel="L1", lease=3600):
    r = claim(hid, channel=channel, limit=1, lease_seconds=lease)
    assert r.status_code == 200, r.text
    tasks = r.json()["tasks"]
    assert len(tasks) == 1
    return tasks[0]


# --------------------------------------------------------------------------- #
# Claim shape and per-channel ordering
# --------------------------------------------------------------------------- #
def test_claim_returns_stable_task_id_version_command_and_lease():
    put_hall()
    r = claim(channel="L1", limit=5, lease_seconds=60)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hall_id"] == "hall-d"
    assert body["version"] == 1
    assert body["count"] == 1
    t = body["tasks"][0]
    assert t["task_id"] == "hall-d:g1:v1:L1:0000"
    assert t["hall_id"] == "hall-d" and t["version"] == 1 and t["channel"] == "L1"
    assert t["command"]["scene_id"] == "s1" and t["command"]["to_level"] == 50
    assert t["lease_token"]
    expires = datetime.fromisoformat(t["lease_expires_at"])
    assert 55 <= (expires - real_clock()).total_seconds() <= 61
    # the issued command is exactly the compiled timeline command
    timeline = client.get("/halls/hall-d/timeline").json()["timeline"]
    assert t["command"] == timeline["L1"][0]


def test_claim_without_channel_fans_out_one_task_per_channel_up_to_limit():
    put_hall()
    r = claim(limit=10, lease_seconds=60)
    tasks = r.json()["tasks"]
    assert [t["channel"] for t in tasks] == ["L1", "L2"]  # sorted, one per channel
    assert all(t["command"]["scene_id"] == "s1" for t in tasks)


def test_claim_limit_caps_the_total():
    put_hall()
    r = claim(limit=1, lease_seconds=60)
    body = r.json()
    assert body["count"] == 1
    assert body["tasks"][0]["channel"] == "L1"


def test_next_task_not_claimable_until_previous_acknowledged():
    put_hall()
    t1 = claim_one()
    # previous task unacknowledged: the next one is withheld
    assert claim(channel="L1", limit=1, lease_seconds=3600).json()["tasks"] == []
    r = ack("hall-d", t1["task_id"], t1["lease_token"])
    assert r.status_code == 200 and r.json()["acknowledged"] is True
    # after the ack the following command of the channel is delivered
    t2 = claim_one()
    assert t2["task_id"] != t1["task_id"]
    assert t2["command"]["scene_id"] == "s2"


def test_channel_exhausted_once_every_command_is_acknowledged():
    put_hall()
    for expected in ("s1", "s2"):
        t = claim_one()
        assert t["command"]["scene_id"] == expected
        assert ack("hall-d", t["task_id"], t["lease_token"]).status_code == 200
    assert claim(channel="L1", limit=1, lease_seconds=3600).json()["tasks"] == []


def test_commands_not_yet_due_are_withheld():
    hall = {
        "id": "hall-d",
        "lamps": [lamp("L1")],
        "scenes": [
            scene("past", iso(2026, 10, 5, 8), iso(2026, 10, 5, 9), [item("L1", 10)]),
            scene("future", iso(2099, 1, 1, 0), iso(2099, 1, 1, 1), [item("L1", 20)]),
        ],
    }
    put_hall(hall)
    tasks = claim(channel="L1", limit=5, lease_seconds=60).json()["tasks"]
    assert [t["command"]["scene_id"] for t in tasks] == ["past"]
    assert ack("hall-d", tasks[0]["task_id"], tasks[0]["lease_token"]).status_code == 200
    # head of the queue is the future command -> not due -> nothing claimable
    assert claim(channel="L1", limit=5, lease_seconds=60).json()["tasks"] == []


# --------------------------------------------------------------------------- #
# Lease semantics
# --------------------------------------------------------------------------- #
def test_active_lease_never_reissues_the_task():
    put_hall()
    claim_one(lease=3600)
    assert claim(channel="L1", limit=1, lease_seconds=3600).json()["tasks"] == []


def test_expired_lease_reclaims_same_task_with_fresh_token():
    put_hall()
    t1 = claim_one(lease=0)  # expires immediately
    t2 = claim_one(lease=3600)
    assert t2["task_id"] == t1["task_id"]  # stable task identity
    assert t2["lease_token"] != t1["lease_token"]  # fresh token
    # and the fresh lease is again exclusive
    assert claim(channel="L1", limit=1, lease_seconds=3600).json()["tasks"] == []


# --------------------------------------------------------------------------- #
# Acknowledgement semantics
# --------------------------------------------------------------------------- #
def test_ack_requires_the_current_token_and_conflict_changes_nothing():
    put_hall()
    t = claim_one()
    r = ack("hall-d", t["task_id"], "forged-token")
    assert r.status_code == 409
    assert r.json()["code"] == "token_conflict"
    # state unchanged: still leased, not re-issued, correct token still works
    assert claim(channel="L1", limit=1, lease_seconds=3600).json()["tasks"] == []
    assert ack("hall-d", t["task_id"], t["lease_token"]).status_code == 200


def test_old_token_conflicts_after_reclaim():
    put_hall()
    t1 = claim_one(lease=0)
    t2 = claim_one(lease=3600)
    r = ack("hall-d", t1["task_id"], t1["lease_token"])  # superseded token
    assert r.status_code == 409 and r.json()["code"] == "token_conflict"
    assert ack("hall-d", t2["task_id"], t2["lease_token"]).status_code == 200


def test_duplicate_ack_is_idempotent_and_advances_cursor_once():
    put_hall()
    t = claim_one()
    r1 = ack("hall-d", t["task_id"], t["lease_token"])
    r2 = ack("hall-d", t["task_id"], t["lease_token"])
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["duplicate"] is False
    assert r2.json()["duplicate"] is True
    # the cursor moved exactly one command forward
    assert claim_one()["command"]["scene_id"] == "s2"
    # a different token against the already-acked task is a conflict
    r3 = ack("hall-d", t["task_id"], "other-token")
    assert r3.status_code == 409 and r3.json()["code"] == "token_conflict"


def test_ack_unknown_task_is_404():
    put_hall()
    r = ack("hall-d", "hall-d:g1:v1:L1:0099", "x")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_task"
    # a task issued by another hall is unknown on this path
    t = claim_one()
    r = ack("other-hall", t["task_id"], t["lease_token"])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_task"


def test_claim_validation_and_unknown_channel():
    put_hall()
    assert claim(channel="L1", limit=0, lease_seconds=1).status_code == 422
    assert claim(channel="L1", limit=1).status_code == 422  # lease_seconds required
    assert claim(channel="L1", lease_seconds=1).status_code == 422  # limit required
    r = claim(channel="NOPE", limit=1, lease_seconds=1)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_channel"
    r = claim("ghost-hall", limit=1, lease_seconds=1)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "hall_not_found"


# --------------------------------------------------------------------------- #
# Re-publish / delete invalidation
# --------------------------------------------------------------------------- #
def test_republish_invalidates_unacked_tasks_and_old_lease_reports_version_expired():
    put_hall()  # version 1
    t = claim_one()
    put_hall()  # version 2
    r = ack("hall-d", t["task_id"], t["lease_token"])
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "version_expired"
    assert body["task_version"] == 1 and body["current_version"] == 2


def test_new_version_does_not_reissue_commands_from_before_its_publish_time():
    put_hall()  # v1: commands of 2026-10-05 are claimable
    claim_one()
    put_hall()  # v2, same content, published now
    r = claim(channel="L1", limit=5, lease_seconds=60)
    body = r.json()
    assert body["version"] == 2
    assert body["tasks"] == []  # every command predates the v2 publish time


def test_new_version_serves_commands_scheduled_after_its_publish_time():
    start = real_clock() + timedelta(hours=1)
    hall = {
        "id": "hall-f",
        "lamps": [lamp("L1")],
        "scenes": [scene("f1", start.isoformat(), (start + timedelta(hours=2)).isoformat(), [item("L1", 66)])],
    }
    put_hall(hall)  # v1
    put_hall(hall)  # v2
    # move time past the command's scheduled instant: it becomes claimable
    board.clock = lambda: start + timedelta(seconds=1)
    r = claim("hall-f", channel="L1", limit=1, lease_seconds=60)
    tasks = r.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["version"] == 2 and tasks[0]["command"]["scene_id"] == "f1"
    assert ack("hall-f", tasks[0]["task_id"], tasks[0]["lease_token"]).status_code == 200


def test_delete_invalidates_unacked_tasks():
    put_hall()
    t = claim_one()
    assert client.delete("/halls/hall-d").status_code == 204
    r = ack("hall-d", t["task_id"], t["lease_token"])
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "version_expired" and body["current_version"] is None
    # claims on a deleted hall are 404
    assert claim(channel="L1", limit=1, lease_seconds=1).status_code == 404


def test_delete_then_recreate_still_invalidates_the_old_generation():
    put_hall()  # v1, generation 1
    t = claim_one()
    assert client.delete("/halls/hall-d").status_code == 204
    put_hall()  # v1 again, but a new generation
    # the old lease must not confirm against the coincidentally equal version
    r = ack("hall-d", t["task_id"], t["lease_token"])
    assert r.status_code == 409 and r.json()["code"] == "version_expired"
    # and the new generation issues its commands fresh, from the top
    t2 = claim_one()
    assert t2["task_id"] != t["task_id"]
    assert t2["command"]["scene_id"] == "s1"
    assert ack("hall-d", t2["task_id"], t2["lease_token"]).status_code == 200


def test_failed_publish_does_not_change_the_queue():
    put_hall()  # v1
    t = claim_one()
    # failed single-hall publish (invalid orchestration)
    bad = {
        "id": "hall-d",
        "lamps": [lamp("L1")],
        "scenes": [scene("x", iso(2026, 10, 5, 8), iso(2026, 10, 5, 9), [item("GHOST", 1)])],
    }
    assert client.put("/halls/hall-d", json=bad).status_code == 422
    # failed batch publish (stale expected version)
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [{"hall_id": "hall-d", "expected_version": 99, "orchestration": HALL}]},
    )
    assert r.status_code == 409
    # the queue is untouched: version 1 lease still acknowledges
    assert ack("hall-d", t["task_id"], t["lease_token"]).status_code == 200
    assert client.get("/halls/hall-d").json()["version"] == 1


def test_batch_publish_follows_the_same_invalidation_rules():
    put_hall()  # v1
    t = claim_one()
    r = client.post(
        "/halls/batch-publish",
        json={"halls": [{"hall_id": "hall-d", "expected_version": 1, "orchestration": HALL}]},
    )
    assert r.status_code == 200 and r.json()["published"][0]["version"] == 2
    r = ack("hall-d", t["task_id"], t["lease_token"])
    assert r.status_code == 409 and r.json()["code"] == "version_expired"
    # and the re-published version does not re-issue pre-publish commands
    assert claim(channel="L1", limit=5, lease_seconds=60).json()["tasks"] == []


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def _run_parallel(count, fn):
    barrier = threading.Barrier(count)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        out = fn()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_claims_issue_the_task_exactly_once():
    put_hall()
    results = _run_parallel(8, lambda: claim(channel="L1", limit=1, lease_seconds=3600).json()["tasks"])
    issued = [ts[0] for ts in results if ts]
    assert len(issued) == 1
    assert ack("hall-d", issued[0]["task_id"], issued[0]["lease_token"]).status_code == 200


def test_concurrent_duplicate_acks_are_all_idempotent():
    put_hall()
    t = claim_one()
    codes = _run_parallel(8, lambda: ack("hall-d", t["task_id"], t["lease_token"]).status_code)
    assert codes.count(200) == 8
    # cursor advanced exactly once despite eight acknowledgements
    assert claim_one()["command"]["scene_id"] == "s2"


def test_concurrent_ack_and_publish_never_confirm_across_versions():
    put_hall()  # v1
    t = claim_one()
    barrier = threading.Barrier(2)
    outcomes = {}

    def acker():
        barrier.wait()
        outcomes["ack"] = ack("hall-d", t["task_id"], t["lease_token"]).status_code

    def publisher():
        barrier.wait()
        outcomes["pub"] = client.put("/halls/hall-d", json=HALL).status_code

    threads = [threading.Thread(target=acker), threading.Thread(target=publisher)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert outcomes["pub"] == 200
    # the ack either serialised before the publish (200) or after (409
    # version_expired) — never a cross-version confirmation
    assert outcomes["ack"] in (200, 409)
    # whichever won, the version-1 task is unreachable afterwards
    r = ack("hall-d", t["task_id"], t["lease_token"])
    assert r.status_code == 409 and r.json()["code"] == "version_expired"
    assert client.get("/halls/hall-d").json()["version"] == 2
