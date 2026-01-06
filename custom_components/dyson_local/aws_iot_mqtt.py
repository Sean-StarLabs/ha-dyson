"""AWS IoT MQTT-over-WebSocket client used by Dyson devices (cloud-only)."""

from __future__ import annotations

import json
import logging
import ssl
import threading
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

from homeassistant.core import HomeAssistant

from .cloud_client import DysonIoTCredentialsResponse

_LOGGER = logging.getLogger(__name__)


MessageCallback = Callable[[str, bytes], None]


def _make_headers(creds: DysonIoTCredentialsResponse) -> dict[str, str]:
    iot = creds.iot_credentials
    return {
        iot.token_key or "token": iot.token_value,
        "X-Amz-CustomAuthorizer-Name": iot.custom_authorizer_name,
        "X-Amz-CustomAuthorizer-Signature": iot.token_signature,
    }


class DysonAwsIotMqtt:
    """Threaded AWS IoT MQTT client (websockets)."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        creds: DysonIoTCredentialsResponse,
        on_message: MessageCallback,
    ) -> None:
        self._hass = hass
        self._creds = creds
        self._on_message = on_message

        self._client: Optional[mqtt.Client] = None
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._subscriptions: set[str] = set()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._client is not None:
            return

        iot = self._creds.iot_credentials
        client = mqtt.Client(client_id=iot.client_id, transport="websockets")
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.ws_set_options(path="/mqtt", headers=_make_headers(self._creds))

        def _on_connect(_client: mqtt.Client, _userdata: Any, _flags: Any, rc: int) -> None:
            if rc == 0:
                self._connected.set()
                _LOGGER.debug("AWS IoT MQTT connected")
                # Resubscribe on reconnect.
                for topic in list(self._subscriptions):
                    try:
                        _client.subscribe(topic, qos=1)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Failed to resubscribe to %s", topic)
            else:
                _LOGGER.error("AWS IoT MQTT connect failed rc=%s", rc)

        def _on_disconnect(_client: mqtt.Client, _userdata: Any, rc: int) -> None:
            self._connected.clear()
            if self._stopping.is_set():
                return
            _LOGGER.warning("AWS IoT MQTT disconnected rc=%s", rc)

        def _on_message(
            _client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage
        ) -> None:
            # Bounce back onto HA loop.
            topic = msg.topic
            payload = bytes(msg.payload)
            self._hass.loop.call_soon_threadsafe(self._on_message, topic, payload)

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message

        endpoint = self._creds.endpoint
        _LOGGER.debug("Connecting AWS IoT MQTT to %s", endpoint)

        client.connect_async(endpoint, port=443, keepalive=10)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        self._stopping.set()
        self._connected.clear()
        if self._client is None:
            return
        try:
            self._client.disconnect()
            self._client.loop_stop()
        finally:
            self._client = None

    def subscribe(self, topics: list[str]) -> None:
        if not self._client:
            raise RuntimeError("MQTT client not started")
        for topic in topics:
            self._subscriptions.add(topic)
            self._client.subscribe(topic, qos=1)

    def publish_json(self, topic: str, payload: dict[str, Any]) -> None:
        if not self._client:
            raise RuntimeError("MQTT client not started")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._client.publish(topic, data, qos=1, retain=False)
