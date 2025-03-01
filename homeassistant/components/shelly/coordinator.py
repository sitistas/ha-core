"""Coordinators for the Shelly integration."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, cast

import aioshelly
from aioshelly.block_device import BlockDevice
from aioshelly.rpc_device import RpcDevice
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry, update_coordinator
from homeassistant.helpers.debounce import Debouncer

from .const import (
    AIOSHELLY_DEVICE_TIMEOUT_SEC,
    ATTR_BETA,
    ATTR_CHANNEL,
    ATTR_CLICK_TYPE,
    ATTR_DEVICE,
    ATTR_GENERATION,
    BATTERY_DEVICES_WITH_PERMANENT_CONNECTION,
    CONF_SLEEP_PERIOD,
    DUAL_MODE_LIGHT_MODELS,
    ENTRY_RELOAD_COOLDOWN,
    EVENT_SHELLY_CLICK,
    INPUTS_EVENTS_DICT,
    LOGGER,
    MODELS_SUPPORTING_LIGHT_EFFECTS,
    POLLING_TIMEOUT_SEC,
    REST_SENSORS_UPDATE_INTERVAL,
    RPC_INPUTS_EVENTS_TYPES,
    RPC_RECONNECT_INTERVAL,
    RPC_SENSORS_POLLING_INTERVAL,
    SHBTN_MODELS,
    SLEEP_PERIOD_MULTIPLIER,
    UPDATE_PERIOD_MULTIPLIER,
)
from .utils import device_update_info, get_block_device_name, get_rpc_device_name


class BlockDeviceWrapper(update_coordinator.DataUpdateCoordinator):
    """Wrapper for a Shelly block based device with Home Assistant specific functions."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: BlockDevice
    ) -> None:
        """Initialize the Shelly device wrapper."""
        self.device_id: str | None = None

        if sleep_period := entry.data[CONF_SLEEP_PERIOD]:
            update_interval = SLEEP_PERIOD_MULTIPLIER * sleep_period
        else:
            update_interval = (
                UPDATE_PERIOD_MULTIPLIER * device.settings["coiot"]["update_period"]
            )

        device_name = (
            get_block_device_name(device) if device.initialized else entry.title
        )
        super().__init__(
            hass,
            LOGGER,
            name=device_name,
            update_interval=timedelta(seconds=update_interval),
        )
        self.hass = hass
        self.entry = entry
        self.device = device

        self._debounced_reload: Debouncer[Coroutine[Any, Any, None]] = Debouncer(
            hass,
            LOGGER,
            cooldown=ENTRY_RELOAD_COOLDOWN,
            immediate=False,
            function=self._async_reload_entry,
        )
        entry.async_on_unload(self._debounced_reload.async_cancel)
        self._last_cfg_changed: int | None = None
        self._last_mode: str | None = None
        self._last_effect: int | None = None

        entry.async_on_unload(
            self.async_add_listener(self._async_device_updates_handler)
        )
        self._last_input_events_count: dict = {}

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._handle_ha_stop)
        )

    async def _async_reload_entry(self) -> None:
        """Reload entry."""
        LOGGER.debug("Reloading entry %s", self.name)
        await self.hass.config_entries.async_reload(self.entry.entry_id)

    @callback
    def _async_device_updates_handler(self) -> None:
        """Handle device updates."""
        if not self.device.initialized:
            return

        assert self.device.blocks

        # For buttons which are battery powered - set initial value for last_event_count
        if self.model in SHBTN_MODELS and self._last_input_events_count.get(1) is None:
            for block in self.device.blocks:
                if block.type != "device":
                    continue

                if len(block.wakeupEvent) == 1 and block.wakeupEvent[0] == "button":
                    self._last_input_events_count[1] = -1

                break

        # Check for input events and config change
        cfg_changed = 0
        for block in self.device.blocks:
            if block.type == "device":
                cfg_changed = block.cfgChanged

            # For dual mode bulbs ignore change if it is due to mode/effect change
            if self.model in DUAL_MODE_LIGHT_MODELS:
                if "mode" in block.sensor_ids:
                    if self._last_mode != block.mode:
                        self._last_cfg_changed = None
                    self._last_mode = block.mode

            if self.model in MODELS_SUPPORTING_LIGHT_EFFECTS:
                if "effect" in block.sensor_ids:
                    if self._last_effect != block.effect:
                        self._last_cfg_changed = None
                    self._last_effect = block.effect

            if (
                "inputEvent" not in block.sensor_ids
                or "inputEventCnt" not in block.sensor_ids
            ):
                continue

            channel = int(block.channel or 0) + 1
            event_type = block.inputEvent
            last_event_count = self._last_input_events_count.get(channel)
            self._last_input_events_count[channel] = block.inputEventCnt

            if (
                last_event_count is None
                or last_event_count == block.inputEventCnt
                or event_type == ""
            ):
                continue

            if event_type in INPUTS_EVENTS_DICT:
                self.hass.bus.async_fire(
                    EVENT_SHELLY_CLICK,
                    {
                        ATTR_DEVICE_ID: self.device_id,
                        ATTR_DEVICE: self.device.settings["device"]["hostname"],
                        ATTR_CHANNEL: channel,
                        ATTR_CLICK_TYPE: INPUTS_EVENTS_DICT[event_type],
                        ATTR_GENERATION: 1,
                    },
                )
            else:
                LOGGER.warning(
                    "Shelly input event %s for device %s is not supported, please open issue",
                    event_type,
                    self.name,
                )

        if self._last_cfg_changed is not None and cfg_changed > self._last_cfg_changed:
            LOGGER.info(
                "Config for %s changed, reloading entry in %s seconds",
                self.name,
                ENTRY_RELOAD_COOLDOWN,
            )
            self.hass.async_create_task(self._debounced_reload.async_call())
        self._last_cfg_changed = cfg_changed

    async def _async_update_data(self) -> None:
        """Fetch data."""
        if sleep_period := self.entry.data.get(CONF_SLEEP_PERIOD):
            # Sleeping device, no point polling it, just mark it unavailable
            raise update_coordinator.UpdateFailed(
                f"Sleeping device did not update within {sleep_period} seconds interval"
            )

        LOGGER.debug("Polling Shelly Block Device - %s", self.name)
        try:
            async with async_timeout.timeout(POLLING_TIMEOUT_SEC):
                await self.device.update()
                device_update_info(self.hass, self.device, self.entry)
        except OSError as err:
            raise update_coordinator.UpdateFailed("Error fetching data") from err

    @property
    def model(self) -> str:
        """Model of the device."""
        return cast(str, self.entry.data["model"])

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.entry.unique_id)

    @property
    def sw_version(self) -> str:
        """Firmware version of the device."""
        return self.device.firmware_version if self.device.initialized else ""

    def async_setup(self) -> None:
        """Set up the wrapper."""
        dev_reg = device_registry.async_get(self.hass)
        entry = dev_reg.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self.mac)},
            manufacturer="Shelly",
            model=aioshelly.const.MODEL_NAMES.get(self.model, self.model),
            sw_version=self.sw_version,
            hw_version=f"gen{self.device.gen} ({self.model})",
            configuration_url=f"http://{self.entry.data[CONF_HOST]}",
        )
        self.device_id = entry.id
        self.device.subscribe_updates(self.async_set_updated_data)

    async def async_trigger_ota_update(self, beta: bool = False) -> None:
        """Trigger or schedule an ota update."""
        update_data = self.device.status["update"]
        LOGGER.debug("OTA update service - update_data: %s", update_data)

        if not update_data["has_update"] and not beta:
            LOGGER.warning("No OTA update available for device %s", self.name)
            return

        if beta and not update_data.get("beta_version"):
            LOGGER.warning(
                "No OTA update on beta channel available for device %s", self.name
            )
            return

        if update_data["status"] == "updating":
            LOGGER.warning("OTA update already in progress for %s", self.name)
            return

        new_version = update_data["new_version"]
        if beta:
            new_version = update_data["beta_version"]
        LOGGER.info(
            "Start OTA update of device %s from '%s' to '%s'",
            self.name,
            self.device.firmware_version,
            new_version,
        )
        try:
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                result = await self.device.trigger_ota_update(beta=beta)
        except (asyncio.TimeoutError, OSError) as err:
            LOGGER.exception("Error while perform ota update: %s", err)
        LOGGER.debug("Result of OTA update call: %s", result)

    def shutdown(self) -> None:
        """Shutdown the wrapper."""
        self.device.shutdown()

    @callback
    def _handle_ha_stop(self, _event: Event) -> None:
        """Handle Home Assistant stopping."""
        LOGGER.debug("Stopping BlockDeviceWrapper for %s", self.name)
        self.shutdown()


