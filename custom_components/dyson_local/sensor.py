"""Sensor platform for Dyson (cloud-only)."""

from datetime import datetime
from typing import Callable, Union, Optional

from libdyson.const import MessageType

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass, SensorEntity
from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    CONF_NAME,
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from . import DysonEntity, DysonDevice
from .const import CONF_CATEGORY, DATA_DEVICES, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up Dyson sensor from a config entry."""
    device = hass.data[DOMAIN][DATA_DEVICES][config_entry.entry_id]
    name = config_entry.data[CONF_NAME]

    # Cleanup: remove legacy dust sensors (replaced by per-area dust entities).
    ent_reg = er.async_get(hass)
    removed: list[str] = []
    for entry in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id):
        if entry.platform != DOMAIN:
            continue
        unique_id = entry.unique_id or ""
        if unique_id.endswith("-selected_dust_mg") or unique_id.endswith("-dust_by_area"):
            ent_reg.async_remove(entry.entity_id)
            removed.append(entry.entity_id)
    if removed:
        schedule_save = getattr(ent_reg, "async_schedule_save", None)
        if callable(schedule_save):
            schedule_save()

    if config_entry.data.get(CONF_CATEGORY) == "robot":
        entities = [
            DysonBatterySensor(device, name),
            DysonRobotCurrentAreaSensor(device, name),
            DysonRobotSelectedAreasSensor(device, name),
            DysonRobotLastMessageTimeSensor(device, name),
        ]

        # Add per-area dust prediction sensors (Vis Nav).
        zones = getattr(device, "zones", None)
        if isinstance(zones, list):
            for zone_id, zone_name in zones:
                if isinstance(zone_id, str) and zone_id and isinstance(zone_name, str) and zone_name:
                    entities.append(DysonRobotAreaDustSensor(device, name, zone_id, zone_name))
    else:
        entities = [
            DysonHumiditySensor(device, name),
            DysonTemperatureSensor(device, name),
            DysonVOCSensor(device, name),
        ]

        entities.extend(
            [
                DysonPM25Sensor(device, name),
                DysonPM10Sensor(device, name),
                DysonNO2Sensor(device, name),
                DysonHCHOSensor(device, name),
                DysonCarbonDioxideSensor(device, name),
            ]
        )
        if getattr(device, "filter_life", None) is not None:
            entities.extend(
                [
                    DysonFilterLifeSensor(device, name),
                    DysonFilterLifeSensorPercentage(device, name),
                ]
            )
        if getattr(device, "carbon_filter_life", None) is not None:
            entities.append(DysonCarbonFilterLifeSensor(device, name))
        if getattr(device, "hepa_filter_life", None) is not None:
            entities.append(DysonHEPAFilterLifeSensor(device, name))
        if getattr(device, "hepa_filter_life", None) is not None and getattr(
            device, "carbon_filter_life", None
        ) is None:
            entities.append(DysonCombinedFilterLifeSensor(device, name))
        if hasattr(device, "time_until_next_clean"):
            entities.append(DysonNextDeepCleanSensor(device, name))
    async_add_entities(entities)


class DysonSensor(SensorEntity, DysonEntity):
    """Base class for a Dyson sensor."""

    _MESSAGE_TYPE = MessageType.STATE
    _SENSOR_TYPE: Optional[str] = None
    _SENSOR_NAME: Optional[str] = None

    def __init__(self, device: DysonDevice, name: str):
        """Initialize the sensor."""
        super().__init__(device, name)

    @property
    def sub_name(self):
        """Return the name of the Dyson sensor."""
        return self._SENSOR_NAME

    @property
    def sub_unique_id(self):
        """Return the sensor's unique id."""
        return self._SENSOR_TYPE


class DysonSensorEnvironmental(DysonSensor):
    """Dyson environmental sensor (push updates)."""

    _MESSAGE_TYPE = MessageType.ENVIRONMENTAL


class DysonBatterySensor(DysonSensor):
    """Dyson battery sensor."""

    _SENSOR_TYPE = "battery_level"
    _SENSOR_NAME = "Battery Level"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int:
        """Return the state of the sensor."""
        return int(getattr(self._device, "battery_level", 0))


class DysonRobotCurrentAreaSensor(DysonSensor):
    """Dyson robot current area name (best-effort)."""

    _SENSOR_TYPE = "current_area"
    _SENSOR_NAME = "Current Area"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Optional[str]:
        value = getattr(self._device, "current_zone_name", None)
        if isinstance(value, str) and value:
            return value
        if bool(getattr(self._device, "is_clean_session_active", False)):
            return "Unknown"
        return "None"


