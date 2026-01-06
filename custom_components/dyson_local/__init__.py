"""Support for Dyson devices.

This fork focuses on cloud-only control (AWS IoT over websockets) and does not
connect to devices via local-network MQTT brokers.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

from homeassistant.config_entries import SOURCE_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity import Entity

from .cloud.const import CONF_AUTH, CONF_REGION, DATA_ACCOUNT, DATA_DEVICES
from .const import (
    CONF_ACCOUNT_ENTRY_ID,
    CONF_CATEGORY,
    CONF_MODEL,
    CONF_MQTT_ROOT_TOPIC,
    CONF_PRODUCT_NAME,
    CONF_SERIAL,
    CONF_TYPE,
    DATA_COORDINATORS,
    DATA_DEVICES,
    DATA_DISCOVERY,
    DOMAIN,
)

from libdyson import MessageType
from libdyson.exceptions import DysonInvalidAuth, DysonNetworkError

from .cloud_client import DysonCloudClient, extract_bearer_token
from .cloud_devices import DysonDeviceInfo, create_cloud_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS_ROBOT = ["binary_sensor", "sensor", "vacuum"]
PLATFORMS_AIR = ["fan", "select", "sensor", "switch", "button", "climate", "humidifier"]


class DysonDevice(Protocol):
    """Subset of device interface used by entities in this integration."""

    serial: str
    device_type: str
    is_connected: bool

    def add_message_listener(self, listener): ...
    def remove_message_listener(self, listener): ...


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Dyson integration."""
    hass.data[DOMAIN] = {
        DATA_DEVICES: {},
    }
    return True


