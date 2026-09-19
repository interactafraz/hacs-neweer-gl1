"""Neewer GL1 WiFi UDP protocol implementation."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING

from .const import (
    DEFAULT_COMMAND_DELAY,
    DEFAULT_PORT,
    HANDSHAKE_REPEAT,
    HEARTBEAT_ACK,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_PACKET,
    MAX_BRIGHTNESS,
    MAX_COLOR_TEMP_PROTOCOL,
    MIN_BRIGHTNESS,
    MIN_COLOR_TEMP_PROTOCOL,
    POWER_OFF_PACKET,
    POWER_ON_PACKET,
    REHANDSHAKE_INTERVAL,
    WAKEUP_PACKET,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def build_handshake(client_ip: str) -> bytes:
    """Build the checksummed handshake packet embedding the client IP."""
    header = bytes([0x80, 0x02, 0x10, 0x00, 0x00, len(client_ip)])
    ip_bytes = client_ip.encode("ascii")
    payload = header + ip_bytes
    checksum = sum(payload) & 0xFF
    return payload + bytes([checksum])


def build_brightness_temp_packet(brightness: int, temperature: int) -> bytes:
    """Build brightness and color-temperature command packet."""
    prefix = [0x80, 0x05, 0x03, 0x02]
    bri = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, brightness))
    temp = max(MIN_COLOR_TEMP_PROTOCOL, min(MAX_COLOR_TEMP_PROTOCOL, temperature))
    payload = prefix + [bri, temp]
    checksum = sum(payload) & 0xFF
    return bytes(payload + [checksum])


def is_neewer_response(data: bytes) -> bool:
    """Return True if UDP payload looks like a Neewer protocol response."""
    if len(data) < 4:
        return False
    if data[0] != 0x80:
        return False
    # Known heartbeat acknowledgement from GL1 Pro
    if data[:4] == HEARTBEAT_ACK:
        return True
    # Accept other 0x80-framed short responses as plausible Neewer traffic
    return len(data) <= 16


def kelvin_to_protocol(kelvin: int) -> int:
    """Map kelvin (2900-7000) to protocol temperature units (29-70)."""
    clamped = max(MIN_COLOR_TEMP_PROTOCOL * 100, min(MAX_COLOR_TEMP_PROTOCOL * 100, kelvin))
    return clamped // 100


def protocol_to_kelvin(protocol_temp: int) -> int:
    """Map protocol temperature units to kelvin."""
    return protocol_temp * 100


def ha_brightness_to_protocol(brightness: int) -> int:
    """Map Home Assistant brightness (0-255) to protocol (1-100)."""
    return max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, round(brightness * 100 / 255)))


def protocol_brightness_to_ha(brightness: int) -> int:
    """Map protocol brightness (1-100) to Home Assistant (0-255)."""
    return round(brightness * 255 / 100)


class NeewerProtocol(asyncio.DatagramProtocol):
    """Async UDP protocol bound to port 5052 for Neewer session management."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.transport: asyncio.DatagramTransport | None = None
        self._ready = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._sessions: dict[str, _LightSession] = {}
        self._heartbeat_task: asyncio.Task | None = None

    async def async_setup(self) -> None:
        """Bind the shared UDP socket on port 5052."""
        loop = self.hass.loop
        await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=("0.0.0.0", DEFAULT_PORT),
            family=socket.AF_INET,
            reuse_port=True,
        )
        await self._ready.wait()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        _LOGGER.info("UDP socket bound on 0.0.0.0:%d", DEFAULT_PORT)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store transport when the UDP endpoint is ready."""
        self.transport = transport
        self._ready.set()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Route inbound packets to the matching light session."""
        host = addr[0]
        session = self._sessions.get(host)
        if session is not None and is_neewer_response(data):
            session.last_response = data
            _LOGGER.debug("Received %d bytes from %s: %s", len(data), host, data.hex())

    def error_received(self, exc: Exception) -> None:
        """Log UDP socket errors."""
        _LOGGER.debug("UDP error received: %s", exc)

    async def async_close(self) -> None:
        """Stop heartbeat and close the UDP socket."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self.transport is not None:
            self.transport.close()
            _LOGGER.info("UDP socket on port %d closed", DEFAULT_PORT)

    def register_light(self, host: str, client_ip: str) -> _LightSession:
        """Register a light host for session tracking."""
        session = self._sessions.get(host)
        if session is None:
            session = _LightSession(host=host, client_ip=client_ip)
            self._sessions[host] = session
        else:
            session.client_ip = client_ip
        return session

    def unregister_light(self, host: str) -> None:
        """Remove a light from session tracking."""
        if self._sessions.pop(host, None) is not None:
            _LOGGER.info("Unregistered Neewer light session for %s", host)

    async def async_connect(self, host: str, client_ip: str) -> None:
        """Perform handshake and wakeup for a light."""
        _LOGGER.info("Connecting to Neewer light at %s (client IP %s)", host, client_ip)
        session = self.register_light(host, client_ip)
        handshake = build_handshake(client_ip)
        for _ in range(HANDSHAKE_REPEAT):
            await self._send(host, handshake)
            await asyncio.sleep(DEFAULT_COMMAND_DELAY)
        await asyncio.sleep(1.5)
        await self._send(host, WAKEUP_PACKET)
        await asyncio.sleep(1.5)
        session.connected = True
        session.last_handshake = asyncio.get_running_loop().time()
        _LOGGER.info("Connected to Neewer light at %s", host)

    async def async_ensure_connected(self, host: str) -> _LightSession:
        """Ensure session exists and is connected."""
        session = self._sessions.get(host)
        if session is None or not session.connected:
            raise RuntimeError(f"No registered session for {host}")
        return session

    async def async_power_on(self, host: str) -> None:
        """Turn the light on."""
        async with self._command_lock:
            await self.async_ensure_connected(host)
            await self._send(host, POWER_ON_PACKET)

    async def async_power_off(self, host: str) -> None:
        """Turn the light off."""
        async with self._command_lock:
            await self.async_ensure_connected(host)
            await self._send(host, POWER_OFF_PACKET)

    async def async_set_brightness_temp(
        self, host: str, brightness: int, temperature: int
    ) -> None:
        """Set brightness and color temperature."""
        async with self._command_lock:
            await self.async_ensure_connected(host)
            packet = build_brightness_temp_packet(brightness, temperature)
            await self._send(host, packet)

    async def _send(self, host: str, data: bytes) -> None:
        """Send a UDP packet to the light."""
        if self.transport is None:
            raise RuntimeError("UDP transport is not ready")
        self.transport.sendto(data, (host, DEFAULT_PORT))
        _LOGGER.debug("Sent %d bytes to %s:%d", len(data), host, DEFAULT_PORT)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats and re-handshake stale sessions."""
        try:
            while True:
                now = asyncio.get_running_loop().time()
                for host, session in list(self._sessions.items()):
                    if not session.connected:
                        continue
                    if now - session.last_handshake >= REHANDSHAKE_INTERVAL:
                        _LOGGER.debug("Periodic re-handshake for %s", host)
                        session.connected = False
                        try:
                            await self.async_connect(host, session.client_ip)
                        except Exception as err:  # noqa: BLE001 - keep heartbeat loop alive
                            _LOGGER.warning(
                                "Re-handshake failed for %s: %s", host, err
                            )
                        continue
                    try:
                        await self._send(host, HEARTBEAT_PACKET)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Heartbeat failed for %s: %s", host, err)
                        session.connected = False
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            pass


