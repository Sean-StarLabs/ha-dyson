"""Cloud-backed Dyson device models (AWS IoT MQTT).

These classes provide (a subset of) the same interface that the existing
platform entities expect from libdyson devices, but without any local-network
MQTT connection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Callable, Optional

from aiohttp import ClientResponseError

from homeassistant.core import HomeAssistant

from libdyson import MessageType

from .aws_iot_mqtt import DysonAwsIotMqtt
from .cloud_client import (
    DysonCloudClient,
    DysonIoTCredentialsResponse,
    DysonPersistentMapMetadata,
)

_LOGGER = logging.getLogger(__name__)


def _normalise_key(key: str) -> str:
    # Dyson uses kebab-case in some command fields (e.g. mode-reason).
    # Convert to camelCase to make downstream handling consistent.
    out = ""
    upper = False
    for ch in key:
        if ch in "- ":
            upper = True
            continue
        if upper:
            out += ch.upper()
            upper = False
        else:
            out += ch
    return out


def normalise_keys(obj: Any) -> Any:
    if isinstance(obj, list):
        return [normalise_keys(v) for v in obj]
    if isinstance(obj, dict):
        return {_normalise_key(str(k)): normalise_keys(v) for k, v in obj.items()}
    return obj


Listener = Callable[[MessageType], None]


@dataclass(frozen=True, slots=True)
class DysonDeviceInfo:
    serial: str
    name: str
    mqtt_root_topic: str
    category: str
    model: str
    product_name: str
    type: str


class DysonCloudDevice:
    """Common base for cloud-backed devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        info: DysonDeviceInfo,
        cloud: DysonCloudClient,
    ) -> None:
        self._hass = hass
        self._info = info
        self._cloud = cloud
        self._mqtt: Optional[DysonAwsIotMqtt] = None
        self._listeners: list[Listener] = []

    @property
    def serial(self) -> str:
        return self._info.serial

    @property
    def device_type(self) -> str:
        # Used for HA device registry model field.
        return self._info.model or self._info.type or self._info.mqtt_root_topic

    @property
    def is_connected(self) -> bool:
        return bool(self._mqtt and self._mqtt.is_connected)

    def add_message_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_message_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, message_type: MessageType) -> None:
        for listener in list(self._listeners):
            try:
                listener(message_type)
            except Exception:  # noqa: BLE001 - isolate entity listeners
                _LOGGER.exception("Dyson message listener raised")

    def _topic(self, suffix: str) -> str:
        root = self._info.mqtt_root_topic
        serial = self._info.serial
        return f"{root}/{serial}/{suffix}".rstrip("/")

    @property
    def _command_topic(self) -> str:
        return self._topic("command")

    async def async_start(self) -> None:
        creds = await self._cloud.async_get_iot_credentials(self._info.serial)
        self._mqtt = DysonAwsIotMqtt(self._hass, creds=creds, on_message=self._on_message)
        # paho-mqtt TLS setup loads system certs and can block; run in executor.
        await self._hass.async_add_executor_job(self._mqtt.start)
        self._subscribe()
        # Subscriptions are async; requesting immediately can race and miss the response.
        # Delay slightly to ensure we receive the initial CURRENT-STATE.
        self._hass.async_create_task(self._async_request_initial_state())

    async def _async_request_initial_state(self) -> None:
        await asyncio.sleep(1)
        try:
            self._request_initial_state()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Initial state request failed for %s", self._info.serial)

    async def async_stop(self) -> None:
        if self._mqtt:
            self._mqtt.stop()
            self._mqtt = None

    def _subscribe(self) -> None:
        raise NotImplementedError

    def _request_initial_state(self) -> None:
        raise NotImplementedError

    def _on_message(self, topic: str, payload: bytes) -> None:
        raise NotImplementedError

    def _publish(self, message: dict[str, Any]) -> None:
        if not self._mqtt:
            raise RuntimeError("Device MQTT not started")
        self._mqtt.publish_json(self._command_topic, message)