async def async_setup_account(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a MyDyson Account."""
    _LOGGER.debug("Setting up MyDyson Account for region: %s", entry.data[CONF_REGION])

    try:
        token = extract_bearer_token(entry.data[CONF_AUTH])
        cloud = DysonCloudClient(hass, token=token, china=entry.data[CONF_REGION] == "CN")
        devices = await cloud.async_get_manifest()
        _LOGGER.debug("Retrieved %d manifest devices from cloud", len(devices))
    except DysonNetworkError:
        _LOGGER.error("Cannot connect to Dyson cloud service.")
        raise ConfigEntryNotReady
    except DysonInvalidAuth:
        _LOGGER.error("Invalid authentication credentials for Dyson cloud service.")
        raise ConfigEntryNotReady
    except Exception as err:
        _LOGGER.error("Unexpected error retrieving devices: %s", str(err))
        raise ConfigEntryNotReady

    _LOGGER.debug("Starting device discovery flows for %d devices", len(devices))
    for device in devices:
        mqtt_root_topic = device.mqtt_root_topic
        if not mqtt_root_topic:
            _LOGGER.debug(
                "Skipping %s (%s) because no MQTT root topic in manifest",
                device.serial_number,
                device.product_name,
            )
            continue

        _LOGGER.debug(
            "Creating discovery flow for device: %s (category=%s, type=%s, model=%s, mqtt=%s)",
            device.name,
            device.category,
            device.type,
            device.model,
            mqtt_root_topic,
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_DISCOVERY},
                data={
                    "account_entry_id": entry.entry_id,
                    "serial_number": device.serial_number,
                    "name": device.name,
                    "product_name": device.product_name,
                    "category": device.category,
                    "type": device.type,
                    "model": device.model,
                    "variant": device.variant,
                    "mqtt_root_topic": mqtt_root_topic,
                },
            )
        )

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_ACCOUNT: entry.data[CONF_AUTH],
        DATA_DEVICES: devices,
        "china": entry.data[CONF_REGION] == "CN",
    }
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dyson from a config entry."""
    _LOGGER.debug("Setting up entry: %s", entry.entry_id)
    
    if CONF_REGION in entry.data:
        return await async_setup_account(hass, entry)

    account_entry_id = entry.data.get(CONF_ACCOUNT_ENTRY_ID)
    if not isinstance(account_entry_id, str) or not account_entry_id:
        raise ConfigEntryNotReady("Missing account reference for Dyson device")

    account_data = hass.data[DOMAIN].get(account_entry_id)
    if not account_data:
        raise ConfigEntryNotReady("Referenced Dyson account is not loaded")

    try:
        token = extract_bearer_token(account_data[DATA_ACCOUNT])
    except Exception as err:
        raise ConfigEntryNotReady(f"Unable to read Dyson token: {err}") from err

    cloud = DysonCloudClient(hass, token=token, china=bool(account_data.get("china")))
    info = DysonDeviceInfo(
        serial=str(entry.data.get(CONF_SERIAL)),
        name=str(entry.data.get("name") or entry.title or entry.data.get(CONF_SERIAL)),
        mqtt_root_topic=str(entry.data.get(CONF_MQTT_ROOT_TOPIC)),
        category=str(entry.data.get(CONF_CATEGORY) or ""),
        model=str(entry.data.get(CONF_MODEL) or ""),
        product_name=str(entry.data.get(CONF_PRODUCT_NAME) or ""),
        type=str(entry.data.get(CONF_TYPE) or ""),
    )

    device = create_cloud_device(hass, info=info, cloud=cloud)
    try:
        await device.async_start()
    except Exception as err:
        _LOGGER.error("Failed to start Dyson device %s: %s", info.serial, err)
        raise ConfigEntryNotReady from err

    hass.data[DOMAIN][DATA_DEVICES][entry.entry_id] = device

    platforms = PLATFORMS_ROBOT if info.category == "robot" else PLATFORMS_AIR
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading entry: %s", entry.entry_id)

    if CONF_REGION in entry.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True

    device = hass.data[DOMAIN][DATA_DEVICES].pop(entry.entry_id, None)
    platforms = PLATFORMS_ROBOT if entry.data.get(CONF_CATEGORY) == "robot" else PLATFORMS_AIR
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if device is not None:
        try:
            await device.async_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error stopping Dyson device %s", entry.entry_id)
    return unload_ok
class DysonEntity(Entity):
    """Dyson entity base class."""

    _MESSAGE_TYPE = MessageType.STATE

    def __init__(self, device: DysonDevice, name: str):
        """Initialize the entity."""
        self._device = device
        self._name = name

    async def async_added_to_hass(self) -> None:
        """Call when entity is added to hass."""
        self._device.add_message_listener(self._on_message)

    async def async_will_remove_from_hass(self) -> None:
        """Call when entity will be removed from hass."""
        try:
            self._device.remove_message_listener(self._on_message)
        except Exception as e:
            _LOGGER.debug("Error removing message listener for %s: %s", self.unique_id, e)

    def _on_message(self, message_type: MessageType) -> None:
        if self._MESSAGE_TYPE is None or message_type == self._MESSAGE_TYPE:
            self.schedule_update_ha_state()

    @property
    def should_poll(self) -> bool:
        """No polling needed."""
        return False

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        if self.sub_name is None:
            return self._name
        return f"{self._name} {self.sub_name}"

    @property
    def sub_name(self) -> Optional[str]:
        """Return sub name of the entity."""
        return None

    @property
    def unique_id(self) -> str:
        """Return the entity unique id."""
        if self.sub_unique_id is None:
            return self._device.serial
        return f"{self._device.serial}-{self.sub_unique_id}"

    @property
    def sub_unique_id(self) -> str:
        """Return the entity sub unique id."""
        return None

    @property
    def device_info(self) -> dict:
        """Return device info of the entity."""
        return {
            "identifiers": {(DOMAIN, self._device.serial)},
            "name": self._name,
            "manufacturer": "Dyson",
            "model": self._device.device_type,
        }

async def _async_register_device_with_discovery(
    hass: HomeAssistant, discovery: DysonDiscovery, device: DysonDevice, setup_entry, entry: ConfigEntry
) -> None:
    """Register device with discovery service with enhanced handling."""
    _LOGGER.debug("Registering device %s with discovery service", device.serial)
    
    # Check what devices are currently discovered
    with discovery._lock:
        discovered_devices = list(discovery._discovered.keys())
        _LOGGER.debug("Currently discovered devices: %s", discovered_devices)
        
        if device.serial in discovery._discovered:
            discovered_ip = discovery._discovered[device.serial]
            _LOGGER.debug("Device %s already discovered at %s, should trigger immediate callback", device.serial, discovered_ip)
    
    # Register the device with discovery
    await hass.async_add_executor_job(
        discovery.register_device, device, setup_entry
    )
    _LOGGER.debug("Registered device %s with discovery", device.serial)
    
    # Give discovery a moment to potentially call the setup callback
    # In case the device was already discovered
    await asyncio.sleep(0.5)
    
    # Check if the device was actually connected
    if entry.entry_id not in hass.data[DOMAIN][DATA_DEVICES]:
        # Check if we have a cached IP for this device
        device_ips = hass.data[DOMAIN].get("device_ips", {})
        if device.serial in device_ips:
            cached_ip = device_ips[device.serial]
            _LOGGER.debug("Found cached IP %s for device %s, attempting connection", cached_ip, device.serial)
            try:
                # Call setup_entry directly with the cached IP
                result = await hass.async_add_executor_job(
                    partial(setup_entry, cached_ip, is_discovery=True)
                )
                if result:
                    _LOGGER.debug("Successfully connected device %s via cached IP", device.serial)
                else:
                    _LOGGER.warning("Failed to connect device %s via cached IP", device.serial)
            except Exception as e:
                _LOGGER.error("Error attempting cached IP connection for device %s: %s", device.serial, e)
        else:
            # Check if we have preserved discovered data as fallback
            preserved_discovered = hass.data[DOMAIN].get("preserved_discovered", {})
            if device.serial in preserved_discovered:
                discovered_ip = preserved_discovered[device.serial]
                _LOGGER.debug("Found device %s at IP %s in preserved discovery data, attempting connection", device.serial, discovered_ip)
                try:
                    # Call setup_entry directly with the preserved IP
                    result = await hass.async_add_executor_job(
                        partial(setup_entry, discovered_ip, is_discovery=True)
                    )
                    if result:
                        _LOGGER.debug("Successfully connected device %s via preserved discovery IP", device.serial)
                    else:
                        _LOGGER.warning("Failed to connect device %s via preserved discovery IP", device.serial)
                except Exception as e:
                    _LOGGER.error("Error attempting preserved discovery connection for device %s: %s", device.serial, e)
            else:
                _LOGGER.info("Device %s will connect when discovered by the discovery service", device.serial)
                # For discovery-based devices, this is normal - they connect when discovered
                # Don't treat this as an error, the device will connect when found
