"""Select platform for dyson."""

from typing import Callable

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import DysonEntity
from .const import DATA_DEVICES, DOMAIN

AIR_QUALITY_TARGET_ENUM_TO_STR = {
    "OFF": "Off",
    "0004": "Good",
    "0002": "Default",
    "0003": "Sensitive",
    "0001": "Very Sensitive",
}

AIR_QUALITY_TARGET_STR_TO_ENUM = {
    value: key for key, value in AIR_QUALITY_TARGET_ENUM_TO_STR.items()
}

async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson sensor from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]
    entities = []
    if getattr(device, "air_quality_target", None) is not None:
        entities.append(DysonAirQualitySelect(device, name))
    async_add_entities(entities)


class DysonAirQualitySelect(DysonEntity, SelectEntity):
    """Air quality target for supported models."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(AIR_QUALITY_TARGET_STR_TO_ENUM.keys())

    @property
    def current_option(self) -> str:
        """Return the current selected option."""
        return AIR_QUALITY_TARGET_ENUM_TO_STR.get(self._device.air_quality_target, "Off")

    def select_option(self, option: str) -> None:
        """Configure the new selected option."""
        self._device.set_air_quality_target(AIR_QUALITY_TARGET_STR_TO_ENUM[option])

    @property
    def sub_name(self) -> str:
        """Return the name of the select."""
        return "Air Quality"

    @property
    def sub_unique_id(self):
        """Return the select's unique id."""
        return "air_quality"
