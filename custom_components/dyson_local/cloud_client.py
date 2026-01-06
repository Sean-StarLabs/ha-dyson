"""Dyson cloud API client (MyDyson) used for AWS IoT MQTT provisioning.

This integration intentionally uses cloud-only control: it does not connect to
devices via local-network MQTT brokers.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional

from aiohttp import ClientResponseError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


DYSON_API_URL_GLOBAL = "https://appapi.cp.dyson.com"
DYSON_API_URL_CHINA = "https://appapi.cp.dyson.cn"


@dataclass(frozen=True, slots=True)
class DysonIoTCredentials:
    """AWS IoT credentials returned by Dyson cloud."""

    client_id: str
    custom_authorizer_name: str
    token_key: str
    token_signature: str
    token_value: str


@dataclass(frozen=True, slots=True)
class DysonIoTCredentialsResponse:
    endpoint: str
    iot_credentials: DysonIoTCredentials


@dataclass(frozen=True, slots=True)
class DysonPersistentMapZone:
    id: str
    name: str
    area: Optional[float]
    icon: Optional[str]


@dataclass(frozen=True, slots=True)
class DysonPersistentMapMetadata:
    id: str
    name: Optional[str]
    last_visited: Optional[str]
    zones_definition_last_updated_date: Optional[str]
    zones: list[DysonPersistentMapZone]


@dataclass(frozen=True, slots=True)
class DysonManifestMqtt:
    local_broker_credentials: str
    mqtt_root_topic_level: str
    remote_broker_type: str


@dataclass(frozen=True, slots=True)
class DysonManifestConnectedConfiguration:
    mqtt: DysonManifestMqtt


@dataclass(frozen=True, slots=True)
class DysonManifestDevice:
    category: str
    serial_number: str
    name: str
    product_name: str
    model: str
    type: str
    variant: Optional[str]
    connected_configuration: Optional[DysonManifestConnectedConfiguration]

    @property
    def mqtt_root_topic(self) -> Optional[str]:
        if not self.connected_configuration:
            return None
        return self.connected_configuration.mqtt.mqtt_root_topic_level


def _as_str(value: Any, *, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_optional_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _as_optional_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def extract_bearer_token(auth_info: Any) -> str:
    """Best-effort extraction of a Bearer token from libdyson auth structures."""

    if isinstance(auth_info, str) and auth_info:
        return auth_info

    token_attr = getattr(auth_info, "token", None)
    if isinstance(token_attr, str) and token_attr:
        return token_attr

    if isinstance(auth_info, dict):
        for key in ("token", "access_token", "accessToken", "Token"):
            value = auth_info.get(key)
            if isinstance(value, str) and value:
                return value

        for key in ("auth", "data", "result"):
            nested = auth_info.get(key)
            if isinstance(nested, dict):
                try:
                    return extract_bearer_token(nested)
                except ValueError:
                    pass

    raise ValueError("Unable to extract Dyson Bearer token from MyDyson auth info")


class DysonCloudClient:
    """Minimal Dyson cloud client used by this integration."""

    def __init__(self, hass: HomeAssistant, *, token: str, china: bool) -> None:
        self._session = async_get_clientsession(hass)
        self._token = token
        self._base_url = DYSON_API_URL_CHINA if china else DYSON_API_URL_GLOBAL

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def async_get_manifest(self) -> list[DysonManifestDevice]:
        url = f"{self._base_url}/v3/manifest"
        async with self._session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        if not isinstance(payload, list):
            raise ValueError("Unexpected Dyson manifest response (expected list)")

        devices: list[DysonManifestDevice] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue

            connected = raw.get("connectedConfiguration")
            connected_cfg: Optional[DysonManifestConnectedConfiguration] = None
            if isinstance(connected, dict):
                mqtt = connected.get("mqtt")
                if isinstance(mqtt, dict):
                    mqtt_root_topic_level = _as_str(mqtt.get("mqttRootTopicLevel"))
                    if not mqtt_root_topic_level:
                        # Fallback: some accounts/devices appear to omit mqttRootTopicLevel.
                        # Best-effort:
                        # - Robot vacuums use RBxx as the MQTT root topic.
                        # - Air devices use type (+variant) as the MQTT root topic.
                        category = _as_str(raw.get("category"))
                        model = _as_str(raw.get("model"))
                        if category == "robot" and model:
                            mqtt_root_topic_level = model.split("-", 1)[0]
                        else:
                            base = _as_str(raw.get("type"))
                            variant = raw.get("variant")
                            if isinstance(variant, str) and variant.strip():
                                mqtt_root_topic_level = (
                                    f"{base}{variant.strip().upper()}"
                                )
                            else:
                                mqtt_root_topic_level = base

                    # Robot vacuums use RBxx MQTT roots (e.g. RB03, RB05). Some manifests
                    # report a numeric product type (e.g. "277") here; prefer RBxx.
                    category = _as_str(raw.get("category"))
                    model = _as_str(raw.get("model"))
                    if category == "robot" and model.startswith("RB"):
                        mqtt_root_topic_level = model.split("-", 1)[0]

                    connected_cfg = DysonManifestConnectedConfiguration(
                        mqtt=DysonManifestMqtt(
                            local_broker_credentials=_as_str(
                                mqtt.get("localBrokerCredentials")
                            ),
                            mqtt_root_topic_level=mqtt_root_topic_level,
                            remote_broker_type=_as_str(mqtt.get("remoteBrokerType")),
                        )
                    )

            serial_number = _as_str(raw.get("serialNumber"))
            product_name = _as_str(raw.get("productName"))
            name = _as_str(raw.get("name")) or product_name or serial_number

            devices.append(
                DysonManifestDevice(
                    category=_as_str(raw.get("category")),
                    serial_number=serial_number,
                    name=name,
                    product_name=product_name,
                    model=_as_str(raw.get("model")),
                    type=_as_str(raw.get("type")),
                    variant=raw.get("variant") if isinstance(raw.get("variant"), str) else None,
                    connected_configuration=connected_cfg,
                )
            )

        return devices

    async def async_get_iot_credentials(
        self, serial_number: str
    ) -> DysonIoTCredentialsResponse:
        url = f"{self._base_url}/v2/authorize/iot-credentials"
        body = {"Serial": serial_number}
        try:
            async with self._session.post(url, headers=self._headers(), json=body) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except ClientResponseError as err:
            _LOGGER.error("Dyson IoT credentials request failed: %s", err)
            raise

        if not isinstance(payload, dict):
            raise ValueError("Unexpected Dyson IoT credentials response (expected object)")

        endpoint = _as_str(payload.get("Endpoint"))
        iot = payload.get("IoTCredentials")
        if not endpoint or not isinstance(iot, dict):
            raise ValueError("Unexpected Dyson IoT credentials response structure")

        creds = DysonIoTCredentials(
            client_id=_as_str(iot.get("ClientId")),
            custom_authorizer_name=_as_str(iot.get("CustomAuthorizerName")),
            token_key=_as_str(iot.get("TokenKey")) or "token",
            token_signature=_as_str(iot.get("TokenSignature")),
            token_value=_as_str(iot.get("TokenValue")),
        )
        return DysonIoTCredentialsResponse(endpoint=endpoint, iot_credentials=creds)

    async def async_get_persistent_map_metadata(
        self, serial_number: str
    ) -> list[DysonPersistentMapMetadata]:
        """Return persistent map metadata for Dyson robot vacuums (e.g. RB03 Vis Nav)."""

        url = f"{self._base_url}/v1/app/{serial_number}/persistent-map-metadata"
        async with self._session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        if not isinstance(payload, list):
            raise ValueError("Unexpected Dyson persistent map metadata response (expected list)")

        maps: list[DysonPersistentMapMetadata] = []
        for raw_map in payload:
            if not isinstance(raw_map, dict):
                continue

            zones_raw = raw_map.get("zones")
            zones: list[DysonPersistentMapZone] = []
            if isinstance(zones_raw, list):
                for raw_zone in zones_raw:
                    if not isinstance(raw_zone, dict):
                        continue
                    zone_id = _as_str(raw_zone.get("id"))
                    if not zone_id:
                        continue
                    zones.append(
                        DysonPersistentMapZone(
                            id=zone_id,
                            name=_as_str(raw_zone.get("name")) or zone_id,
                            area=_as_optional_float(raw_zone.get("area")),
                            icon=_as_optional_str(raw_zone.get("icon")),
                        )
                    )

            map_id = _as_str(raw_map.get("id"))
            if not map_id:
                continue

            maps.append(
                DysonPersistentMapMetadata(
                    id=map_id,
                    name=_as_optional_str(raw_map.get("name")),
                    last_visited=_as_optional_str(raw_map.get("lastVisited")),
                    zones_definition_last_updated_date=_as_optional_str(
                        raw_map.get("zonesDefinitionLastUpdatedDate")
                    ),
                    zones=zones,
                )
            )

        return maps