class _LightSession:
    """Per-light session state tracked locally."""

    def __init__(self, host: str, client_ip: str) -> None:
        self.host = host
        self.client_ip = client_ip
        self.connected = False
        self.last_handshake = 0.0
        self.last_response: bytes | None = None


async def async_probe_light(
    host: str,
    client_ip: str,
    timeout: float = 2.0,
) -> bool:
    """
    Probe a host for a Neewer WiFi light using handshake + wakeup + heartbeat.

    Binds DEFAULT_PORT (with SO_REUSEPORT) since the light always replies to
    that fixed port rather than to the sender's actual source port.
    Returns True if a plausible Neewer response is received.
    """
    _LOGGER.debug("Probing %s with client IP %s (timeout %.1fs)", host, client_ip, timeout)
    loop = asyncio.get_running_loop()
    response_future: asyncio.Future[bytes] = loop.create_future()

    class _ProbeProtocol(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            if addr[0] == host and is_neewer_response(data) and not response_future.done():
                response_future.set_result(data)

        def error_received(self, exc: Exception) -> None:
            if not response_future.done():
                response_future.set_exception(exc)

    transport, _ = await loop.create_datagram_endpoint(
        _ProbeProtocol,
        local_addr=("0.0.0.0", DEFAULT_PORT),
        family=socket.AF_INET,
        reuse_port=True,
    )

    try:
        handshake = build_handshake(client_ip)
        for _ in range(HANDSHAKE_REPEAT):
            transport.sendto(handshake, (host, DEFAULT_PORT))
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.5)
        transport.sendto(WAKEUP_PACKET, (host, DEFAULT_PORT))
        await asyncio.sleep(0.5)
        transport.sendto(HEARTBEAT_PACKET, (host, DEFAULT_PORT))
        try:
            response = await asyncio.wait_for(response_future, timeout=timeout)
            _LOGGER.debug(
                "Probe succeeded for %s, response: %s", host, response.hex()
            )
            return True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _LOGGER.debug("Probe timed out for %s", host)
            return False
    finally:
        transport.close()
