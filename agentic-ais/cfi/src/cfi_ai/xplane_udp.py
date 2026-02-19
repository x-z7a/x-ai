from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections import deque
from contextlib import suppress
from typing import Final

from cfi_ai.types import FlightSnapshot, UdpStateSource


RREF_RESPONSE_PREFIX: Final[bytes] = b"RREF,"
RREF_REQUEST_HEADER: Final[bytes] = b"RREF\x00"


DATAREF_BY_KEY: dict[str, str] = {
    "latitude_deg": "sim/flightmodel/position/latitude",
    "longitude_deg": "sim/flightmodel/position/longitude",
    "elevation_m": "sim/flightmodel/position/elevation",
    "groundspeed_m_s": "sim/flightmodel/position/groundspeed",
    "indicated_airspeed_kt": "sim/cockpit2/gauges/indicators/airspeed_kts_pilot",
    "heading_true_deg": "sim/flightmodel/position/true_psi",
    "magnetic_heading_deg": "sim/flightmodel/position/mag_psi",
    "vertical_speed_fpm": "sim/flightmodel/position/vh_ind_fpm",
    "roll_deg": "sim/flightmodel/position/phi",
    "pitch_deg": "sim/flightmodel/position/theta",
    "throttle_ratio": "sim/cockpit2/engine/actuators/throttle_ratio_all",
    "flap_ratio": "sim/flightmodel/controls/flaprqst",
    "parking_brake_ratio": "sim/cockpit2/controls/parking_brake_ratio",
    "on_ground": "sim/flightmodel/failures/onground_any",
    "stall_warning": "sim/cockpit2/annunciators/stall_warning",
    "com1_hz": "sim/cockpit2/radios/actuators/com1_frequency_hz_833",
}

INDEX_BY_KEY: dict[str, int] = {
    key: idx for idx, key in enumerate(DATAREF_BY_KEY.keys(), start=1)
}
KEY_BY_INDEX: dict[int, str] = {
    idx: key for key, idx in INDEX_BY_KEY.items()
}


def build_rref_request_packet(freq_hz: int, index: int, dataref: str) -> bytes:
    encoded = dataref.encode("ascii", errors="ignore")[:399]
    payload = encoded + b"\x00"
    payload = payload.ljust(400, b"\x00")
    return struct.pack("<5sii400s", RREF_REQUEST_HEADER, freq_hz, index, payload)


def parse_rref_datagram(payload: bytes) -> dict[int, float]:
    if not payload.startswith(RREF_RESPONSE_PREFIX):
        return {}

    out: dict[int, float] = {}
    body = payload[len(RREF_RESPONSE_PREFIX) :]
    chunk_size = 8
    entry_count = len(body) // chunk_size
    for i in range(entry_count):
        chunk = body[i * chunk_size : (i + 1) * chunk_size]
        index, value = struct.unpack("<if", chunk)
        out[index] = float(value)
    return out


class XPlaneUdpClient(UdpStateSource):
    def __init__(
        self,
        *,
        xplane_host: str,
        xplane_port: int,
        local_port: int,
        rref_hz: int,
        buffer_retention_sec: float = 120.0,
        local_host: str = "0.0.0.0",
    ) -> None:
        self._xplane_addr = (xplane_host, xplane_port)
        self._local_host = local_host
        self._local_port = local_port
        self._rref_hz = rref_hz
        self._buffer_retention_sec = max(30.0, buffer_retention_sec)

        self._socket: socket.socket | None = None
        self._rx_task: asyncio.Task[None] | None = None
        self._resubscribe_task: asyncio.Task[None] | None = None
        self._running = False

        self._values: dict[str, float] = {}
        self._latest: FlightSnapshot | None = None
        maxlen = int(self._buffer_retention_sec * max(1, self._rref_hz) * 2)
        self._snapshots: deque[FlightSnapshot] = deque(maxlen=maxlen)

    async def start(self) -> None:
        if self._running:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._local_host, self._local_port))
        sock.setblocking(False)

        self._socket = sock
        self._running = True

        await self._subscribe_all(freq_hz=self._rref_hz)
        self._rx_task = asyncio.create_task(self._receive_loop())
        self._resubscribe_task = asyncio.create_task(self._resubscribe_loop())

    async def stop(self) -> None:
        self._running = False
        await self._subscribe_all(freq_hz=0)

        tasks = [self._rx_task, self._resubscribe_task]
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            with suppress(Exception):
                await task

        self._rx_task = None
        self._resubscribe_task = None

        if self._socket is not None:
            with suppress(Exception):
                self._socket.close()
        self._socket = None

    def latest(self) -> FlightSnapshot | None:
        return self._latest

    def window(self, seconds: float) -> list[FlightSnapshot]:
        if seconds <= 0:
            return []
        cutoff = time.time() - seconds
        return [snap for snap in self._snapshots if snap.timestamp_sec >= cutoff]

    async def _receive_loop(self) -> None:
        if self._socket is None:
            return
        loop = asyncio.get_running_loop()

        while self._running and self._socket is not None:
            payload, _addr = await loop.sock_recvfrom(self._socket, 4096)
            values_by_index = parse_rref_datagram(payload)
            if not values_by_index:
                continue

            for idx, value in values_by_index.items():
                key = KEY_BY_INDEX.get(idx)
                if key is None:
                    continue
                self._values[key] = value

            snapshot = self._build_snapshot(time.time())
            self._latest = snapshot
            self._snapshots.append(snapshot)

    async def _resubscribe_loop(self) -> None:
        while self._running:
            await asyncio.sleep(5.0)
            await self._subscribe_all(freq_hz=self._rref_hz)

    async def _subscribe_all(self, *, freq_hz: int) -> None:
        if self._socket is None:
            return
        loop = asyncio.get_running_loop()
        for key, dataref in DATAREF_BY_KEY.items():
            idx = INDEX_BY_KEY[key]
            packet = build_rref_request_packet(freq_hz=freq_hz, index=idx, dataref=dataref)
            await loop.sock_sendto(self._socket, packet, self._xplane_addr)

    def _build_snapshot(self, timestamp_sec: float) -> FlightSnapshot:
        def _f(name: str) -> float | None:
            if name not in self._values:
                return None
            return float(self._values[name])

        def _i(name: str) -> int | None:
            value = _f(name)
            if value is None:
                return None
            return int(round(value))

        on_ground = (_f("on_ground") or 0.0) >= 0.5
        stall_warning = (_f("stall_warning") or 0.0) >= 0.5

        return FlightSnapshot(
            timestamp_sec=timestamp_sec,
            latitude_deg=_f("latitude_deg"),
            longitude_deg=_f("longitude_deg"),
            elevation_m=_f("elevation_m"),
            groundspeed_m_s=_f("groundspeed_m_s"),
            indicated_airspeed_kt=_f("indicated_airspeed_kt"),
            heading_true_deg=_f("heading_true_deg"),
            magnetic_heading_deg=_f("magnetic_heading_deg"),
            vertical_speed_fpm=_f("vertical_speed_fpm"),
            roll_deg=_f("roll_deg"),
            pitch_deg=_f("pitch_deg"),
            throttle_ratio=_f("throttle_ratio"),
            flap_ratio=_f("flap_ratio"),
            parking_brake_ratio=_f("parking_brake_ratio"),
            com1_hz=_i("com1_hz"),
            on_ground=on_ground,
            stall_warning=stall_warning,
        )
