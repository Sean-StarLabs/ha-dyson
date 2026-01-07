
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.components.button import ButtonEntity, ButtonDeviceClass

from typing import Callable, Optional

from .const import DATA_COORDINATORS, DATA_DEVICES, DOMAIN

from . import DysonEntity, DysonDevice

import logging

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson button from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]


    entities = []

    if hasattr(device, "filter_life"):
        entities.append(DysonFilterResetButton(device, name))

    if callable(getattr(device, "clean_selected_zone", None)):
        entities.append(DysonRobotCleanAreaButton(device, name))
    if callable(getattr(device, "add_selected_zone", None)):
        entities.append(DysonRobotAddAreaButton(device, name))
    if callable(getattr(device, "clear_selected_zones", None)):
        entities.append(DysonRobotClearAreasButton(device, name))
    if callable(getattr(device, "clean_selected_zones", None)):
        entities.append(DysonRobotCleanAreasButton(device, name))

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


class DysonRobotCleanAreaButton(DysonEntity, ButtonEntity):
    """Trigger a zone clean using the currently selected area."""

    _attr_entity_category = None

    @property
    def sub_name(self) -> Optional[str]:
        return "Clean Area"

    @property
    def sub_unique_id(self) -> str:
        return "clean-area"

    def press(self) -> None:
        self._device.clean_selected_zone()


class DysonRobotAddAreaButton(DysonEntity, ButtonEntity):
    """Add the current area to the multi-area selection list."""

    _attr_entity_category = None

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
    def sub_name(self) -> Optional[str]:
        return "Clear Areas"

    @property
    def sub_unique_id(self) -> str:
        return "clear-areas"

    def press(self) -> None:
        self._device.clear_selected_zones()


class DysonRobotCleanAreasButton(DysonEntity, ButtonEntity):
    """Trigger a multi-zone clean using the selected areas list."""

    _attr_entity_category = None

    @property
    def sub_name(self) -> Optional[str]:
        return "Clean Areas"

    @property
    def sub_unique_id(self) -> str:
        return "clean-areas"

    def press(self) -> None:
        self._device.clean_selected_zones()
