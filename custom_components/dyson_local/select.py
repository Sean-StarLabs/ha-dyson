"""Select platform for dyson."""

from typing import Callable, Optional

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
    if callable(getattr(device, "clean_selected_zone", None)) and callable(
        getattr(device, "select_zone", None)
    ):
        entities.append(DysonRobotMapSelect(device, name))
        entities.append(DysonRobotAreaSelect(device, name))
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


def _make_unique_labels(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return (id, label) ensuring label uniqueness by suffixing duplicates with the id."""
    counts: dict[str, int] = {}
    for _, label in pairs:
        counts[label] = counts.get(label, 0) + 1
    out: list[tuple[str, str]] = []
    for item_id, label in pairs:
        if counts.get(label, 0) > 1:
            out.append((item_id, f"{label} ({item_id})"))
        else:
            out.append((item_id, label))
    return out


class DysonRobotMapSelect(DysonEntity, SelectEntity):
    """Select the active persistent map (RB03/Vis Nav)."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def options(self) -> list[str]:
        maps = list(getattr(self._device, "maps", []) or [])
        labelled = _make_unique_labels([(m_id, m_name) for m_id, m_name in maps])
        return [label for _, label in labelled]

    def _option_to_id(self) -> dict[str, str]:
        maps = list(getattr(self._device, "maps", []) or [])
        labelled = _make_unique_labels([(m_id, m_name) for m_id, m_name in maps])
        return {label: m_id for m_id, label in labelled}

    @property
    def current_option(self) -> Optional[str]:
        selected = getattr(self._device, "selected_map_id", None)
        if not isinstance(selected, str) or not selected:
            return self.options[0] if self.options else None

        maps = list(getattr(self._device, "maps", []) or [])
        labelled = _make_unique_labels([(m_id, m_name) for m_id, m_name in maps])
        for m_id, label in labelled:
            if m_id == selected:
                return label
        return self.options[0] if self.options else None

    def select_option(self, option: str) -> None:
        option_to_id = self._option_to_id()
        map_id = option_to_id.get(option)
        if map_id:
            self._device.select_map(map_id)

    @property
    def sub_name(self) -> str:
        return "Map"

    @property
    def sub_unique_id(self) -> str:
        return "map"


class DysonRobotAreaSelect(DysonEntity, SelectEntity):
    """Select a zone/area for the current map (RB03/Vis Nav)."""

    _attr_entity_category = EntityCategory.CONFIG

    @property
    def options(self) -> list[str]:
        zones = list(getattr(self._device, "zones", []) or [])
        labelled = _make_unique_labels([(z_id, z_name) for z_id, z_name in zones])
        return [label for _, label in labelled]

    def _option_to_id(self) -> dict[str, str]:
        zones = list(getattr(self._device, "zones", []) or [])
        labelled = _make_unique_labels([(z_id, z_name) for z_id, z_name in zones])
        return {label: z_id for z_id, label in labelled}

    @property
    def current_option(self) -> Optional[str]:
        selected = getattr(self._device, "selected_zone_id", None)
        if not isinstance(selected, str) or not selected:
            return self.options[0] if self.options else None

        zones = list(getattr(self._device, "zones", []) or [])
        labelled = _make_unique_labels([(z_id, z_name) for z_id, z_name in zones])
        for z_id, label in labelled:
            if z_id == selected:
                return label
        return self.options[0] if self.options else None

    def select_option(self, option: str) -> None:
        option_to_id = self._option_to_id()
        zone_id = option_to_id.get(option)
        if zone_id:
            self._device.select_zone(zone_id)

    @property
    def sub_name(self) -> str:
        return "Area"

    @property
    def sub_unique_id(self) -> str:
        return "area"
