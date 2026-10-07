"""Two-phase cut-off: soft cut-off first, hard cut-off once the tail has drained.

The old cut-off was a single blow: at the deadline the flight flipped state and
every passenger still queuing at the desk was refused, even though their bags
were the very bags the line was built to carry. This module splits that moment
in two.

**Soft cut-off** (``OPEN``/``BOARDING`` -> ``CUTOFF``) stops issuing new
acceptance instructions: the desk refuses new check-ins, while every bag
already accepted keeps travelling to the sorter. The instant the flight enters
soft cut-off a :class:`~bagsort.model.entities.CutoffMark` is committed in the
same journal session as the state flip, recording how many bags were still in
transit and how many had been accepted in total. That mark is the baseline the
tail-off window is watched against, and committing it atomically with the
state flip is what makes the window recoverable: a restart replays both or
neither.

**Hard cut-off** (``CUTOFF`` -> ``CLOSED``) is only allowed once the in-transit
queue is empty. After it, nothing is accepted for the flight again; hard
cut-off is final and cannot be re-opened.

**Rollback.** If the tail cannot be finished (a belt fault, a rescheduled
departure), soft cut-off can be rolled back to ``OPEN``. The rollback touches
no bag, mapping or sort record — it only flips the flight state and retires
the mark — so nothing can be accepted twice: bag identities come from the
committed tag sequence, and the sorter still refuses a second dispatch for any
bag it already sorted. A later soft cut-off simply takes a fresh baseline.

**Tail chute decision.** During the tail-off window the remaining bags keep
travelling to the flight's *original* chute; no dedicated tail-off chute is
assigned. A dedicated chute was considered and rejected. Its costs:

* it consumes a second chute resource for the whole tail-off window;
* re-assigning the flight bumps the chute assignment generation, which
  invalidates every cached route mid-flight — holders of the old generation
  are refused with a stale-reference error and must re-confirm, exactly during
  the most time-pressured minutes of the flight;
* bags already sorted stay in the old chute, so ground crew must sweep two
  chutes during the overlap instead of one;
* the reassignment is one more audited, fallible operation on the cut-off
  critical path.

Since soft cut-off already stops new bags from entering, the original chute
sees only tail bags anyway, so the separation a dedicated chute buys is
illusory; keeping the original chute drains the tail with zero routing
disruption.
"""

from __future__ import annotations

from typing import Any, Protocol

from bagsort.clock import Clock
from bagsort.errors import OrderingError
from bagsort.model.entities import Bag, CutoffMark, Flight, SortRecord
from bagsort.model.enums import BagState, FlightState
from bagsort.model.validate import is_open_state
from bagsort.store.store import Store

# Audit kinds the two transitions and the rollback are recorded under. The
# console maps domain events onto these; ``Plant.cutoff_review`` filters by
# them, so the whole cut-off history of a flight can be replayed entry by
# entry.
KIND_SOFT = "flight_cutoff_soft"
KIND_REOPENED = "flight_reopened"
KIND_HARD = "flight_cutoff_hard"
AUDIT_KINDS = (KIND_SOFT, KIND_REOPENED, KIND_HARD)


class CutoffBook(Protocol):
    """The part of the flight book the cut-off manager reads."""

    def get(self, flight_id: str) -> Flight: ...

    def list_bags_for(self, flight_id: str) -> list: ...


class TransitQueue(Protocol):
    """The part of the injector the tail-off window watches."""

    def queue(self, flight_id: str) -> list[Bag]: ...

    def in_transit(self, flight_id: str) -> int: ...


class SortLogView(Protocol):
    """The part of the sorter the reconciliation cross-checks against."""

    def was_executed(self, bag_id: str) -> bool: ...

    def records(self) -> list[SortRecord]: ...