class ShellyDeviceRestWrapper(update_coordinator.DataUpdateCoordinator):
    """Rest Wrapper for a Shelly device with Home Assistant specific functions."""

    def __init__(
        self, hass: HomeAssistant, device: BlockDevice, entry: ConfigEntry
    ) -> None:
        """Initialize the Shelly device wrapper."""
        if (
            device.settings["device"]["type"]
            in BATTERY_DEVICES_WITH_PERMANENT_CONNECTION
        ):
            update_interval = (
                SLEEP_PERIOD_MULTIPLIER * device.settings["coiot"]["update_period"]
            )
        else:
            update_interval = REST_SENSORS_UPDATE_INTERVAL

        super().__init__(
            hass,
            LOGGER,
            name=get_block_device_name(device),
            update_interval=timedelta(seconds=update_interval),
        )
        self.device = device
        self.entry = entry

    async def _async_update_data(self) -> None:
        """Fetch data."""
        try:
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                LOGGER.debug("REST update for %s", self.name)
                await self.device.update_status()

                if self.device.status["uptime"] > 2 * REST_SENSORS_UPDATE_INTERVAL:
                    return
                old_firmware = self.device.firmware_version
                await self.device.update_shelly()
                if self.device.firmware_version == old_firmware:
                    return
                device_update_info(self.hass, self.device, self.entry)
        except OSError as err:
            raise update_coordinator.UpdateFailed("Error fetching data") from err

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.device.settings["device"]["mac"])