class DysonCloudRobot(DysonCloudDevice):
    """Cloud-backed robot vacuum (Dyson360 protocol)."""

    def __init__(self, hass: HomeAssistant, *, info: DysonDeviceInfo, cloud: DysonCloudClient) -> None:
        super().__init__(hass, info=info, cloud=cloud)
        self._status: dict[str, Any] = {}
        self._maps: list[DysonPersistentMapMetadata] = []
        self._selected_map_id: Optional[str] = None
        self._selected_zone_id: Optional[str] = None
        self._selected_zone_ids: list[str] = []
        self._last_message_time: Optional[str] = None
        self._dust_by_map: dict[str, dict[str, float]] = {}
        self._last_dust_refresh = 0.0
        self._seen_current_state = False

    async def async_start(self) -> None:
        await super().async_start()
        self._hass.async_create_task(self.async_refresh_maps())

    async def async_refresh_maps(self) -> None:
        """Fetch persistent map metadata to enable zone/area controls in HA."""
        try:
            maps = await self._cloud.async_get_persistent_map_metadata(self._info.serial)
        except ClientResponseError as err:
            # Not all robot models support these endpoints.
            if err.status in (400, 401, 403, 404):
                _LOGGER.debug(
                    "Robot %s persistent map metadata not available (%s)",
                    self._info.serial,
                    err.status,
                )
                return
            _LOGGER.warning(
                "Robot %s persistent map metadata request failed: %s",
                self._info.serial,
                err,
            )
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Robot %s persistent map metadata request failed: %s",
                self._info.serial,
                err,
            )
            return

        self._maps = maps

        current_map_id = str(self._status.get("persistentMapId") or "")
        if current_map_id and any(m.id == current_map_id for m in maps):
            self._selected_map_id = current_map_id
        elif maps:
            self._selected_map_id = maps[0].id
        else:
            self._selected_map_id = None
            self._selected_zone_id = None
            self._selected_zone_ids = []

        if self._selected_map_id and not self._selected_zone_id:
            zones = self._zones_for_map(self._selected_map_id)
            if zones:
                self._selected_zone_id = zones[0].id
                if not self._selected_zone_ids:
                    self._selected_zone_ids = [zones[0].id]

        _LOGGER.debug(
            "Robot %s loaded %d persistent maps (selected_map=%s selected_zone=%s)",
            self._info.serial,
            len(self._maps),
            self._selected_map_id,
            self._selected_zone_id,
        )
        self._hass.async_create_task(self.async_refresh_dust_predictions())
        self._emit(MessageType.STATE)

    async def async_refresh_dust_predictions(self) -> None:
        """Fetch per-zone dust predictions (supported on 360 Vis Nav)."""
        now = time.monotonic()
        if self._last_dust_refresh and (now - self._last_dust_refresh) < 300:
            return
        self._last_dust_refresh = now

        try:
            dust_by_map = await self._cloud.async_get_recommended_cleans(self._info.serial)
        except ClientResponseError as err:
            if err.status in (400, 401, 403, 404):
                _LOGGER.debug(
                    "Robot %s recommended-cleans not available (%s)",
                    self._info.serial,
                    err.status,
                )
                return
            _LOGGER.debug(
                "Robot %s recommended-cleans request failed: %s",
                self._info.serial,
                err,
            )
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Robot %s recommended-cleans request failed: %s",
                self._info.serial,
                err,
            )
            return

        if isinstance(dust_by_map, dict):
            self._dust_by_map = dust_by_map
            self._emit(MessageType.STATE)

    def _subscribe(self) -> None:
        assert self._mqtt
        self._mqtt.subscribe([self._topic("status")])

    def _request_initial_state(self) -> None:
        # Request current state after connecting.
        self._publish({"msg": "REQUEST-CURRENT-STATE"})

    @property
    def battery_level(self) -> int:
        value = self._status.get("batteryChargeLevel")
        try:
            return int(value)
        except Exception:
            return 0

    @property
    def is_charging(self) -> bool:
        state = str(self._status.get("state") or "")
        return "CHARG" in state or "DOCK" in state

    @property
    def is_bin_full(self) -> bool:
        # Not currently exposed via MQTT in a uniform way across models.
        return False

    @property
    def state(self) -> str:
        return str(self._status.get("state") or "UNKNOWN")

    @property
    def current_power_mode(self) -> str:
        # Prefer Vis Nav cleaning strategy, else Eye/Heurist power mode.
        return str(
            self._status.get("currentCleaningStrategy")
            or self._status.get("defaultCleaningStrategy")
            or self._status.get("currentVacuumPowerMode")
            or self._status.get("defaultVacuumPowerMode")
            or "auto"
        )

    def set_default_cleaning_strategy(self, strategy: str) -> None:
        self._publish(
            {
                "msg": "STATE-SET",
                "mode-reason": "LAPP",
                "defaults": {"defaultCleaningStrategy": strategy},
            }
        )

    def _map_display_name(self, m: DysonPersistentMapMetadata) -> str:
        return m.name or m.id

    def _zones_for_map(self, map_id: str) -> list[Any]:
        for m in self._maps:
            if m.id == map_id:
                return list(m.zones)
        return []

    @property
    def maps(self) -> list[tuple[str, str]]:
        """Available persistent maps as (id, display_name)."""
        return [(m.id, self._map_display_name(m)) for m in self._maps]

    @property
    def selected_map_id(self) -> Optional[str]:
        return self._selected_map_id

    def select_map(self, map_id: str) -> None:
        if map_id == self._selected_map_id:
            return
        if not any(m.id == map_id for m in self._maps):
            raise ValueError(f"Unknown map id: {map_id}")
        self._selected_map_id = map_id
        zones = self._zones_for_map(map_id)
        self._selected_zone_id = zones[0].id if zones else None
        self._selected_zone_ids = [self._selected_zone_id] if self._selected_zone_id else []
        self._emit(MessageType.STATE)

    @property
    def zones(self) -> list[tuple[str, str]]:
        """Available zones for the selected map as (id, name)."""
        if not self._selected_map_id:
            return []
        zones = self._zones_for_map(self._selected_map_id)
        return [(z.id, z.name) for z in zones]

    @property
    def selected_zone_id(self) -> Optional[str]:
        return self._selected_zone_id

    def select_zone(self, zone_id: str) -> None:
        if zone_id == self._selected_zone_id:
            return
        if not self._selected_map_id:
            raise ValueError("No map selected")
        zones = self._zones_for_map(self._selected_map_id)
        if not any(z.id == zone_id for z in zones):
            raise ValueError(f"Unknown zone id: {zone_id}")
        self._selected_zone_id = zone_id
        self._emit(MessageType.STATE)

    @property
    def selected_zone_ids(self) -> list[str]:
        return list(self._selected_zone_ids)

    def clear_selected_zones(self) -> None:
        self._selected_zone_ids = []
        self._emit(MessageType.STATE)

    def add_selected_zone(self, zone_id: Optional[str] = None) -> None:
        """Add a zone to the multi-zone selection list."""
        zone_id = zone_id or self._selected_zone_id
        if not isinstance(zone_id, str) or not zone_id:
            return
        if not self._selected_map_id:
            return
        zones = self._zones_for_map(self._selected_map_id)
        if not any(z.id == zone_id for z in zones):
            raise ValueError(f"Unknown zone id: {zone_id}")
        if zone_id not in self._selected_zone_ids:
            self._selected_zone_ids.append(zone_id)
            self._emit(MessageType.STATE)

    def _zone_name(self, zone_id: str) -> Optional[str]:
        map_id = self._selected_map_id or str(self._status.get("persistentMapId") or "")
        if not map_id:
            return None
        zones = self._zones_for_map(map_id)
        for z in zones:
            if z.id == zone_id:
                return z.name
        return None

    @property
    def current_zone_name(self) -> Optional[str]:
        zone_id = self._status.get("zoneId")
        if isinstance(zone_id, str) and zone_id:
            return self._zone_name(zone_id) or zone_id
        return None

    @property
    def selected_zone_names(self) -> list[str]:
        out: list[str] = []
        for zone_id in self._selected_zone_ids:
            out.append(self._zone_name(zone_id) or zone_id)
        return out

    @property
    def last_message_time(self) -> Optional[str]:
        return self._last_message_time

    @property
    def selected_zones_dust_mg(self) -> Optional[float]:
        map_id = self._selected_map_id or str(self._status.get("persistentMapId") or "")
        if not map_id:
            return None
        dust_by_zone = self._dust_by_map.get(map_id)
        if not isinstance(dust_by_zone, dict):
            return None
        zones = [z for z in self._selected_zone_ids if isinstance(z, str) and z]
        if not zones and isinstance(self._selected_zone_id, str) and self._selected_zone_id:
            zones = [self._selected_zone_id]
        if not zones:
            return None
        total = 0.0
        found = False
        for zone_id in zones:
            weight = dust_by_zone.get(zone_id)
            if isinstance(weight, (int, float)):
                total += float(weight)
                found = True
        return total if found else None

    def clean_selected_zone(self) -> None:
        """Start a zone clean for the currently selected map/zone."""
        map_id = self._selected_map_id
        zone_id = self._selected_zone_id
        if not map_id or not zone_id:
            _LOGGER.warning(
                "Robot %s cannot start zone clean without a selected map/zone",
                self._info.serial,
            )
            return

        map_meta = next((m for m in self._maps if m.id == map_id), None)
        if not map_meta:
            _LOGGER.warning("Robot %s selected map %s not found", self._info.serial, map_id)
            return

        cleaning_programme = {
            "orderedZones": [],
            "persistentMapId": map_meta.id,
            "unorderedZones": [zone_id],
            "zonesDefinitionLastUpdatedDate": map_meta.zones_definition_last_updated_date,
        }
        self._publish(
            {
                "msg": "START",
                "mode-reason": "LAPP",
                "fullCleanType": "immediate",
                "cleaningMode": "zoneConfigured",
                "cleaningProgramme": cleaning_programme,
            }
        )

    def clean_selected_zones(self) -> None:
        """Start a zone clean for the currently selected map and selected zones list."""
        map_id = self._selected_map_id
        if not map_id:
            _LOGGER.warning(
                "Robot %s cannot start multi-zone clean without a selected map",
                self._info.serial,
            )
            return

        map_meta = next((m for m in self._maps if m.id == map_id), None)
        if not map_meta:
            _LOGGER.warning("Robot %s selected map %s not found", self._info.serial, map_id)
            return

        zones = [z for z in self._selected_zone_ids if isinstance(z, str) and z]
        if not zones and self._selected_zone_id:
            zones = [self._selected_zone_id]
        if not zones:
            _LOGGER.warning("Robot %s cannot start multi-zone clean without zones", self._info.serial)
            return

        valid_zone_ids = {z.id for z in map_meta.zones}
        zones = [z for z in zones if z in valid_zone_ids]
        if not zones:
            _LOGGER.warning(
                "Robot %s cannot start multi-zone clean: selected zones not in map",
                self._info.serial,
            )
            return

        cleaning_programme = {
            "orderedZones": [],
            "persistentMapId": map_meta.id,
            "unorderedZones": zones,
            "zonesDefinitionLastUpdatedDate": map_meta.zones_definition_last_updated_date,
        }
        self._publish(
            {
                "msg": "START",
                "mode-reason": "LAPP",
                "fullCleanType": "immediate",
                "cleaningMode": "zoneConfigured",
                "cleaningProgramme": cleaning_programme,
            }
        )

    @property
    def position(self) -> Any:
        return self._status.get("globalPosition")

    # Commands used by HA vacuum entity
    def start(self) -> None:
        self._publish({"msg": "START", "mode-reason": "LAPP", "fullCleanType": "immediate"})

    def pause(self) -> None:
        self._publish({"msg": "PAUSE", "mode-reason": "LAPP"})

    def resume(self) -> None:
        self._publish({"msg": "RESUME", "mode-reason": "LAPP"})

    def abort(self) -> None:
        self._publish({"msg": "ABORT", "mode-reason": "LAPP"})

    def _on_message(self, topic: str, payload: bytes) -> None:
        try:
            raw = json.loads(payload.decode("utf-8"))
        except Exception:
            _LOGGER.debug("Robot MQTT payload not JSON topic=%s payload=%r", topic, payload)
            return

        msg = normalise_keys(raw)
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("msg")
        if msg_type == "STATE-CHANGE":
            # Convert state-change format to current-state like the Matterbridge plugin does.
            msg = {
                **{k: v for k, v in msg.items() if not str(k).startswith("old")},
                "msg": "CURRENT-STATE",
                "state": msg.get("newstate") or msg.get("state"),
                "activeFaults": msg.get("newActiveFaults") or msg.get("activeFaults"),
                "outOfBoxState": msg.get("newOutOfBoxState") or msg.get("outOfBoxState"),
                "zoneId": msg.get("newZoneId") or msg.get("zoneId"),
            }
            msg_type = "CURRENT-STATE"

        if msg_type == "CURRENT-STATE":
            # Merge; clear a handful of keys that may disappear.
            for key in (
                "activeFaults",
                "cleanDuration",
                "cleanId",
                "cleaningProgramme",
                "faults",
                "globalPosition",
                "persistentMapId",
                "sessionId",
                "traverseTargetId",
                "zoneId",
                "zonesDefinitionVersion",
                "zoneStatus",
            ):
                self._status.pop(key, None)

            msg.pop("msg", None)
            time_value = msg.get("time")
            if isinstance(time_value, str) and time_value:
                self._last_message_time = time_value
            msg.pop("time", None)
            self._status.update(msg)
            if not self._seen_current_state:
                self._seen_current_state = True
                _LOGGER.warning(
                    "Robot %s initial CURRENT-STATE batteryChargeLevel=%r state=%r",
                    self._info.serial,
                    self._status.get("batteryChargeLevel"),
                    self._status.get("state"),
                )
            current_map_id = str(self._status.get("persistentMapId") or "")
            if current_map_id and current_map_id != (self._selected_map_id or ""):
                if any(m.id == current_map_id for m in self._maps):
                    self._selected_map_id = current_map_id
                    self._hass.async_create_task(self.async_refresh_dust_predictions())
            self._emit(MessageType.STATE)