class CutoffManager:
    """Drives soft cut-off, the tail-off window, rollback and hard cut-off."""

    def __init__(
        self,
        store: Store,
        book: CutoffBook,
        injector: TransitQueue,
        sorter: SortLogView,
        clock: Clock,
    ) -> None:
        self._store = store
        self._book = book
        self._injector = injector
        self._sorter = sorter
        self._clock = clock

    def mark(self, flight_id: str) -> CutoffMark | None:
        """The live soft cut-off baseline, or ``None`` outside the window."""

        return self._store.state().cutoffs.get(flight_id)

    def soft_cutoff(self, flight_id: str) -> CutoffMark:
        """Stop new check-ins and snapshot the tail, in one committed step."""

        flight = self._book.get(flight_id)
        if not is_open_state(flight.state):
            raise OrderingError(
                f"flight {flight_id!r} cannot enter soft cut-off from "
                f"{flight.state.value!r}"
            )
        at = self._clock.now_iso()
        mark = CutoffMark(
            flight_id=flight_id,
            began_at=at,
            in_transit=self._injector.in_transit(flight_id),
            accepted=len(self._book.list_bags_for(flight_id)),
        )
        updated = flight.evolved(state=FlightState.CUTOFF)
        with self._store.session() as session:
            session.state.flights[flight_id] = updated
            session.state.cutoffs[flight_id] = mark
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record("cutoff.begin", {"mark": mark.as_dict()}, at)
        return mark

    def reopen(self, flight_id: str) -> Flight:
        """Roll soft cut-off back to open when the tail cannot be finished.

        Only the flight state and the mark change. No bag, mapping or sort
        record is touched, so nothing already accepted can be accepted again.
        """

        flight = self._book.get(flight_id)
        if flight.state is not FlightState.CUTOFF:
            raise OrderingError(
                f"flight {flight_id!r} is not in soft cut-off and cannot be re-opened"
            )
        at = self._clock.now_iso()
        updated = flight.evolved(state=FlightState.OPEN)
        with self._store.session() as session:
            session.state.flights[flight_id] = updated
            session.state.cutoffs.pop(flight_id, None)
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record("cutoff.reopen", {"flight_id": flight_id}, at)
        return updated

    def hard_cutoff(self, flight_id: str) -> Flight:
        """Close the flight for good, once nothing is left on the belt."""

        flight = self._book.get(flight_id)
        if flight.state is not FlightState.CUTOFF:
            raise OrderingError(
                f"flight {flight_id!r} must pass soft cut-off before hard cut-off"
            )
        remaining = self._injector.in_transit(flight_id)
        if remaining:
            raise OrderingError(
                f"flight {flight_id!r} still has {remaining} bag(s) in transit; "
                "finish or drain the tail before hard cut-off"
            )
        at = self._clock.now_iso()
        updated = flight.evolved(state=FlightState.CLOSED, closed_at=at)
        with self._store.session() as session:
            session.state.flights[flight_id] = updated
            session.state.cutoffs.pop(flight_id, None)
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record("cutoff.finish", {"flight_id": flight_id}, at)
        return updated

    def status(self, flight_id: str) -> dict[str, Any]:
        """Cross-check the flight state, the acceptance ledger and the queue.

        Three independently committed views must agree: the flight book (state
        plus the cut-off mark), the acceptance ledger (committed bag records),
        and the in-transit queue, with the sorter's own log as a fourth
        witness. ``consistent`` is True only when every view tells the same
        story; ``tail_empty`` is reported separately because a healthy flight
        mid-window still has bags on the belt.
        """

        flight = self._book.get(flight_id)
        bags = self._book.list_bags_for(flight_id)
        by_state: dict[str, int] = {}
        for bag in bags:
            by_state[bag.state.value] = by_state.get(bag.state.value, 0) + 1
        queue = self._injector.queue(flight_id)
        sorted_ever = sum(1 for bag in bags if self._sorter.was_executed(bag.id))
        records = [
            record for record in self._sorter.records() if record.flight_id == flight_id
        ]
        mark = self.mark(flight_id)
        checks = {
            # The injector's queue agrees with a direct scan of the ledger.
            "queue_matches_ledger": len(queue)
            == by_state.get(BagState.INJECTED.value, 0),
            # Every bag the ledger shows as sorted appears in the sorter's log
            # exactly once: nothing sorted twice, nothing sorted and lost.
            "sort_log_matches_ledger": len(records) == sorted_ever,
            # During the window the tail can only shrink; growth means a bag
            # was accepted after soft cut-off, which must never happen.
            "tail_not_growing": mark is None or len(queue) <= mark.in_transit,
        }
        return {
            "flight_id": flight_id,
            "state": flight.state.value,
            "mark": mark.as_dict() if mark else None,
            "ledger": {"accepted": len(bags), "by_state": by_state},
            "in_transit": {
                "count": len(queue),
                "bag_ids": [bag.id for bag in queue],
            },
            "sort_records": len(records),
            "checks": checks,
            "tail_empty": not queue,
            "consistent": all(checks.values()),
        }
