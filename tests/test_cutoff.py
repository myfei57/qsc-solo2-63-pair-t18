"""Two-phase cut-off: soft cut-off, the tail-off window, rollback, hard cut-off."""

from __future__ import annotations

import json

import pytest

from bagsort.errors import OrderingError
from bagsort.model.enums import FlightState
from bagsort.store.replay import replay
from bagsort.store.store import Store
from tests.support import check_in


def test_soft_cutoff_snapshots_the_tail_and_blocks_new_check_ins(plant, flight):
    check_in(plant, flight.id, 3)
    mark = plant.soft_cutoff(flight.id)
    assert plant.book.get(flight.id).state is FlightState.CUTOFF
    assert (mark.in_transit, mark.accepted) == (3, 3)
    assert plant.cutoff.mark(flight.id) == mark
    with pytest.raises(OrderingError):
        plant.check_in("BAG9000", flight.id)


def test_soft_cutoff_is_not_idempotent(plant, flight):
    plant.soft_cutoff(flight.id)
    with pytest.raises(OrderingError):
        plant.soft_cutoff(flight.id)


def test_soft_cutoff_rejects_a_closed_flight(plant, flight):
    plant.soft_cutoff(flight.id)
    plant.hard_cutoff(flight.id)
    with pytest.raises(OrderingError):
        plant.soft_cutoff(flight.id)