class DysonCloudAir(DysonCloudDevice):
    """Cloud-backed air treatment device."""

    def __init__(self, hass: HomeAssistant, *, info: DysonDeviceInfo, cloud: DysonCloudClient) -> None:
        super().__init__(hass, info=info, cloud=cloud)
        self._product_state: dict[str, Any] = {}
        self._sensor_data: dict[str, Any] = {}

    def _subscribe(self) -> None:
        assert self._mqtt
        self._mqtt.subscribe(
            [
                self._topic("status/connection"),
                self._topic("status/current"),
                self._topic("status/faults"),
            ]
        )

    def _request_initial_state(self) -> None:
        # Request the current state and sensor data after connecting.
        self._publish({"msg": "REQUEST-CURRENT-STATE"})
        self._publish({"msg": "REQUEST-PRODUCT-ENVIRONMENT-CURRENT-SENSOR-DATA"})

    # Basic fan properties/methods used by platform entities
    @property
    def is_on(self) -> bool:
        return self._product_state.get("fpwr") == "ON"

    @property
    def auto_mode(self) -> bool:
        return self._product_state.get("auto") == "ON" or self._product_state.get("fmod") == "AUTO"

    @property
    def speed(self) -> Optional[int]:
        fnsp = self._product_state.get("fnsp")
        if fnsp in (None, "", "AUTO"):
            return None
        try:
            return int(str(fnsp))
        except Exception:
            return None

    @property
    def oscillation(self) -> bool:
        return self._product_state.get("oson") in ("ON", "OION")

    @property
    def night_mode(self) -> bool:
        return self._product_state.get("nmod") == "ON"

    @property
    def continuous_monitoring(self) -> bool:
        return self._product_state.get("rhtm") == "ON"

    @property
    def focus_mode(self) -> bool:
        return self._product_state.get("ffoc") == "ON"

    @property
    def front_airflow(self) -> bool:
        return self._product_state.get("fdir") == "ON"

    def turn_on(self) -> None:
        self.command_state_set({"fpwr": "ON"})

    def turn_off(self) -> None:
        self.command_state_set({"fpwr": "OFF"})

    def set_speed(self, speed: int) -> None:
        self.command_state_set({"fnsp": f"{int(speed):04d}"})

    def enable_auto_mode(self) -> None:
        self.command_state_set({"auto": "ON", "fmod": "AUTO"})

    def disable_auto_mode(self) -> None:
        self.command_state_set({"auto": "OFF", "fmod": "FAN"})

    def enable_oscillation(self, *args) -> None:
        # Some entity code passes (angle_low, angle_high) for supported models.
        if len(args) == 2 and all(isinstance(v, int) for v in args):
            low, high = args
            self.command_state_set({"oson": "ON", "osal": low, "osau": high})
            return
        self.command_state_set({"oson": "ON"})

    def disable_oscillation(self) -> None:
        self.command_state_set({"oson": "OFF"})

    def enable_night_mode(self) -> None:
        self.command_state_set({"nmod": "ON"})

    def disable_night_mode(self) -> None:
        self.command_state_set({"nmod": "OFF"})

    def enable_continuous_monitoring(self) -> None:
        self.command_state_set({"rhtm": "ON"})

    def disable_continuous_monitoring(self) -> None:
        self.command_state_set({"rhtm": "OFF"})

    def enable_focus_mode(self) -> None:
        self.command_state_set({"ffoc": "ON"})

    def disable_focus_mode(self) -> None:
        self.command_state_set({"ffoc": "OFF"})

    def enable_front_airflow(self) -> None:
        self.command_state_set({"fdir": "ON"})

    def disable_front_airflow(self) -> None:
        self.command_state_set({"fdir": "OFF"})

    def set_sleep_timer(self, minutes: int) -> None:
        self.command_state_set({"sltm": f"{int(minutes):04d}"})

    def disable_sleep_timer(self) -> None:
        self.command_state_set({"sltm": "OFF"})

    def reset_filter(self) -> None:
        # Reset filter life values where supported; these keys are device-specific.
        self.command_state_set({"rstf": "RSTF"})

    # Link family air quality target
    @property
    def air_quality_target(self) -> Optional[str]:
        qtar = self._product_state.get("qtar")
        return str(qtar) if isinstance(qtar, str) and qtar else None

    def set_air_quality_target(self, target: str) -> None:
        self.command_state_set({"qtar": target})

    # Heating (Hot+Cool)
    @property
    def heat_mode_is_on(self) -> bool:
        return self._product_state.get("hmod") == "HEAT"

    @property
    def heat_status_is_on(self) -> bool:
        return self._product_state.get("hsta") == "HEAT"

    @property
    def heat_target(self) -> int:
        # Kelvin (rounded).
        hmax = self._product_state.get("hmax")
        try:
            if hmax in (None, "", "OFF"):
                return 0
            return int(int(str(hmax)) / 10)
        except Exception:
            return 0

    def set_heat_target(self, kelvin: int) -> None:
        self.command_state_set({"hmax": int(kelvin)})

    def enable_heat_mode(self) -> None:
        self.command_state_set({"hmod": "HEAT"})

    def disable_heat_mode(self) -> None:
        self.command_state_set({"hmod": "OFF"})

    # Humidification (Humidify+Cool)
    @property
    def humidification(self) -> bool:
        return self._product_state.get("hume") == "HUMD"

    @property
    def humidification_auto_mode(self) -> bool:
        return self._product_state.get("haut") == "ON"

    @property
    def target_humidity(self) -> Optional[int]:
        humt = self._product_state.get("humt")
        try:
            if humt in (None, "", "OFF"):
                return None
            return int(str(humt))
        except Exception:
            return None

    def enable_humidification(self) -> None:
        self.command_state_set({"hume": "HUMD"})

    def disable_humidification(self) -> None:
        self.command_state_set({"hume": "OFF"})

    def set_target_humidity(self, humidity: int) -> None:
        self.command_state_set({"humt": int(humidity)})

    def enable_humidification_auto_mode(self) -> None:
        self.command_state_set({"haut": "ON"})

    def disable_humidification_auto_mode(self) -> None:
        self.command_state_set({"haut": "OFF"})

    # Oscillation angles (Pure Cool / Hot+Cool families)
    @property
    def oscillation_angle_low(self) -> int:
        try:
            return int(str(self._product_state.get("osal") or 0))
        except Exception:
            return 0

    @property
    def oscillation_angle_high(self) -> int:
        try:
            return int(str(self._product_state.get("osau") or 0))
        except Exception:
            return 0

    def set_oscillation_angles(self, low: int, high: int) -> None:
        self.command_state_set({"osal": int(low), "osau": int(high)})

    # Big+Quiet tilt sensor
    @property
    def tilt(self) -> bool:
        return self._product_state.get("tilt") == "TILT"

    # Environmental properties (match expectations of current sensors.py)
    @property
    def temperature(self) -> float:
        # Kelvin (as used by existing entities).
        tact = self._sensor_data.get("tact")
        try:
            if tact in (None, "", "OFF"):
                return -1
            return int(str(tact)) / 10
        except Exception:
            return -1

    @property
    def humidity(self) -> float:
        hact = self._sensor_data.get("hact")
        try:
            if hact in (None, "", "OFF"):
                return -1
            return int(str(hact))
        except Exception:
            return -1

    @property
    def particulate_matter_2_5(self) -> float:
        key = "p25r" if "p25r" in self._sensor_data else "pm25"
        v = self._sensor_data.get(key)
        try:
            if v in (None, "", "OFF"):
                return -1
            return float(int(str(v)))
        except Exception:
            return -1

    @property
    def particulate_matter_10(self) -> float:
        key = "p10r" if "p10r" in self._sensor_data else "pm10"
        v = self._sensor_data.get(key)
        try:
            if v in (None, "", "OFF"):
                return -1
            return float(int(str(v)))
        except Exception:
            return -1

    @property
    def volatile_organic_compounds(self) -> float:
        key = "va10" if "va10" in self._sensor_data else "vact"
        v = self._sensor_data.get(key)
        try:
            if v in (None, "", "OFF"):
                return -1
            return float(int(str(v)))
        except Exception:
            return -1

    @property
    def nitrogen_dioxide(self) -> float:
        v = self._sensor_data.get("noxl")
        try:
            if v in (None, "", "OFF"):
                return -1
            return float(int(str(v)))
        except Exception:
            return -1

    @property
    def formaldehyde(self) -> float:
        key = "hchr" if "hchr" in self._sensor_data else "hcho"
        v = self._sensor_data.get(key)
        try:
            if v in (None, "", "OFF"):
                return -1
            # Dyson reports µg/m³; HA entity expects mg/m³.
            return float(int(str(v))) / 1000.0
        except Exception:
            return -1

    @property
    def carbon_dioxide(self) -> float:
        v = self._sensor_data.get("co2r")
        try:
            if v in (None, "", "OFF"):
                return -1
            return float(int(str(v)))
        except Exception:
            return -1

    # Filter life properties
    @property
    def hepa_filter_life(self) -> Optional[int]:
        v = self._product_state.get("hflr")
        if v in (None, "", "OFF"):
            return None
        try:
            return int(str(v))
        except Exception:
            return None

    @property
    def carbon_filter_life(self) -> Optional[int]:
        v = self._product_state.get("cflr")
        if v in (None, "", "OFF", "INV"):
            return None
        try:
            return int(str(v))
        except Exception:
            return None

    @property
    def filter_life(self) -> Optional[int]:
        # Link models report filter life in hours in filf.
        v = self._product_state.get("filf")
        if v in (None, "", "OFF"):
            return None
        try:
            return int(str(v))
        except Exception:
            return None

    @property
    def time_until_next_clean(self) -> int:
        # Humidify+Cool: cltr is hours until next clean.
        v = self._product_state.get("cltr")
        try:
            if v in (None, "", "OFF"):
                return -1
            return int(str(v))
        except Exception:
            return -1

    def command_state_set(self, product_state: dict[str, Any]) -> None:
        # Dyson expects 4-digit strings for most numeric keys.
        numeric_keys = {
            "hmax",
            "hflr",
            "filf",
            "osal",
            "osau",
            "humt",
            "rect",
            "sltm",
        }
        data: dict[str, Any] = {}
        for key, value in product_state.items():
            if value is None:
                continue
            if key in numeric_keys and isinstance(value, (int, float)):
                # hmax is deci-Kelvin; callers provide Kelvin.
                if key == "hmax":
                    data[key] = f"{int(round(value)) * 10:04d}"
                else:
                    data[key] = f"{int(round(value)):04d}"
            else:
                data[key] = value
        self._publish({"msg": "STATE-SET", "mode-reason": "LAPP", "data": data})

    def _on_message(self, topic: str, payload: bytes) -> None:
        try:
            raw = json.loads(payload.decode("utf-8"))
        except Exception:
            _LOGGER.debug("Air MQTT payload not JSON topic=%s payload=%r", topic, payload)
            return

        msg = normalise_keys(raw)
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("msg")
        if msg_type == "CURRENT-STATE":
            state = msg.get("productState")
            if isinstance(state, dict):
                self._product_state.update(state)
                self._emit(MessageType.STATE)
        elif msg_type == "STATE-CHANGE":
            # productState is a dict of {key: [old,new]}.
            changes = msg.get("productState")
            if isinstance(changes, dict):
                for k, v in changes.items():
                    if isinstance(v, list) and len(v) == 2:
                        self._product_state[k] = v[1]
                self._emit(MessageType.STATE)
        elif msg_type == "ENVIRONMENTAL-CURRENT-SENSOR-DATA":
            data = msg.get("data")
            if isinstance(data, dict):
                self._sensor_data.update(data)
                self._emit(MessageType.ENVIRONMENTAL)


def create_cloud_device(
    hass: HomeAssistant,
    *,
    info: DysonDeviceInfo,
    cloud: DysonCloudClient,
) -> DysonCloudDevice:
    if info.category == "robot":
        return DysonCloudRobot(hass, info=info, cloud=cloud)
    return DysonCloudAir(hass, info=info, cloud=cloud)