class DysonRobotSelectedAreasSensor(DysonSensor):
    """Dyson robot selected areas list (multi-area selection)."""

    _SENSOR_TYPE = "selected_areas"
    _SENSOR_NAME = "Selected Areas"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Optional[str]:
        selected = getattr(self._device, "selected_zone_descriptions", None)
        if isinstance(selected, list):
            cleaned = [str(v) for v in selected if isinstance(v, str) and v]
        else:
            cleaned = []

        if not cleaned:
            strategy = getattr(self._device, "current_power_mode", None)
            if isinstance(strategy, str) and strategy:
                return f"All ({strategy.capitalize()})"
            return "All"

        return ", ".join(cleaned)

    @property
    def extra_state_attributes(self) -> dict:
        # Expose per-area dust predictions as attributes so the UI can display them.
        dust_by_zone = getattr(self._device, "dust_by_zone_mg", None)
        if not isinstance(dust_by_zone, dict):
            dust_by_zone = {}

        zones = dict(getattr(self._device, "zones", []) or [])
        dust_by_area: dict[str, float] = {}
        for zone_id, mg in dust_by_zone.items():
            if isinstance(zone_id, str) and isinstance(mg, (int, float)):
                dust_by_area[zones.get(zone_id, zone_id)] = float(mg)

        return {
            "selected_zone_ids": list(getattr(self._device, "selected_zone_ids", []) or []),
            "selected_map_id": getattr(self._device, "selected_map_id", None),
            "dust_by_area_mg": dust_by_area,
        }

class DysonRobotAreaDustSensor(DysonSensor):
    """Dust prediction for a specific area (Vis Nav)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: DysonDevice, name: str, zone_id: str, zone_name: str):
        super().__init__(device, name)
        self._zone_id = zone_id
        self._zone_name = zone_name

    @staticmethod
    def _level_for_mg(mg: float) -> str:
        # Dyson UI buckets (empirical): Low/Moderate/High.
        if mg < 3:
            return "Low"
        if mg < 6:
            return "Moderate"
        return "High"

    @property
    def sub_name(self) -> str:
        return f"{self._zone_name} Dust"

    @property
    def sub_unique_id(self) -> str:
        return f"dust-{self._zone_id}"

    @property
    def native_value(self) -> Optional[str]:
        dust_by_zone = getattr(self._device, "dust_by_zone_mg", None)
        if not isinstance(dust_by_zone, dict):
            return None
        mg = dust_by_zone.get(self._zone_id)
        if not isinstance(mg, (int, float)):
            return None
        return self._level_for_mg(float(mg))

    @property
    def extra_state_attributes(self) -> dict:
        dust_by_zone = getattr(self._device, "dust_by_zone_mg", None)
        if not isinstance(dust_by_zone, dict):
            return {}
        mg = dust_by_zone.get(self._zone_id)
        attrs: dict = {
            "zone_id": self._zone_id,
            "selected_map_id": getattr(self._device, "selected_map_id", None),
        }
        if isinstance(mg, (int, float)):
            attrs["mg"] = float(mg)
        return attrs


class DysonRobotLastMessageTimeSensor(DysonSensor):
    """Timestamp of the last CURRENT-STATE message received."""

    _SENSOR_TYPE = "last_message_time"
    _SENSOR_NAME = "Last Update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> Optional[datetime]:
        raw = getattr(self._device, "last_message_time", None)
        if not isinstance(raw, str) or not raw:
            return None
        dt = dt_util.parse_datetime(raw)
        if dt is None:
            return None
        return dt_util.as_utc(dt)


class DysonFilterLifeSensor(DysonSensor):
    """Dyson filter life sensor (in hours) for Pure Cool Link."""

    _SENSOR_TYPE = "filter_life"
    _SENSOR_NAME = "Filter Life"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:filter-outline"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        return getattr(self._device, "filter_life", None)


class DysonFilterLifeSensorPercentage(DysonSensor):
    """Dyson filter life sensor (in percentage) for Pure Cool Link."""

    _SENSOR_TYPE = "filter_life_percentage"
    _SENSOR_NAME = "Filter Life Percentage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:filter-outline"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor calculated to a %."""
        value = getattr(self._device, "filter_life", None)
        if not isinstance(value, (int, float)):
            return None
        return (float(value) / 4300.0) * 100.0


class DysonCarbonFilterLifeSensor(DysonSensor):
    """Dyson carbon filter life sensor (in percentage) for Pure Cool."""

    _SENSOR_TYPE = "carbon_filter_life"
    _SENSOR_NAME = "Carbon Filter Life"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:filter-outline"
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        return getattr(self._device, "carbon_filter_life", None)


