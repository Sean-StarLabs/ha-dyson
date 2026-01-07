"""Binary sensor platform for dyson."""

from typing import Callable

from homeassistant.helpers import entity_registry as er
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import DysonEntity
from .const import DATA_DEVICES, DOMAIN

ICON_BIN_FULL = "mdi:delete-variant"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson binary sensor from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    # Cleanup: remove the legacy Tilt sensor (we’ll add it back once we see a reliable
    # robot-specific field that matches the Dyson app).
    ent_reg = er.async_get(hass)
    removed: list[str] = []
    for entry in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id):
        if entry.platform != DOMAIN:
            continue
        unique_id = entry.unique_id or ""
        if unique_id.endswith("-tilt"):
            ent_reg.async_remove(entry.entity_id)
            removed.append(entry.entity_id)
    if removed:
        schedule_save = getattr(ent_reg, "async_schedule_save", None)
        if callable(schedule_save):
            schedule_save()

    entities = []
    if hasattr(device, "is_charging"):
        entities.append(DysonVacuumBatteryChargingSensor(device, name))
    if hasattr(device, "is_bin_full"):
        entities.append(Dyson360HeuristBinFullSensor(device, name))
    async_add_entities(entities)


class DysonVacuumBatteryChargingSensor(DysonEntity, BinarySensorEntity):
    """Dyson vacuum battery charging sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        """Return if the sensor is on."""
        return bool(getattr(self._device, "is_charging", False))

    @property
    def device_class(self) -> str:
        """Return the device class of the sensor."""
        return BinarySensorDeviceClass.BATTERY_CHARGING

    @property
    def sub_name(self) -> str:
        """Return the name of the sensor."""
        return "Battery Charging"

    @property
    def sub_unique_id(self):
        """Return the sensor's unique id."""
        return "battery_charging"


class Dyson360HeuristBinFullSensor(DysonEntity, BinarySensorEntity):
    """Dyson 360 Heurist bin full sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        """Return if the sensor is on."""
        return bool(getattr(self._device, "is_bin_full", False))

    @property
    def icon(self) -> str:
        """Return the sensor icon."""
        return ICON_BIN_FULL

    @property
    def sub_name(self) -> str:
        """Return the name of the sensor."""
        return "Bin Full"

    @property
    def sub_unique_id(self):
        """Return the sensor's unique id."""
        return "bin_full"


class Dyson360VisNavBinFullSensor(DysonEntity, BinarySensorEntity):
    """Dyson 360 VisNav bin full sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        """Return if the sensor is on."""
        return bool(getattr(self._device, "is_bin_full", False))

    @property
    def icon(self) -> str:
        """Return the sensor icon."""
        return ICON_BIN_FULL

    @property
    def sub_name(self) -> str:
        """Return the name of the sensor."""
        return "Bin Full"

    @property
    def sub_unique_id(self):
        """Return the sensor's unique id."""
        return "bin_full"

