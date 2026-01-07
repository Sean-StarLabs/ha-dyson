from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er

from typing import Callable, Optional
import logging

from .const import DATA_DEVICES, DOMAIN

from . import DysonEntity, DysonDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson button from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    # Cleanup: old robot clean buttons are now handled via the vacuum entity.
    # Remove the orphaned entity registry entry so it doesn't linger as "unavailable".
    ent_reg = er.async_get(hass)
    removed: list[str] = []
    for entry in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id):
        if entry.platform != DOMAIN:
            continue
        unique_id = entry.unique_id or ""
        if unique_id.endswith("-clean-areas") or unique_id.endswith("-clean-area"):
            ent_reg.async_remove(entry.entity_id)
            removed.append(entry.entity_id)
    if removed:
        schedule_save = getattr(ent_reg, "async_schedule_save", None)
        if callable(schedule_save):
            schedule_save()
        _LOGGER.warning("Removed legacy entities: %s", removed)


    entities = []

    if hasattr(device, "filter_life"):
        entities.append(DysonFilterResetButton(device, name))

    if callable(getattr(device, "add_selected_zone", None)):
        entities.append(DysonRobotAddAreaButton(device, name))
    if callable(getattr(device, "clear_selected_zones", None)):
        entities.append(DysonRobotClearAreasButton(device, name))

    async_add_entities(entities)


class DysonFilterResetButton(DysonEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def sub_name(self) -> Optional[str]:
        """Return the name of the Dyson button."""
        return "Reset Filter Life"

    @property
    def sub_unique_id(self) -> str:
        """Return the button's unique id."""
        return "reset-filter"

    def press(self) -> None:
        self._device.reset_filter()


class DysonRobotAddAreaButton(DysonEntity, ButtonEntity):
    """Add the current area to the multi-area selection list."""

    _attr_entity_category = None

    @property
    def available(self) -> bool:
        if bool(getattr(self._device, "is_config_locked", False)):
            return False
        # Disable when the "All" option is selected (no cursor zone).
        zone_id = getattr(self._device, "selected_zone_id", None)
        if not isinstance(zone_id, str) or not zone_id:
            return False
        return True

    @property
    def sub_name(self) -> Optional[str]:
        return "Add Area"

    @property
    def sub_unique_id(self) -> str:
        return "add-area"

    def press(self) -> None:
        self._device.add_selected_zone()


class DysonRobotClearAreasButton(DysonEntity, ButtonEntity):
    """Clear the multi-area selection list."""

    _attr_entity_category = None

    @property
    def available(self) -> bool:
        return not bool(getattr(self._device, "is_config_locked", False))

    @property
    def sub_name(self) -> Optional[str]:
        return "Clear Areas"

    @property
    def sub_unique_id(self) -> str:
        return "clear-areas"

    def press(self) -> None:
        self._device.clear_selected_zones()
