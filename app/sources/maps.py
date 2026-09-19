"""Google Map Tiles API, proxied.

Why proxy instead of letting the browser fetch tiles directly: a Map Tiles
request needs `key` in the URL, so a direct tile layer would publish the API key
in the page. Relaying through this service keeps the key on the machine.

Why the Map Tiles API rather than pointing Leaflet at `mt0.google.com`: that
older trick violates the Google Maps Terms of Service, which allow tile access
only through the Maps APIs. This is the sanctioned path — a session token, then
tiles against that session.

Caching is in-memory and small, purely so panning does not re-fetch the tile you
just looked at. Persistent on-disk tile caching is a Terms question this code is
not in a position to answer, and the OSM fallback already covers a dead network.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

CREATE_SESSION_URL = "https://tile.googleapis.com/v1/createSession"
TILE_URL = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"
VIEWPORT_URL = "https://tile.googleapis.com/tile/v1/viewport"

# Google's own attribution must be shown; this is the fallback text if the
# viewport call cannot supply the precise string.
DEFAULT_ATTRIBUTION = "地圖資料 ©Google"

SESSION_SAFETY_MARGIN = 300  # refresh this many seconds before expiry
MAX_CACHED_TILES = 400


class MapTilesError(Exception):
    """Google could not serve the tile. The caller falls back to OSM."""


@dataclass
class TileSession:
    token: str
    expiry: float
    tile_size: int = 256

    @property
    def valid(self) -> bool:
        return bool(self.token) and time.time() < self.expiry - SESSION_SAFETY_MARGIN


class GoogleTileProxy:
    """Keeps one tile session alive and relays tiles."""

    provider = "google"

    def __init__(self, api_key: str, language: str = "zh-TW", region: str = "TW") -> None:
        self.api_key = api_key
        self.language = language
        self.region = region
        self._session: TileSession | None = None
        self._attribution: str | None = None
        self._cache: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._client = httpx.Client(timeout=20)

    # -- session -------------------------------------------------------------
    def session(self) -> TileSession:
        if self._session is not None and self._session.valid:
            return self._session
        response = self._client.post(
            CREATE_SESSION_URL,
            params={"key": self.api_key},
            json={
                "mapType": "roadmap",
                "language": self.language,
                "region": self.region,
                "highDpi": False,
            },
        )
        if response.status_code != 200:
            raise MapTilesError(f"createSession {response.status_code}: {response.text[:160]}")
        body = response.json()
        self._session = TileSession(
            token=body["session"],
            expiry=float(body.get("expiry", time.time() + 3600)),
            tile_size=int(body.get("tileWidth", 256)),
        )
        return self._session

    # -- tiles ---------------------------------------------------------------
    def tile(self, z: int, x: int, y: int) -> bytes:
        key = (z, x, y)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        session = self.session()
        response = self._client.get(
            TILE_URL.format(z=z, x=x, y=y),
            params={"session": session.token, "key": self.api_key},
        )
        if response.status_code != 200:
            raise MapTilesError(f"tile {z}/{x}/{y} → {response.status_code}")

        data = response.content
        self._cache[key] = data
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_CACHED_TILES:
            self._cache.popitem(last=False)
        return data

    # -- attribution ---------------------------------------------------------
    def attribution(self, north: float, south: float, east: float, west: float, zoom: int) -> str:
        """Google requires showing the attribution string for the viewport."""
        if self._attribution:
            return self._attribution
        try:
            session = self.session()
            response = self._client.get(
                VIEWPORT_URL,
                params={
                    "session": session.token,
                    "key": self.api_key,
                    "zoom": zoom,
                    "north": north,
                    "south": south,
                    "east": east,
                    "west": west,
                },
            )
            if response.status_code == 200:
                text = response.json().get("copyright", "")
                self._attribution = text or DEFAULT_ATTRIBUTION
            else:
                self._attribution = DEFAULT_ATTRIBUTION
        except Exception:  # noqa: BLE001 - attribution must never break the map
            self._attribution = DEFAULT_ATTRIBUTION
        return self._attribution

    def close(self) -> None:
        self._client.close()
