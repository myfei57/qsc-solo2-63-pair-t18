"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch.

    The cut-off is two phases. :data:`SOFT_CUTOFF` still accepts baggage at the
    desk (the queue there is not turned away) but no new downstream commands are
    issued; :data:`CUTOFF` is the hard stop that accepts nothing. A soft cut-off
    may be walked back to :data:`BOARDING` while the wind-down is unfinished.
    """

    OPEN = "open"
    BOARDING = "boarding"
    SOFT_CUTOFF = "soft_cutoff"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
        )

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"
