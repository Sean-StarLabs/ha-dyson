"""Vacuum platform for Dyson (cloud-only)."""

from typing import Any, Callable, Mapping

from homeassistant.components.vacuum import (
    ATTR_STATUS,
    VacuumActivity,
    VacuumEntityFeature,
    StateVacuumEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import DysonEntity
from .const import DATA_DEVICES, DOMAIN

class _FeatureMask(int):
    """Bitmask that supports `VacuumEntityFeature.X in supported_features`."""

    def __contains__(self, item: object) -> bool:
        try:
            return bool(int(self) & int(item))  # type: ignore[arg-type]
        except Exception:
            return False


SUPPORTED_FEATURE_MASK = int(
    VacuumEntityFeature.START
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.STATUS
)

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

    def _raw_state(self) -> str:
        return str(getattr(self._device, "state", "UNKNOWN"))

    def _friendly_status(self, raw_state: str) -> str:
        raw = raw_state.upper()
        if "FAULT" in raw:
            summary = str(getattr(self._device, "fault_summary", "") or "")
            if summary:
                return summary
            return "Fault (user action required)" if "USER" in raw else "Fault"
        if "PAUSED" in raw:
            return "Paused"
        if "CLEAN" in raw and "CHARG" in raw:
            return "Charging to continue cleaning"
        if "CHARG" in raw:
            return "Charging"
        if "DOCK" in raw or "INACTIVE" in raw:
            return "Docked"
        if "RETURN" in raw or "ABORT" in raw:
            return "Returning to dock"
        if "RUN" in raw or "CLEAN" in raw or "TRAVERS" in raw or "DISCOVER" in raw or "MAPPING" in raw:
            return "Cleaning"
        return raw_state

    @property
    def available(self) -> bool:
        return self._device.is_connected

    @property
    def supported_features(self) -> _FeatureMask:
        # HA core uses `VacuumEntityFeature.X in self.supported_features` checks.
        # Google Assistant expects supported_features to be an int-like bitmask.
        if bool(getattr(self._device, "has_fault", False)):
            return _FeatureMask(int(VacuumEntityFeature.STATUS))

        mask = int(VacuumEntityFeature.STATUS)
        if bool(getattr(self._device, "can_start", True)):
            mask |= int(VacuumEntityFeature.START)
        if bool(getattr(self._device, "can_pause", True)):
            mask |= int(VacuumEntityFeature.PAUSE)
        if bool(getattr(self._device, "can_return_to_base", True)):
            mask |= int(VacuumEntityFeature.RETURN_HOME)
        return _FeatureMask(mask)

    @property
    def status(self) -> str:
        return self._friendly_status(self._raw_state())

    @property
    def activity(self) -> VacuumActivity:
        raw = self._raw_state().upper()
        if "FAULT" in raw:
            return VacuumActivity.ERROR
        if "PAUSED" in raw:
            return VacuumActivity.PAUSED
        # Some models report that they are charging during a clean (e.g. "FULL_CLEAN_CHARGING").
        # Treat it as paused/charging rather than docked/idle.
        if "CLEAN" in raw and "CHARG" in raw:
            return VacuumActivity.PAUSED
        if "ABORT" in raw or "RETURN" in raw:
            return VacuumActivity.RETURNING
        if "RUN" in raw or "TRAVERS" in raw or "DISCOVER" in raw or "MAPPING" in raw:
            return VacuumActivity.CLEANING
        if "DOCK" in raw or "CHARG" in raw or "INACTIVE" in raw:
            return VacuumActivity.DOCKED
        return VacuumActivity.DOCKED

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        return {
            ATTR_POSITION: str(getattr(self._device, "position", "")),
            ATTR_STATUS: self.status,
            "dyson_state": self._raw_state(),
            "current_area": getattr(self._device, "current_zone_name", None),
            "selected_areas": getattr(self._device, "selected_zone_names", None),
            "has_fault": bool(getattr(self._device, "has_fault", False)),
            "fault_codes": getattr(self._device, "fault_codes", []),
            "bin_present": bool(getattr(self._device, "is_bin_present", True)),
            "tilt": bool(getattr(self._device, "tilt", False)),
        }

    def start(self) -> None:
        if bool(getattr(self._device, "has_fault", False)):
            raise HomeAssistantError(f"Dyson reports a fault: {self.status}")
        if not bool(getattr(self._device, "can_start", True)):
            raise HomeAssistantError(f"Dyson cannot start right now: {self.status}")
        if self.activity == VacuumActivity.PAUSED:
            self._device.resume()
        else:
            if callable(getattr(self._device, "clean", None)):
                self._device.clean()
            else:
                self._device.start()

    def pause(self) -> None:
        if bool(getattr(self._device, "has_fault", False)):
            raise HomeAssistantError(f"Dyson reports a fault: {self.status}")
        if not bool(getattr(self._device, "can_pause", True)):
            raise HomeAssistantError(f"Dyson cannot pause right now: {self.status}")
        self._device.pause()

    def return_to_base(self, **kwargs) -> None:
        if bool(getattr(self._device, "has_fault", False)):
            raise HomeAssistantError(f"Dyson reports a fault: {self.status}")
        if not bool(getattr(self._device, "can_return_to_base", True)):
            raise HomeAssistantError(f"Dyson cannot return to base right now: {self.status}")
        self._device.abort()
