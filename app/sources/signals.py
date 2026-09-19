"""Signal providers.

One generic fixture implementation reads the scenario timeline; each live
provider is a separate class so it can be enabled independently (Stage 3) and
so a missing one degrades to `unavailable` instead of crashing the loop.

Covers the contracts named in references/architecture-and-contracts.md:
get_weather, get_bike_status, get_bus_eta, get_flood_sensors, get_air_quality,
plus the local departure-state observation from CLAUDE.md.
"""

from __future__ import annotations

from datetime import datetime

from app.models import Signal, SignalKind, SourceMode
from app.sources.base import unavailable_signal
from app.sources.scenario import Scenario


class ScenarioSignalProvider:
    """Fixture provider: returns whatever the scenario says is true at `at`."""

    mode = SourceMode.fixture

    def __init__(self, kind: SignalKind, scenario: Scenario, name: str | None = None) -> None:
        self.kind = kind
        self.scenario = scenario
        self.name = name or f"{kind.value}_fixture"

    def fetch(self, at: datetime, area: str | None = None) -> Signal:
        signal = self.scenario.signal_at(self.kind, at)
        if signal is None:
            return unavailable_signal(
                self.kind, self.name, at, f"scenario has no {self.kind.value} value"
            )
        return signal


class NotConfiguredProvider:
    """Live provider placeholder.

    Returns an `unavailable` signal rather than raising, so the UI can show the
    provider badge as unavailable and the planner can record missing evidence.
    """

    mode = SourceMode.unavailable

    def __init__(self, kind: SignalKind, name: str, reason: str) -> None:
        self.kind = kind
        self.name = name
        self.reason = reason

    def fetch(self, at: datetime, area: str | None = None) -> Signal:
        return unavailable_signal(self.kind, self.name, at, self.reason)


# --- live providers ---------------------------------------------------------
# Implemented one at a time in Stage 3. Dataset IDs and field names must be
# confirmed against each platform's docs at implementation time, never guessed.


def live_provider(kind: SignalKind, settings) -> NotConfiguredProvider:
    """Return the live provider for `kind`, or an honest 'not configured' stub."""
    specs: dict[SignalKind, tuple[str, str | None]] = {
        SignalKind.rain: ("cwa_live", settings.cwa_api_key),
        SignalKind.flood: ("cwa_flood_live", settings.cwa_api_key),
        SignalKind.air_quality: ("moenv_aqi_live", None),
        SignalKind.bike_availability: ("tdx_youbike_live", settings.tdx_client_id),
        SignalKind.bus_eta: ("tdx_bus_live", settings.tdx_client_id),
        SignalKind.departure_state: ("device_live", None),
    }
    name, credential = specs.get(kind, (f"{kind.value}_live", None))
    reason = (
        f"{name} not implemented yet (Stage 3)"
        if credential
        else f"{name} not implemented yet and no credential configured"
    )
    return NotConfiguredProvider(kind, name, reason)
