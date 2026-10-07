"""Two phase cut-off: soft wind-down first, hard stop only after reconciliation.

The old cut-off was one move: at the deadline the desk stopped accepting while
people were still queued at it. The cut-off is now two moves apart:

* :meth:`CutoffControl.soft_cutoff` keeps the desk accepting but freezes the
  downstream flow (no new transfer commands) and opens a *cut-off ticket*. The
  ticket lists every bag already in transit plus every bag accepted while the
  soft cut-off runs, so the outstanding wind-down is a query, not a guess.
* :meth:`CutoffControl.hard_cutoff` reconciles the flight state, the check-in
  ledger and the in-transit queue and only then flips the flight to the hard
  ``cutoff`` state, after which the desk accepts nothing.

A soft cut-off whose wind-down does not finish can be :meth:`reopen`-ed. The
ticket is then marked withdrawn (never deleted), and the desk's idempotency
guard means a barcode already committed cannot be accepted a second time.

Chute policy is decided up front. ``original`` keeps the flight's own chute --
free, but late bags mix with the normal sort. ``winddown`` diverts the
wind-down onto a dedicated finishing chute: faster and isolated, but it costs
the lane, two extra assignments (over and back) and manual re-handling of every
bag that used it. The chosen policy and its cost are recorded on the ticket.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import OrderingError, ValidationError
from bagsort.model.entities import Bag, CutoffTicket, Flight
from bagsort.model.enums import BagState, FlightState
from bagsort.model.errors import unknown_flight
from bagsort.model.validate import is_soft_cutoff_state
from bagsort.store.store import Store

POLICY_ORIGINAL = "original"
POLICY_WINDDOWN = "winddown"
POLICIES = (POLICY_ORIGINAL, POLICY_WINDDOWN)

# States that mean "this bag has reached the end of the wind-down".
SETTLED_STATES = (
    BagState.SORTED,
    BagState.RECHECKED,
    BagState.LOADED,
    BagState.DIVERTED,
)


class CutoffControl:
    """Owns the soft -> hard cut-off lifecycle of every flight."""

    def __init__(self, store: Store, book: object, chutes: object, clock: Clock) -> None:
        self._store = store
        self._book = book
        self._chutes = chutes
        self._clock = clock

    # -- queries -----------------------------------------------------------

    def ticket(self, flight_id: str) -> CutoffTicket | None:
        """The latest ticket for the flight, of any status."""

        tickets = self._tickets(flight_id)
        return tickets[-1] if tickets else None

    def open_ticket(self, flight_id: str) -> CutoffTicket | None:
        for ticket in reversed(self._tickets(flight_id)):
            if ticket.status == "open":
                return ticket
        return None

    def require_ticket(self, flight_id: str) -> CutoffTicket:
        ticket = self.open_ticket(flight_id)
        if ticket is None:
            raise OrderingError(f"flight {flight_id!r} has no open cut-off ticket")
        return ticket

    def has_accepted(self, flight_id: str, bag_id: str) -> bool:
        """Whether a bag already appeared on any cut-off attempt of this flight.

        This is the no-duplicate-acceptance guard consulted after a soft
        cut-off has been walked back: the bag the wind-down already knew must
        not be accepted a second time.
        """

        return any(bag_id in ticket.wind_down for ticket in self._tickets(flight_id))

    def events(self, flight_id: str | None = None) -> list[dict]:
        """The item-by-item cut-off trail, oldest first."""

        events = self._store.state().cutoff_events
        if flight_id is None:
            return list(events)
        return [event for event in events if str(event.get("flight_id")) == flight_id]

    def _tickets(self, flight_id: str) -> list[CutoffTicket]:
        prefix = f"{flight_id}#"
        tickets = [
            ticket
            for key, ticket in self._store.state().cutoff_tickets.items()
            if key.startswith(prefix)
        ]
        return sorted(tickets, key=lambda ticket: ticket.attempt)

    def outstanding(self, flight_id: str) -> list[str]:
        """Wind-down members still short of a terminal state, queue ordered."""

        ticket = self.open_ticket(flight_id)
        if ticket is None:
            return []
        bags = self._store.state().bags
        return [
            bag_id
            for bag_id in ticket.wind_down
            if bag_id not in bags or bags[bag_id].state not in SETTLED_STATES
        ]

    def in_transit(self, flight_id: str) -> int:
        """Live in-transit count watched between the two cut-off phases."""

        return len(self._queue(flight_id))

    def reconcile(self, flight_id: str) -> dict:
        """Cross-check flight state, check-in ledger, ticket and live queue.

        The three ledgers must tell the same story: the flight is in its soft
        cut-off, an open ticket exists, every bag on the ticket is a committed
        bag of this flight, and the live in-transit queue is exactly the
        outstanding ticket members and is empty.
        """

        state = self._store.state()
        flight = self._flight(flight_id)
        ticket = self.open_ticket(flight_id)
        queue = [bag.id for bag in self._queue(flight_id)]
        ledger = [bag for bag in state.bags.values() if bag.flight_id == flight_id]
        ledger_ids = {bag.id for bag in ledger}
        outstanding = self.outstanding(flight_id)
        settled = len(ticket.wind_down) - len(outstanding) if ticket else 0

        checks: dict[str, bool] = {}
        checks["state_soft_cutoff"] = is_soft_cutoff_state(flight.state)
        checks["ticket_open"] = ticket is not None and ticket.status == "open"
        checks["winddown_members_on_ledger"] = (
            ticket is not None and set(ticket.wind_down) <= ledger_ids
        )
        checks["queue_matches_outstanding"] = set(queue) == set(outstanding)
        checks["nothing_outstanding"] = not outstanding
        checks["settled_bags_accounted_for"] = (
            ticket is None
            or all(
                bag_id in state.bags and state.bags[bag_id].state in SETTLED_STATES
                for bag_id in ticket.settled
            )
        )
        return {
            "flight_id": flight_id,
            "state": flight.state.value,
            "attempt": ticket.attempt if ticket else 0,
            "policy": ticket.policy if ticket else "",
            "ticket_status": ticket.status if ticket else "",
            "checkin_ledger_total": len(ledger),
            "wind_down_total": len(ticket.wind_down) if ticket else 0,
            "settled": settled,
            "outstanding": list(outstanding),
            "in_transit_live": len(queue),
            "in_transit_queue": list(queue),
            "checks": checks,
            "consistent": all(checks.values()),
        }

    # -- phase moves -------------------------------------------------------

    def soft_cutoff(
        self,
        flight_id: str,
        policy: str = POLICY_ORIGINAL,
        winddown_chute_id: str = "",
        note: str = "",
    ) -> CutoffTicket:
        """Stop issuing new downstream commands while the desk keeps accepting."""

        flight = self._book.require_open(flight_id)
        if is_soft_cutoff_state(flight.state):
            raise OrderingError(f"flight {flight_id!r} is already in soft cut-off")
        policy = (policy or POLICY_ORIGINAL).strip().lower()
        if policy not in POLICIES:
            raise ValidationError(
                f"cut-off chute policy must be one of {', '.join(POLICIES)}"
            )

        latest = self.ticket(flight_id)
        if latest is not None and latest.status == "open":
            raise OrderingError(f"flight {flight_id!r} already has an open cut-off ticket")

        assignment = self._chutes.current(flight_id)
        original_chute = assignment.chute_id if assignment else ""
        original_generation = assignment.generation if assignment else 0
        target = ""
        if policy == POLICY_WINDDOWN:
            target = (winddown_chute_id or "").strip()
            if not target:
                raise ValidationError("wind-down policy needs a finishing chute id")
            if not original_chute:
                raise OrderingError(
                    f"flight {flight_id!r} has no regular chute to divert from"
                )
            if target == original_chute:
                raise ValidationError(
                    "the finishing chute must differ from the flight's regular chute"
                )
            self._chutes.assign(flight_id, target)

        queued = [bag.id for bag in self._queue(flight_id)]
        attempt = flight.cutoff_attempt + 1
        at = self._clock.now_iso()
        ticket = CutoffTicket(
            flight_id=flight_id,
            attempt=attempt,
            policy=policy,
            winddown_chute_id=target if policy == POLICY_WINDDOWN else "",
            original_chute_id=original_chute,
            original_generation=original_generation,
            wind_down=tuple(queued),
            status="open",
            opened_at=at,
            note=note.strip(),
            cost=self._cost(policy, target, len(queued)),
        )
        updated = flight.evolved(
            state=FlightState.SOFT_CUTOFF,
            cutoff_attempt=attempt,
            cutoff_chute_policy=policy,
            cutoff_chute_id=ticket.winddown_chute_id,
        )
        with self._store.session() as session:
            session.state.flights[flight_id] = updated
            session.state.cutoff_tickets[f"{flight_id}#{attempt}"] = ticket
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record("cutoff.open", {"ticket": ticket.as_dict()}, at)
        return ticket

    def track_acceptance(self, flight_id: str, bag_id: str) -> bool:
        """Add one bag accepted during the soft cut-off to its wind-down list."""

        ticket = self.open_ticket(flight_id)
        if ticket is None:
            return False
        if bag_id in ticket.wind_down:
            return False
        at = self._clock.now_iso()
        with self._store.session() as session:
            key = f"{flight_id}#{ticket.attempt}"
            current = session.state.cutoff_tickets[key]
            updated = current.evolved(wind_down=(*current.wind_down, bag_id))
            session.state.cutoff_tickets[key] = updated
            session.record(
                "cutoff.track",
                {"flight_id": flight_id, "bag_id": bag_id, "attempt": ticket.attempt, "at": at},
                at,
            )
        return True

    def reopen(self, flight_id: str, reason: str = "") -> Flight:
        """Walk an unfinished soft cut-off back to boarding.

        The ticket is kept as ``withdrawn``; nothing is erased. If the flight
        had been put onto a finishing chute, its regular chute is restored,
        bumping the assignment generation so stale routing caches die.
        """

        flight = self._require_soft(flight_id)
        ticket = self.require_ticket(flight_id)
        if ticket.policy == POLICY_WINDDOWN and ticket.original_chute_id:
            self._chutes.assign(flight_id, ticket.original_chute_id)
        at = self._clock.now_iso()
        withdrawn = ticket.evolved(
            status="withdrawn",
            closed_at=at,
            note="; ".join(text for text in (ticket.note, reason.strip()) if text),
        )
        updated = flight.evolved(
            state=FlightState.BOARDING,
            cutoff_chute_policy="",
            cutoff_chute_id="",
        )
        with self._store.session() as session:
            session.state.flights[flight_id] = updated
            session.state.cutoff_tickets[f"{flight_id}#{ticket.attempt}"] = withdrawn
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record("cutoff.update", {"ticket": withdrawn.as_dict(), "phase": "reopen"}, at)
        return updated

    def hard_cutoff(self, flight_id: str) -> CutoffTicket:
        """Reconcile, require an empty wind-down, then stop accepting for good."""

        flight = self._require_soft(flight_id)
        ticket = self.require_ticket(flight_id)
        report = self.reconcile(flight_id)
        if not report["consistent"]:
            failed = [name for name, ok in report["checks"].items() if not ok]
            raise OrderingError(
                f"hard cut-off for {flight_id!r} rejected, ledgers disagree: {', '.join(failed)}"
            )

        bags = self._store.state().bags
        settled = tuple(
            bag_id for bag_id in ticket.wind_down if bags[bag_id].state in SETTLED_STATES
        )
        at = self._clock.now_iso()
        cost = dict(ticket.cost)
        cost["manual_rehandling"] = len(ticket.wind_down)
        finished = ticket.evolved(status="hard", settled=settled, closed_at=at, cost=cost)
        updated = flight.evolved(
            state=FlightState.CUTOFF,
            cutoff_chute_policy="",
            cutoff_chute_id="",
        )
        with self._store.session() as session:
            session.state.flights[flight_id] = updated
            session.state.cutoff_tickets[f"{flight_id}#{ticket.attempt}"] = finished
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record("cutoff.update", {"ticket": finished.as_dict(), "phase": "hard"}, at)
        return finished

    # -- internals ---------------------------------------------------------

    def _flight(self, flight_id: str) -> Flight:
        flight = self._store.state().flights.get(flight_id)
        if flight is None:
            raise unknown_flight(flight_id)
        return flight

    def _require_soft(self, flight_id: str) -> Flight:
        flight = self._flight(flight_id)
        if not is_soft_cutoff_state(flight.state):
            raise OrderingError(f"flight {flight_id!r} is not in soft cut-off")
        return flight

    def _queue(self, flight_id: str) -> list[Bag]:
        return [
            bag
            for bag in self._store.state().bags.values()
            if bag.flight_id == flight_id and bag.state is BagState.INJECTED
        ]

    def _cost(self, policy: str, winddown_chute_id: str, queued: int) -> dict:
        if policy == POLICY_WINDDOWN:
            return {
                "lane_occupied": winddown_chute_id,
                "extra_assignments": 2,
                "manual_rehandling": queued,
                "note": (
                    "finishing chute is reserved for the whole wind-down; moving over and "
                    "restoring costs two assignment generations and every bag on it is "
                    "handled manually back into the load"
                ),
            }
        return {
            "lane_occupied": "",
            "extra_assignments": 0,
            "manual_rehandling": 0,
            "note": (
                "no dedicated lane: late bags share the flight's own chute, so a jam or a "
                "full chute blocks the wind-down and the normal sort together"
            ),
        }