class DysonHEPAFilterLifeSensor(DysonSensor):
    """Dyson HEPA filter life sensor (in percentage) for Pure Cool."""

    _SENSOR_TYPE = "hepa_filter_life"
    _SENSOR_NAME = "HEPA Filter Life"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:filter-outline"
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        return getattr(self._device, "hepa_filter_life", None)


class DysonCombinedFilterLifeSensor(DysonSensor):
    """Dyson combined filter life sensor (in percentage) for Pure Cool."""

    _SENSOR_TYPE = "combined_filter_life"
    _SENSOR_NAME = "Filter Life"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:filter-outline"
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        return getattr(self._device, "hepa_filter_life", None)


class DysonNextDeepCleanSensor(DysonSensor):
    """Sensor of time until next deep clean (in hours) for Dyson Pure Humidify+Cool."""

    _SENSOR_TYPE = "next_deep_clean"
    _SENSOR_NAME = "Next Deep Clean"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:filter-outline"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        if (value := self._device.time_until_next_clean) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.time_until_next_clean, (int, float))

class DysonHumiditySensor(DysonSensorEnvironmental):
    """Dyson humidity sensor."""

    _SENSOR_TYPE = "humidity"
    _SENSOR_NAME = "Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        if (value := self._device.humidity) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.humidity, (int, float))


class DysonTemperatureSensor(DysonSensorEnvironmental):
    """Dyson temperature sensor."""

    _SENSOR_TYPE = "temperature"
    _SENSOR_NAME = "Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        """Return the "native" value for this sensor.
        Note that as of 2021-10-28, Home Assistant does not support converting
        from Kelvin native unit to Celsius/Fahrenheit. So we return the Celsius
        value as it's the easiest to calculate.
        """
        if (value := self._device.temperature) >= 0:
            return float(value) - 273.15
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.temperature, (int, float))


class DysonPM25Sensor(DysonSensorEnvironmental):
    """Dyson sensor for PM 2.5 fine particulate matters."""

    _SENSOR_TYPE = "pm25"
    _SENSOR_NAME = "PM 2.5"
    _attr_device_class = SensorDeviceClass.PM25
    _attr_native_unit_of_measurement = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        if (value := self._device.particulate_matter_2_5) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.particulate_matter_2_5, (int, float))


class DysonPM10Sensor(DysonSensorEnvironmental):
    """Dyson sensor for PM 10 particulate matters."""

    _SENSOR_TYPE = "pm10"
    _SENSOR_NAME = "PM 10"
    _attr_device_class = SensorDeviceClass.PM10
    _attr_native_unit_of_measurement = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        if (value := self._device.particulate_matter_10) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.particulate_matter_10, (int, float))


class DysonParticulatesSensor(DysonSensorEnvironmental):
    """Deprecated legacy sensor (not used in cloud-only mode)."""

    _SENSOR_TYPE = "aqi"
    _SENSOR_NAME = "Air Quality Index"


class DysonVOCSensor(DysonSensorEnvironmental):
    """Dyson sensor for volatile organic compounds."""

    _SENSOR_TYPE = "voc-index"
    _SENSOR_NAME = "Volatile Organic Compounds Index"
    _attr_device_class = SensorDeviceClass.AQI
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        if (value := self._device.volatile_organic_compounds) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.volatile_organic_compounds, (int, float))


class DysonNO2Sensor(DysonSensorEnvironmental):
    """Dyson sensor for Nitrogen Dioxide."""

    _SENSOR_TYPE = "no2-index"
    _SENSOR_NAME = "Nitrogen Dioxide Index"
    _attr_device_class = SensorDeviceClass.AQI
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[int]:
        """Return the state of the sensor."""
        if (value := self._device.nitrogen_dioxide) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.nitrogen_dioxide, (int, float))


class DysonHCHOSensor(DysonSensorEnvironmental):
    """Dyson sensor for Formaldehyde."""

    _SENSOR_TYPE = "hcho-mg"
    _SENSOR_NAME = "HCHO"

    _attr_native_unit_of_measurement = CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        if (value := self._device.formaldehyde) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.formaldehyde, (int, float))


class DysonCarbonDioxideSensor(DysonSensorEnvironmental):
    """Dyson sensor for Carbon Dioxide."""

    _SENSOR_TYPE = "c02"
    _SENSOR_NAME = "Carbon Dioxide"

    _attr_device_class = SensorDeviceClass.CO2
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        if (value := self._device.carbon_dioxide) >= 0:
            return value
        return None

    @property
    def available(self) -> bool:
        """Return available only if device not in off, init or failed states."""
        return isinstance(self._device.carbon_dioxide, (int, float))
