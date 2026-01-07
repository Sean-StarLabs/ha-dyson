"""Select platform for dyson."""

from typing import Callable, Optional

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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

ROBOT_STRATEGY_ENUM_TO_STR = {
    "auto": "Auto",
    "quick": "Quick",
    "quiet": "Quiet",
    "boost": "Boost",
}

ROBOT_STRATEGY_STR_TO_ENUM = {v: k for k, v in ROBOT_STRATEGY_ENUM_TO_STR.items()}

async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson sensor from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    # Cleanup: global robot cleaning level is now controlled via the vacuum's
    # fan-speed UI (VacuumEntityFeature.FAN_SPEED). Remove the legacy duplicate.
    ent_reg = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id):
        if entry.platform != DOMAIN:
            continue
        unique_id = entry.unique_id or ""
        if unique_id.endswith("-cleaning-level"):
            ent_reg.async_remove(entry.entity_id)

    entities = []
    if getattr(device, "air_quality_target", None) is not None:
        entities.append(DysonAirQualitySelect(device, name))
    if callable(getattr(device, "clean_selected_zone", None)) and callable(
        getattr(device, "select_zone", None)
    ):
        entities.append(DysonRobotAreaLevelSelect(device, name))
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
    def available(self) -> bool:
        return not bool(getattr(self._device, "is_config_locked", False))

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

    # This is an active control used for cleaning, not just configuration.
    _attr_entity_category = None
    _ALL = "All"

    @property
    def available(self) -> bool:
        return not bool(getattr(self._device, "is_config_locked", False))

    @property
    def options(self) -> list[str]:
        zones = list(getattr(self._device, "zones", []) or [])
        labelled = _make_unique_labels([(z_id, z_name) for z_id, z_name in zones])
        return [self._ALL, *[label for _, label in labelled]]

    def _option_to_id(self) -> dict[str, str]:
        zones = list(getattr(self._device, "zones", []) or [])
        labelled = _make_unique_labels([(z_id, z_name) for z_id, z_name in zones])
        return {label: z_id for z_id, label in labelled}

    @property
    def current_option(self) -> Optional[str]:
        selected_ids = getattr(self._device, "selected_zone_ids", None)
        if isinstance(selected_ids, list):
            if len(selected_ids) == 0:
                return self._ALL
            # When multiple zones are selected, show the last explicitly selected zone
            # in the select and rely on the Selected Areas sensor for the full list.
            selected = getattr(self._device, "selected_zone_id", None) or selected_ids[-1]
        else:
            selected = getattr(self._device, "selected_zone_id", None)
            if not isinstance(selected, str) or not selected:
                return self._ALL

        zones = list(getattr(self._device, "zones", []) or [])
        labelled = _make_unique_labels([(z_id, z_name) for z_id, z_name in zones])
        for z_id, label in labelled:
            if z_id == selected:
                return label
        return self._ALL

    def select_option(self, option: str) -> None:
        if option == self._ALL:
            if callable(getattr(self._device, "clear_selected_zones", None)):
                self._device.clear_selected_zones()
            if callable(getattr(self._device, "clear_zone_cursor", None)):
                self._device.clear_zone_cursor()
            return
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


class DysonRobotAreaLevelSelect(DysonEntity, SelectEntity):
    """Set the cleaning level for the currently selected area (RB03/Vis Nav)."""

    _attr_entity_category = None
    _attr_options = list(ROBOT_STRATEGY_STR_TO_ENUM.keys())

    @property
    def available(self) -> bool:
        if bool(getattr(self._device, "is_config_locked", False)):
            return False
        # Also used to set the global strategy when Area=All.
        return True

    @property
    def current_option(self) -> Optional[str]:
        zone_id = getattr(self._device, "selected_zone_id", None)
        if not isinstance(zone_id, str) or not zone_id:
            strategy = getattr(self._device, "current_power_mode", None)
        else:
            strategy = getattr(self._device, "selected_zone_effective_strategy", None)
        if not isinstance(strategy, str) or not strategy:
            return "Auto"
        return ROBOT_STRATEGY_ENUM_TO_STR.get(strategy, "Auto")

    def select_option(self, option: str) -> None:
        zone_id = getattr(self._device, "selected_zone_id", None)
        strategy = ROBOT_STRATEGY_STR_TO_ENUM.get(option)
        if not strategy:
            return
        if not isinstance(zone_id, str) or not zone_id:
            # Area=All -> set global default strategy.
            if callable(getattr(self._device, "set_default_cleaning_strategy", None)):
                self._device.set_default_cleaning_strategy(strategy)
            return
        map_id = getattr(self._device, "selected_map_id", None)
        if not isinstance(map_id, str) or not map_id:
            return
        if callable(getattr(self._device, "set_zone_cleaning_strategy", None)):
            self._device.set_zone_cleaning_strategy(map_id, zone_id, strategy)

    @property
    def sub_name(self) -> str:
        return "Area Level"

    @property
    def sub_unique_id(self) -> str:
        return "area-level"
