"""Data coordinator for Neewer WiFi lights."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_COMMAND_DELAY, DOMAIN
from .discovery import async_get_local_networks, async_resolve_client_ip, client_ip_for_host
from .protocol import (
    NeewerProtocol,
    ha_brightness_to_protocol,
    kelvin_to_protocol,
    protocol_brightness_to_ha,
    protocol_to_kelvin,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class NeewerLightState:
    """Locally tracked light state (device does not report state)."""

    is_on: bool = False
    brightness: int = protocol_brightness_to_ha(50)
    color_temp_kelvin: int = protocol_to_kelvin(56)


class NeewerDataUpdateCoordinator(DataUpdateCoordinator[NeewerLightState]):
    """Coordinator managing protocol commands and local state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        protocol: NeewerProtocol,
    ) -> None:
        self.entry = entry
        self.protocol = protocol
        self.host = entry.data["host"]
        self._state = NeewerLightState()
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.host}",
            update_interval=None,
        )

    async def _async_update_data(self) -> NeewerLightState:
        """Return locally tracked state (no device polling)."""
        return self._state

    @property
    def state(self) -> NeewerLightState:
        """Current light state."""
        return self._state

    async def async_connect(self) -> None:
        """Establish UDP session with the light."""
        client_ip = await self._async_resolve_client_ip()
        _LOGGER.info(
            "Setting up light %s using client IP %s", self.host, client_ip
        )
        await self.protocol.async_connect(self.host, client_ip)

    async def _async_resolve_client_ip(self) -> str:
        """Resolve client IP from config or local adapters."""
        if self.entry.data.get("client_ip"):
            return self.entry.data["client_ip"]
        networks = await async_get_local_networks(self.hass)
        client_ip = client_ip_for_host(self.host, networks)
        if client_ip is None:
            client_ip = await async_resolve_client_ip(self.hass, self.host)
        if client_ip is None:
            _LOGGER.error(
                "Cannot determine client IP for light %s on host networks",
                self.host,
            )
            raise RuntimeError(f"Cannot determine client IP for light {self.host}")
        return client_ip

    async def async_turn_on(
        self,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
    ) -> None:
        """Turn on and optionally set brightness and color temperature."""
        bri = ha_brightness_to_protocol(
            brightness if brightness is not None else self._state.brightness
        )
        kelvin = (
            color_temp_kelvin
            if color_temp_kelvin is not None
            else self._state.color_temp_kelvin
        )
        temp = kelvin_to_protocol(kelvin)

        if not self._state.is_on:
            await self.protocol.async_power_on(self.host)
            await asyncio.sleep(DEFAULT_COMMAND_DELAY)
        await self.protocol.async_set_brightness_temp(self.host, bri, temp)

        self._state.is_on = True
        self._state.brightness = protocol_brightness_to_ha(bri)
        self._state.color_temp_kelvin = protocol_to_kelvin(temp)
        _LOGGER.info(
            "Turned on %s (brightness=%d, color_temp=%dK)",
            self.host,
            self._state.brightness,
            self._state.color_temp_kelvin,
        )
        self.async_update_listeners()

    async def async_turn_off(self) -> None:
        """Turn the light off."""
        await self.protocol.async_power_off(self.host)
        _LOGGER.info("Turned off %s", self.host)
        self._state.is_on = False
        self.async_update_listeners()

    async def async_set_brightness(self, brightness: int) -> None:
        """Set brightness while preserving color temperature."""
        if not self._state.is_on:
            await self.async_turn_on(brightness=brightness)
            return
        bri = ha_brightness_to_protocol(brightness)
        temp = kelvin_to_protocol(self._state.color_temp_kelvin)
        await self.protocol.async_set_brightness_temp(self.host, bri, temp)
        self._state.brightness = protocol_brightness_to_ha(bri)
        self.async_update_listeners()

    async def async_set_color_temp(self, color_temp_kelvin: int) -> None:
        """Set color temperature while preserving brightness."""
        if not self._state.is_on:
            await self.async_turn_on(color_temp_kelvin=color_temp_kelvin)
            return
        bri = ha_brightness_to_protocol(self._state.brightness)
        temp = kelvin_to_protocol(color_temp_kelvin)
        await self.protocol.async_set_brightness_temp(self.host, bri, temp)
        self._state.color_temp_kelvin = protocol_to_kelvin(temp)
        self.async_update_listeners()


class NeewerHub:
    """Domain-level hub sharing the UDP socket across config entries."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.protocol = NeewerProtocol(hass)
        self.coordinators: dict[str, NeewerDataUpdateCoordinator] = {}

    async def async_setup(self) -> None:
        """Initialize shared UDP transport."""
        _LOGGER.info("Starting shared Neewer UDP hub")
        await self.protocol.async_setup()

    async def async_shutdown(self) -> None:
        """Close shared UDP transport."""
        _LOGGER.info("Shutting down shared Neewer UDP hub")
        await self.protocol.async_close()

    def get_coordinator(self, entry: ConfigEntry) -> NeewerDataUpdateCoordinator | None:
        """Return coordinator for a config entry id."""
        return self.coordinators.get(entry.entry_id)

    async def async_register_entry(self, entry: ConfigEntry) -> NeewerDataUpdateCoordinator:
        """Create and connect a coordinator for a config entry."""
        _LOGGER.info("Registering Neewer light %s", entry.data["host"])
        coordinator = NeewerDataUpdateCoordinator(self.hass, entry, self.protocol)
        await coordinator.async_connect()
        await coordinator.async_refresh()
        self.coordinators[entry.entry_id] = coordinator
        return coordinator

    async def async_unregister_entry(self, entry: ConfigEntry) -> None:
        """Remove coordinator and protocol session for an entry."""
        _LOGGER.info("Unregistering Neewer light %s", entry.data["host"])
        self.coordinators.pop(entry.entry_id, None)
        self.protocol.unregister_light(entry.data["host"])