class RpcDeviceWrapper(update_coordinator.DataUpdateCoordinator):
    """Wrapper for a Shelly RPC based device with Home Assistant specific functions."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: RpcDevice
    ) -> None:
        """Initialize the Shelly device wrapper."""
        self.device_id: str | None = None

        device_name = get_rpc_device_name(device) if device.initialized else entry.title
        super().__init__(
            hass,
            LOGGER,
            name=device_name,
            update_interval=timedelta(seconds=RPC_RECONNECT_INTERVAL),
        )
        self.entry = entry
        self.device = device

        self._debounced_reload: Debouncer[Coroutine[Any, Any, None]] = Debouncer(
            hass,
            LOGGER,
            cooldown=ENTRY_RELOAD_COOLDOWN,
            immediate=False,
            function=self._async_reload_entry,
        )
        entry.async_on_unload(self._debounced_reload.async_cancel)

        entry.async_on_unload(
            self.async_add_listener(self._async_device_updates_handler)
        )
        self._last_event: dict[str, Any] | None = None

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._handle_ha_stop)
        )

    async def _async_reload_entry(self) -> None:
        """Reload entry."""
        LOGGER.debug("Reloading entry %s", self.name)
        await self.hass.config_entries.async_reload(self.entry.entry_id)

    @callback
    def _async_device_updates_handler(self) -> None:
        """Handle device updates."""
        if (
            not self.device.initialized
            or not self.device.event
            or self.device.event == self._last_event
        ):
            return

        self._last_event = self.device.event

        for event in self.device.event["events"]:
            event_type = event.get("event")
            if event_type is None:
                continue

            if event_type == "config_changed":
                LOGGER.info(
                    "Config for %s changed, reloading entry in %s seconds",
                    self.name,
                    ENTRY_RELOAD_COOLDOWN,
                )
                self.hass.async_create_task(self._debounced_reload.async_call())
            elif event_type in RPC_INPUTS_EVENTS_TYPES:
                self.hass.bus.async_fire(
                    EVENT_SHELLY_CLICK,
                    {
                        ATTR_DEVICE_ID: self.device_id,
                        ATTR_DEVICE: self.device.hostname,
                        ATTR_CHANNEL: event["id"] + 1,
                        ATTR_CLICK_TYPE: event["event"],
                        ATTR_GENERATION: 2,
                    },
                )

    async def _async_update_data(self) -> None:
        """Fetch data."""
        if self.device.connected:
            return

        try:
            LOGGER.debug("Reconnecting to Shelly RPC Device - %s", self.name)
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                await self.device.initialize()
                device_update_info(self.hass, self.device, self.entry)
        except OSError as err:
            raise update_coordinator.UpdateFailed("Device disconnected") from err

    @property
    def model(self) -> str:
        """Model of the device."""
        return cast(str, self.entry.data["model"])

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.entry.unique_id)

    @property
    def sw_version(self) -> str:
        """Firmware version of the device."""
        return self.device.firmware_version if self.device.initialized else ""

    def async_setup(self) -> None:
        """Set up the wrapper."""
        dev_reg = device_registry.async_get(self.hass)
        entry = dev_reg.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self.mac)},
            manufacturer="Shelly",
            model=aioshelly.const.MODEL_NAMES.get(self.model, self.model),
            sw_version=self.sw_version,
            hw_version=f"gen{self.device.gen} ({self.model})",
            configuration_url=f"http://{self.entry.data[CONF_HOST]}",
        )
        self.device_id = entry.id
        self.device.subscribe_updates(self.async_set_updated_data)

    async def async_trigger_ota_update(self, beta: bool = False) -> None:
        """Trigger an ota update."""

        update_data = self.device.status["sys"]["available_updates"]
        LOGGER.debug("OTA update service - update_data: %s", update_data)

        if not bool(update_data) or (not update_data.get("stable") and not beta):
            LOGGER.warning("No OTA update available for device %s", self.name)
            return

        if beta and not update_data.get(ATTR_BETA):
            LOGGER.warning(
                "No OTA update on beta channel available for device %s", self.name
            )
            return

        new_version = update_data.get("stable", {"version": ""})["version"]
        if beta:
            new_version = update_data.get(ATTR_BETA, {"version": ""})["version"]

        assert self.device.shelly
        LOGGER.info(
            "Start OTA update of device %s from '%s' to '%s'",
            self.name,
            self.device.firmware_version,
            new_version,
        )
        try:
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                await self.device.trigger_ota_update(beta=beta)
        except (asyncio.TimeoutError, OSError) as err:
            LOGGER.exception("Error while perform ota update: %s", err)

        LOGGER.debug("OTA update call successful")

    async def shutdown(self) -> None:
        """Shutdown the wrapper."""
        await self.device.shutdown()

    async def _handle_ha_stop(self, _event: Event) -> None:
        """Handle Home Assistant stopping."""
        LOGGER.debug("Stopping RpcDeviceWrapper for %s", self.name)
        await self.shutdown()


class RpcPollingWrapper(update_coordinator.DataUpdateCoordinator):
    """Polling Wrapper for a Shelly RPC based device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: RpcDevice
    ) -> None:
        """Initialize the RPC polling coordinator."""
        self.device_id: str | None = None

        device_name = get_rpc_device_name(device) if device.initialized else entry.title
        super().__init__(
            hass,
            LOGGER,
            name=device_name,
            update_interval=timedelta(seconds=RPC_SENSORS_POLLING_INTERVAL),
        )
        self.entry = entry
        self.device = device

    async def _async_update_data(self) -> None:
        """Fetch data."""
        if not self.device.connected:
            raise update_coordinator.UpdateFailed("Device disconnected")

        try:
            LOGGER.debug("Polling Shelly RPC Device - %s", self.name)
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                await self.device.update_status()
        except (OSError, aioshelly.exceptions.RPCTimeout) as err:
            raise update_coordinator.UpdateFailed("Device disconnected") from err

    @property
    def model(self) -> str:
        """Model of the device."""
        return cast(str, self.entry.data["model"])

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.entry.unique_id)
