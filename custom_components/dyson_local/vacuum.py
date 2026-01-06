"""Vacuum platform for Dyson (cloud-only)."""

from typing import Any, Callable, List, Mapping

from homeassistant.components.vacuum import (
    ATTR_STATUS,
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_ERROR,
    STATE_RETURNING,
    VacuumEntityFeature,
    StateVacuumEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, STATE_PAUSED
from homeassistant.core import HomeAssistant

from . import DysonEntity
from .const import DATA_DEVICES, DOMAIN

SUPPORTED_FEATURES = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.FAN_SPEED
    | VacuumEntityFeature.STATUS
    | VacuumEntityFeature.STATE
    | VacuumEntityFeature.BATTERY
)

FAN_SPEED_LIST = ["Auto", "Quick", "Quiet", "Boost"]
FAN_SPEED_TO_STRATEGY = {
    "Auto": "auto",
    "Quick": "quick",
    "Quiet": "quiet",
    "Boost": "boost",
}
STRATEGY_TO_FAN_SPEED = {v: k for k, v in FAN_SPEED_TO_STRATEGY.items()}

ATTR_POSITION = "position"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson vacuum from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]
    async_add_entities([DysonCloudVacuumEntity(device, name)])


class DysonCloudVacuumEntity(DysonEntity, StateVacuumEntity):
    """Dyson robot vacuum entity (cloud-only)."""

    @property
    def available(self) -> bool:
        return self._device.is_connected

    @property
    def supported_features(self) -> int:
        return SUPPORTED_FEATURES

    @property
    def battery_level(self) -> int:
        return int(getattr(self._device, "battery_level", 0))

    @property
    def status(self) -> str:
        return str(getattr(self._device, "state", "UNKNOWN"))

    @property
    def state(self) -> str:
        raw = self.status.upper()
        if "FAULT" in raw:
            return STATE_ERROR
        if "PAUSED" in raw:
            return STATE_PAUSED
        if "ABORT" in raw or "RETURN" in raw:
            return STATE_RETURNING
        if "RUN" in raw or "TRAVERS" in raw or "DISCOVER" in raw or "MAPPING" in raw:
            return STATE_CLEANING
        if "DOCK" in raw or "CHARG" in raw or "INACTIVE" in raw:
            return STATE_DOCKED
        return STATE_DOCKED

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        return {
            ATTR_POSITION: str(getattr(self._device, "position", "")),
            ATTR_STATUS: self.status,
        }

    @property
    def fan_speed(self) -> str:
        strategy = str(getattr(self._device, "current_power_mode", "auto"))
        return STRATEGY_TO_FAN_SPEED.get(strategy, "Auto")

    @property
    def fan_speed_list(self) -> List[str]:
        return FAN_SPEED_LIST

    def set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        strategy = FAN_SPEED_TO_STRATEGY.get(fan_speed)
        if strategy:
            self._device.set_default_cleaning_strategy(strategy)

    def start(self) -> None:
        if self.state == STATE_PAUSED:
            self._device.resume()
        else:
            self._device.start()

    def pause(self) -> None:
        self._device.pause()

    def return_to_base(self, **kwargs) -> None:
        self._device.abort()
