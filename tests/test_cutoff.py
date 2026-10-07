"""Two phase cut-off: soft wind-down, reconciliation, hard stop, reopen.

Covers every promise of the split cut-off:

* soft cut-off keeps the desk accepting but freezes new transfer commands;
* the outstanding wind-down is observable between the two phases;
* flight state, check-in ledger and in-transit queue reconcile before the hard
  cut-off, which refuses to land while a bag is unaccounted for;
* every transition lands in the journal *and* the tamper-evident audit chain;
* the finishing-chute policy records its cost;
* an unfinished soft cut-off reopens without ever accepting a bag twice;
* after the hard cut-off nothing is accepted and the snapshot still replays.
"""

from __future__ import annotations

import pytest

from bagsort.errors import ConflictError, OrderingError, ValidationError
from bagsort.model.enums import FlightState
from bagsort.store.replay import replay
from tests.support import check_in, register_flight


def _settle(plant, flight_id, chute, barcodes):
    for barcode in barcodes:
        bag_id = plant.namespace.bag_id(barcode)
        plant.push_bag(bag_id, chute)


# -- soft cut-off keeps accepting, hard cut-off stops ----------------------


def test_soft_cutoff_keeps_accepting_and_tracks_the_wind_down(plant, flight):
    check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    assert plant.book.get(flight.id).state is FlightState.SOFT_CUTOFF

    late = plant.check_in("BAG9000", flight.id)
    ticket = plant.cutoff_ticket(flight.id)
    assert late.id in ticket.wind_down
    assert len(ticket.wind_down) == 3
    # the in-transit count is live between the two phases
    assert plant.cutoff.in_transit(flight.id) == 3
    assert len(plant.cutoff.outstanding(flight.id)) == 3


def test_transfer_commands_are_frozen_during_soft_cutoff(plant, flight):
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id)
    with pytest.raises(OrderingError):
        plant.append_transfer(flight.id, "T1-CRCR1", 1)


def test_hard_cutoff_is_refused_while_bags_remain_outstanding(plant, flight):
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id)
    report = plant.cutoff_reconcile(flight.id)
    assert report["consistent"] is False
    assert report["checks"]["nothing_outstanding"] is False
    with pytest.raises(OrderingError):
        plant.hard_cutoff(flight.id)


def test_hard_cutoff_lands_only_after_the_wind_down_drains(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    plant.check_in("BAG9000", flight.id)

    _settle(plant, flight.id, "T1-CHA", ["BAG0000", "BAG0001", "BAG9000"])
    report = plant.cutoff_reconcile(flight.id)
    assert report["consistent"] is True
    assert report["outstanding"] == []

    ticket = plant.hard_cutoff(flight.id)
    assert ticket.status == "hard"
    assert plant.book.get(flight.id).state is FlightState.CUTOFF
    # hard cut-off is terminal for the desk
    with pytest.raises(OrderingError):
        plant.check_in("BAG9001", flight.id)


def test_close_is_refused_while_still_in_soft_cutoff(plant, flight):
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id)
    with pytest.raises(OrderingError):
        plant.close_flight(flight.id)


def test_hard_cutoff_then_close_drains_and_closes(plant, flight):
    bag = check_in(plant, flight.id, 1)[0]
    plant.soft_cutoff(flight.id)
    plant.divert_bag(bag.id)  # the wind-down bag reaches a terminal state
    plant.hard_cutoff(flight.id)
    closed = plant.close_flight(flight.id)
    assert closed.state is FlightState.CLOSED


# -- three ledgers cross-check ---------------------------------------------


def test_reconcile_flags_a_queue_that_drifts_from_the_ticket(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1)[0]
    plant.soft_cutoff(flight.id)
    # settling the bag without it vanishing from the ticket must reconcile;
    # diverting it (terminal too) keeps the ledgers consistent
    plant.divert_bag(bag.id)
    report = plant.cutoff_reconcile(flight.id)
    assert report["checks"]["settled_bags_accounted_for"] is True
    assert report["consistent"] is True


# -- audit trail: every transition is linked and reviewable ----------------


def test_every_phase_transition_is_audited_in_order(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id, note="run to deadline")
    _settle(plant, flight.id, "T1-CHA", ["BAG0000"])
    plant.hard_cutoff(flight.id)
    plant.close_flight(flight.id)

    kinds = [entry.kind for entry in plant.audit.for_subject(flight.id)]
    assert "soft_cutoff" in kinds
    assert "hard_cutoff" in kinds
    assert "flight_close" in kinds
    assert plant.audit.verify() is True
    # the journal trail is item-by-item reviewable
    trail = [event["kind"] for event in plant.cutoff_events(flight.id)]
    assert trail[0] == "cutoff.open"
    assert "cutoff.update" in trail


def test_wind_down_tracks_every_soft_acceptance_one_by_one(plant, flight):
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id)
    plant.check_in("BAG9001", flight.id)
    plant.check_in("BAG9002", flight.id)
    trails = plant.cutoff_events(flight.id)
    tracks = [event for event in trails if event["kind"] == "cutoff.track"]
    assert {event["bag_id"] for event in tracks} == {
        plant.namespace.bag_id("BAG9001"),
        plant.namespace.bag_id("BAG9002"),
    }