def test_tail_keeps_sorting_on_the_original_chute(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bags = check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    record = plant.push_bag(bags[0].id, "T1-CHA")
    assert record.chute_id == "T1-CHA"
    status = plant.cutoff_status(flight.id)
    assert status["in_transit"]["count"] == 1
    assert status["consistent"] is True
    assert status["tail_empty"] is False


def test_status_cross_checks_the_three_views(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bags = check_in(plant, flight.id, 3)
    plant.push_bag(bags[0].id, "T1-CHA")
    plant.soft_cutoff(flight.id)
    status = plant.cutoff_status(flight.id)
    assert status["checks"] == {
        "queue_matches_ledger": True,
        "sort_log_matches_ledger": True,
        "tail_not_growing": True,
    }
    assert status["ledger"]["accepted"] == 3
    assert status["ledger"]["by_state"]["injected"] == 2
    assert status["sort_records"] == 1
    assert status["mark"]["in_transit"] == 2
    assert status["in_transit"]["bag_ids"] == [bags[1].id, bags[2].id]


def test_hard_cutoff_requires_soft_cutoff_first(plant, flight):
    with pytest.raises(OrderingError):
        plant.hard_cutoff(flight.id)


def test_hard_cutoff_refuses_while_the_tail_is_alive(plant, flight):
    check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    with pytest.raises(OrderingError):
        plant.hard_cutoff(flight.id)


def test_hard_cutoff_closes_and_locks_the_flight(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bags = check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    for bag in bags:
        plant.push_bag(bag.id, "T1-CHA")
    closed = plant.hard_cutoff(flight.id)
    assert closed.state is FlightState.CLOSED
    assert closed.closed_at != ""
    assert plant.cutoff.mark(flight.id) is None
    assert plant.cutoff_status(flight.id)["tail_empty"] is True
    with pytest.raises(OrderingError):
        plant.check_in("BAG9001", flight.id)
    with pytest.raises(OrderingError):
        plant.reopen_flight(flight.id)


def test_draining_the_tail_settles_stragglers_before_hard_cutoff(plant, flight):
    check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    drained = plant.injector.drain_in_transit(flight.id)
    assert drained == 2
    closed = plant.hard_cutoff(flight.id)
    assert closed.state is FlightState.CLOSED


def test_reopen_rolls_back_without_duplicate_acceptance(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bags = check_in(plant, flight.id, 2)
    plant.push_bag(bags[0].id, "T1-CHA")
    plant.soft_cutoff(flight.id)
    reopened = plant.reopen_flight(flight.id)
    assert reopened.state is FlightState.OPEN
    assert plant.cutoff.mark(flight.id) is None
    # The rollback touched nothing already accepted.
    assert len(plant.book.list_bags_for(flight.id)) == 2
    assert len(plant.sorter.records()) == 1
    # The bag still in transit sorts exactly once after the re-open.
    assert plant.retry_bag(bags[1].id) is True
    assert plant.retry_bag(bags[1].id) is False
    assert len(plant.sorter.records()) == 2
    # A second soft cut-off takes a fresh baseline instead of double counting.
    mark = plant.soft_cutoff(flight.id)
    assert (mark.in_transit, mark.accepted) == (0, 2)


def test_reopen_requires_soft_cutoff(plant, flight):
    with pytest.raises(OrderingError):
        plant.reopen_flight(flight.id)


def test_cutoff_window_survives_replay(plant, flight):
    check_in(plant, flight.id, 2)
    mark = plant.soft_cutoff(flight.id)
    rebuilt = replay(plant.store.entries())
    assert rebuilt.cutoffs[flight.id] == mark
    assert rebuilt.flights[flight.id].state is FlightState.CUTOFF


def test_cutoff_window_survives_a_snapshot_restart(plant, flight):
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id)
    plant.store.persist()
    restored = Store.open(plant.store.path)
    assert restored.state().cutoffs[flight.id].in_transit == 1
    assert restored.state().flights[flight.id].state is FlightState.CUTOFF


def test_audit_trail_covers_every_transition_in_order(plant, flight):
    plant.soft_cutoff(flight.id)
    plant.reopen_flight(flight.id)
    plant.soft_cutoff(flight.id)
    plant.hard_cutoff(flight.id)
    review = plant.cutoff_review(flight.id)
    assert [entry.kind for entry in review] == [
        "flight_cutoff_soft",
        "flight_reopened",
        "flight_cutoff_soft",
        "flight_cutoff_hard",
    ]
    # Each entry carries the numbers an auditor steps through, one by one.
    assert review[0].detail["in_transit"] == 0
    assert review[0].detail["accepted"] == 0
    assert review[-1].detail["closed_at"] != ""
    assert plant.audit.verify() is True


def test_remove_flight_clears_its_cutoff_mark(plant, flight):
    plant.soft_cutoff(flight.id)
    plant.book.remove(flight.id)
    assert plant.store.state().cutoffs == {}


def _call(server, method: str, path: str, payload=None):
    response = server.call(method, path, payload)
    if response.content_type.startswith("application/json"):
        return response.status, json.loads(response.body)
    return response.status, response.body.decode("utf-8")


def test_cutoff_console_endpoints(server):
    status, flight = _call(
        server, "POST", "/api/flights", {"code": "CA1234", "carousel": "CR1", "chute": "A"}
    )
    assert status == 201
    flight_id = flight["id"]
    _call(server, "POST", "/api/bags/checkin", {"barcode": "7001", "flight_id": flight_id})

    status, payload = _call(server, "POST", f"/api/flights/{flight_id}/cutoff")
    assert status == 200
    assert payload["flight"]["state"] == "cutoff"
    assert payload["mark"]["in_transit"] == 1

    # New check-ins are refused while the tail drains.
    assert _call(server, "POST", "/api/bags/checkin", {"barcode": "7002", "flight_id": flight_id})[0] == 409
    # Hard cut-off is refused while one bag is still on the belt.
    assert _call(server, "POST", f"/api/flights/{flight_id}/hard-cutoff")[0] == 409

    status, view = _call(server, "GET", f"/api/flights/{flight_id}/cutoff-status")
    assert status == 200
    assert view["consistent"] is True
    assert view["in_transit"]["count"] == 1

    # Settle the tail on the original chute, then hard cut-off succeeds.
    assert _call(server, "POST", "/api/sort", {"bag_id": "T1-7001", "chute_id": "T1-CHA"})[0] == 201
    status, closed = _call(server, "POST", f"/api/flights/{flight_id}/hard-cutoff")
    assert status == 200
    assert closed["state"] == "closed"

    status, review = _call(server, "GET", f"/api/flights/{flight_id}/cutoff-review")
    assert status == 200
    assert [entry["kind"] for entry in review] == ["flight_cutoff_soft", "flight_cutoff_hard"]

    # A rolled-back flight accepts bags again, then re-enters the window.
    status, other = _call(server, "POST", "/api/flights", {"code": "MU5678"})
    assert status == 201
    _call(server, "POST", f"/api/flights/{other['id']}/cutoff")
    status, reopened = _call(server, "POST", f"/api/flights/{other['id']}/open")
    assert status == 200
    assert reopened["state"] == "open"
    assert _call(server, "POST", "/api/bags/checkin", {"barcode": "7003", "flight_id": other["id"]})[0] == 201
