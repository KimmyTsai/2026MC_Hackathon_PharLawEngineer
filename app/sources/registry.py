"""Provider registry.

Builds the provider set for the current run mode and reports each provider's
mode so the UI badge can never claim live data it does not have.
"""

from __future__ import annotations

from datetime import datetime

from app.config import RunMode, Settings
from app.models import Signal, SignalKind, SourceMode
from app.sources.calendar import FixtureCalendar, GoogleCalendar
from app.sources.mailbox import FixtureMailbox, GmailMailbox
from app.sources.scenario import Scenario
from app.sources.signals import ScenarioSignalProvider, live_provider

SIGNAL_KINDS: tuple[SignalKind, ...] = (
    SignalKind.rain,
    SignalKind.flood,
    SignalKind.air_quality,
    SignalKind.bike_availability,
    SignalKind.bus_eta,
    SignalKind.departure_state,
)


class ProviderRegistry:
    def __init__(self, settings: Settings, scenario: Scenario) -> None:
        self.settings = settings
        self.scenario = scenario
        replay = settings.mode is RunMode.replay

        self.signals = {
            kind: (
                ScenarioSignalProvider(kind, scenario)
                if replay
                else live_provider(kind, settings)
            )
            for kind in SIGNAL_KINDS
        }

        self.mailbox = (
            FixtureMailbox(settings.mailbox_dir, settings.outbox_dir, scenario.mailbox_ids)
            if replay
            else GmailMailbox()
        )
        self.calendar = FixtureCalendar() if replay else GoogleCalendar()

    def fetch(self, kind: SignalKind, at: datetime, area: str | None = None) -> Signal:
        provider = self.signals.get(kind)
        if provider is None:
            from app.sources.base import unavailable_signal

            return unavailable_signal(kind, "unknown", at, f"no provider for {kind.value}")
        return provider.fetch(at, area)

    def fetch_all(self, at: datetime) -> dict[SignalKind, Signal]:
        return {kind: self.fetch(kind, at) for kind in self.signals}

    def modes(self, at: datetime | None = None) -> dict[str, str]:
        """Per-provider badge. Uses freshness when a timestamp is given."""
        out: dict[str, str] = {}
        for kind, provider in self.signals.items():
            mode: SourceMode = getattr(provider, "mode", SourceMode.unavailable)
            if at is not None:
                try:
                    mode = provider.fetch(at).freshness(at)
                except Exception:  # noqa: BLE001 - badge must never break the page
                    mode = SourceMode.unavailable
            out[kind.value] = mode.value
        out["mailbox"] = self.mailbox.mode.value
        out["calendar"] = self.calendar.mode.value
        return out
