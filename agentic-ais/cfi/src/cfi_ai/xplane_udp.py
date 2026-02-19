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
BEACON_PREFIX: Final[bytes] = b"BECN\x00"


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
    "engine_running": "sim/flightmodel/engine/ENGN_running[0]",
    "engine_rpm": "sim/cockpit2/engine/indicators/engine_speed_rpm[0]",
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
        discovery_enabled: bool,
        beacon_multicast_group: str,
        beacon_port: int,
        beacon_timeout_sec: float,
        local_port: int,
        rref_hz: int,
        buffer_retention_sec: float = 120.0,
        local_host: str = "0.0.0.0",
    ) -> None:
        self._xplane_host = xplane_host.strip()
        self._xplane_port = int(xplane_port)
        self._discovery_enabled = bool(discovery_enabled)
        self._beacon_multicast_group = beacon_multicast_group.strip()
        self._beacon_port = int(beacon_port)
        self._beacon_timeout_sec = max(0.1, float(beacon_timeout_sec))
        self._xplane_addr: tuple[str, int] | None = None
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

        self._xplane_addr = await self._resolve_xplane_addr()
        if self._xplane_addr is None:
            raise RuntimeError("Unable to resolve X-Plane UDP endpoint.")

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
        if self._socket is None or self._xplane_addr is None:
            return
        loop = asyncio.get_running_loop()
        for key, dataref in DATAREF_BY_KEY.items():
            idx = INDEX_BY_KEY[key]
            packet = build_rref_request_packet(freq_hz=freq_hz, index=idx, dataref=dataref)
            await loop.sock_sendto(self._socket, packet, self._xplane_addr)

    async def _resolve_xplane_addr(self) -> tuple[str, int]:
        if self._should_attempt_beacon_discovery():
            discovered = await asyncio.to_thread(
                discover_xplane_via_beacon,
                multicast_group=self._beacon_multicast_group,
                beacon_port=self._beacon_port,
                timeout_sec=self._beacon_timeout_sec,
            )
            if discovered is not None:
                return discovered

        if self._xplane_host and self._xplane_host.lower() not in {"auto", "discover"}:
            if self._xplane_port <= 0:
                raise RuntimeError("Explicit X-Plane host configured without valid UDP port.")
            return (self._xplane_host, self._xplane_port)

        raise RuntimeError("X-Plane UDP endpoint not discovered from BEACON and no explicit host configured.")

    def _should_attempt_beacon_discovery(self) -> bool:
        if not self._discovery_enabled:
            return False
        host = self._xplane_host.lower()
        if host in {"", "auto", "discover"}:
            return True
        # If discovery is enabled and explicit host is set, explicit host wins.
        return False

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
        engine_running_raw = _f("engine_running")
        engine_running: bool | None = None
        if engine_running_raw is not None:
            engine_running = engine_running_raw >= 0.5

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
            engine_running=engine_running,
            engine_rpm=_f("engine_rpm"),
            flap_ratio=_f("flap_ratio"),
            parking_brake_ratio=_f("parking_brake_ratio"),
            com1_hz=_i("com1_hz"),
            on_ground=on_ground,
            stall_warning=stall_warning,
        )


def parse_beacon_datagram(payload: bytes, sender_ip: str) -> tuple[str, int] | None:
    if not payload.startswith(BEACON_PREFIX):
        return None

    body = payload[len(BEACON_PREFIX) :]
    if len(body) < 16:
        return None

    # BECN body layout (X-Plane):
    #  byte major, byte minor, int host_id, int version, int role, ushort port, char[] name
    port = struct.unpack_from("<H", body, 14)[0]
    if port <= 0:
        return None
    host = sender_ip.strip()
    if not host:
        return None
    return (host, int(port))


def discover_xplane_via_beacon(
    *,
    multicast_group: str,
    beacon_port: int,
    timeout_sec: float,
) -> tuple[str, int] | None:
    group = multicast_group.strip()
    if not group:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", beacon_port))

        mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        deadline = time.time() + max(0.1, timeout_sec)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                payload, addr = sock.recvfrom(2048)
            except socket.timeout:
                return None
            parsed = parse_beacon_datagram(payload, sender_ip=addr[0])
            if parsed is not None:
                return parsed
    finally:
        with suppress(OSError):
            sock.close()
