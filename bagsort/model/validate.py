"""Structural validation shared by the desk, sorter and console."""

from __future__ import annotations

from .entities import Bag, Flight, Reading, TransferCommand
from .enums import BagState, FlightState
from .errors import empty_field


def require_text(value: str, name: str) -> str:
    """Return ``value`` stripped, raising when nothing is left."""

    text = (value or "").strip()
    if not text:
        raise empty_field(name)
    return text


def validate_flight(flight: Flight) -> Flight:
    require_text(flight.id, "id")
    require_text(flight.code, "code")
    return flight


def validate_bag(bag: Bag) -> Bag:
    require_text(bag.id, "id")
    require_text(bag.barcode, "barcode")
    return bag


def validate_reading(reading: Reading) -> Reading:
    require_text(reading.barcode, "barcode")
    require_text(reading.bag_id, "bag_id")
    return reading


def validate_transfer(command: TransferCommand) -> TransferCommand:
    require_text(command.flight_id, "flight_id")
    require_text(command.carousel, "carousel")
    require_text(command.id, "id")
    return command


def is_open_state(state: FlightState) -> bool:
    """Whether the desk may still accept baggage for the flight.

    A soft cut-off keeps accepting while the wind-down runs; only the hard
    cut-off and the closed state turn the desk away.
    """

    return state in (
        FlightState.OPEN,
        FlightState.BOARDING,
        FlightState.SOFT_CUTOFF,
    )


def is_soft_cutoff_state(state: FlightState) -> bool:
    return state is FlightState.SOFT_CUTOFF


def is_active_bag_state(state: BagState) -> bool:
    return state.active