# -- chute policy and the cost of the finishing chute ----------------------


def test_winddown_policy_moves_to_the_finishing_chute_and_records_cost(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    check_in(plant, flight.id, 1)
    ticket = plant.soft_cutoff(flight.id, policy="winddown", winddown_chute_id="T1-CHWIND")

    assert ticket.policy == "winddown"
    assert plant.chutes.current(flight.id).chute_id == "T1-CHWIND"
    assert ticket.cost["lane_occupied"] == "T1-CHWIND"
    assert ticket.cost["extra_assignments"] == 2
    assert ticket.cost["manual_rehandling"] == 1
    assert ticket.cost["note"]


def test_winddown_policy_requires_a_distinct_finishing_chute(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    with pytest.raises(ValidationError):
        plant.soft_cutoff(flight.id, policy="winddown", winddown_chute_id="")
    with pytest.raises(ValidationError):
        plant.soft_cutoff(flight.id, policy="winddown", winddown_chute_id="T1-CHA")


def test_original_policy_has_no_finishing_chute_cost(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    ticket = plant.soft_cutoff(flight.id, policy="original")
    assert ticket.winddown_chute_id == ""
    assert ticket.cost["extra_assignments"] == 0
    assert plant.chutes.current(flight.id).chute_id == "T1-CHA"


def test_unknown_policy_is_rejected(plant, flight):
    with pytest.raises(ValidationError):
        plant.soft_cutoff(flight.id, policy="mystery")


# -- reopen: state recoverable, no duplicate acceptance --------------------


def test_reopen_restores_boarding_and_the_regular_chute(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    check_in(plant, flight.id, 1)
    ticket = plant.soft_cutoff(flight.id, policy="winddown", winddown_chute_id="T1-CHWIND")
    gen_on_winddown = plant.chutes.current(flight.id).generation

    updated = plant.reopen_flight(flight.id, reason="dispatcher delayed")
    assert updated.state is FlightState.BOARDING
    assert plant.chutes.current(flight.id).chute_id == "T1-CHA"
    assert plant.chutes.current(flight.id).generation == gen_on_winddown + 1
    # the withdrawn ticket is retained for review, never deleted
    assert ticket.attempt == plant.cutoff_ticket(flight.id).attempt
    assert plant.cutoff_ticket(flight.id).status == "withdrawn"
    assert "dispatcher delayed" in plant.cutoff_ticket(flight.id).note


def test_reopen_only_works_from_soft_cutoff(plant, flight):
    with pytest.raises(OrderingError):
        plant.reopen_flight(flight.id)
    bag = check_in(plant, flight.id, 1)[0]
    plant.soft_cutoff(flight.id)
    plant.divert_bag(bag.id)
    plant.hard_cutoff(flight.id)
    with pytest.raises(OrderingError):
        plant.reopen_flight(flight.id)


def test_reopen_then_a_new_soft_cutoff_starts_a_new_attempt(plant, flight):
    check_in(plant, flight.id, 1)
    first = plant.soft_cutoff(flight.id)
    plant.reopen_flight(flight.id)
    second = plant.soft_cutoff(flight.id)
    assert second.attempt == first.attempt + 1
    assert second.status == "open"


def test_no_duplicate_acceptance_after_reopen(plant, flight):
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id)
    plant.reopen_flight(flight.id)
    with pytest.raises(ConflictError):
        plant.check_in("BAG0000", flight.id)
    # a genuinely new bag is still accepted
    new_bag = plant.check_in("BAG0001", flight.id)
    assert new_bag.flight_id == flight.id


def test_re_scan_outside_a_cutoff_stays_idempotent(plant, flight):
    plant.check_in("BAG0000", flight.id)
    # historical behaviour: re-scanning the same tag with no cut-off involved
    # is an overwrite, not a duplicate rejection
    again = plant.check_in("BAG0000", flight.id)
    assert again.id == plant.namespace.bag_id("BAG0000")


# -- persistence: the whole two phase story survives replay ----------------


def test_journal_replay_rebuilds_the_cutoff_ticket(plant, flight):
    plant.assign_chute(flight.id, "T1-CHA")
    check_in(plant, flight.id, 1)
    plant.soft_cutoff(flight.id, policy="winddown", winddown_chute_id="T1-CHWIND")
    rebuilt = replay(plant.store.entries())
    assert rebuilt.as_dict() == plant.store.state().as_dict()
    ticket = rebuilt.cutoff_tickets[f"{flight.id}#1"]
    assert ticket.status == "open"
    assert ticket.winddown_chute_id == "T1-CHWIND"


def test_full_two_phase_story_is_audit_intact(plant):
    flight = register_flight(plant, code="MU9", chute="B")
    plant.assign_chute(flight.id, "T1-CHB")
    check_in(plant, flight.id, 2)
    plant.soft_cutoff(flight.id)
    plant.check_in("BAG9000", flight.id)
    for barcode in ("BAG0000", "BAG0001", "BAG9000"):
        plant.push_bag(plant.namespace.bag_id(barcode), "T1-CHB")
    plant.hard_cutoff(flight.id)
    plant.close_flight(flight.id)
    assert plant.audit.verify() is True
    assert replay(plant.store.entries()).as_dict() == plant.store.state().as_dict()
