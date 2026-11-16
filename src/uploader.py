"""Nightscout uploader for CGM data and Treatments from Medtrum EasyView."""

from __future__ import annotations

import base64
import functools
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

# Proxy nur fuer die Medtrum-EasyView-Anfragen (HTTPS_PROXY/HTTP_PROXY wie im Installer).
# Nightscout wird immer direkt angesprochen.
PROXY_ENV_VARS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "MEDTRUM_PROXY", "GLOBAL_AGENT_HTTPS_PROXY",
)


def get_proxy_url() -> str | None:
    """Return the proxy URL for EasyView requests from the environment, if any."""
    for name in PROXY_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def mask_proxy(url: str) -> str:
    """Hide credentials in a proxy URL for logging."""
    return re.sub(r"//[^@/]*@", "//***@", url)


@dataclass
class SensorStatus:
    """Dataclass representing a Medtrum sensor status."""

    device_type: str
    glucose: float
    glucose_rate: int
    sensor_id: int
    sequence: int
    serial: int
    status: Status | None
    update_time: float
    app_name: str | None = None
    battery_percent: float | None = None
    current: int | None = None

    class Status(Enum):
        """Medtrum Status"""

        WARMING_UP = 2
        NORMAL = 3
        NEEDS_CALIBRATION = 10

    @property
    def unix_timestamp(self) -> int:
        """Return the timestamp as an integer."""
        return round(self.update_time)

    @property
    def timestamp(self) -> datetime:
        """Return the timestamp as a datetime object."""
        return datetime.fromtimestamp(self.unix_timestamp, tz=timezone.utc)

    @property
    def key(self) -> tuple[int, int]:
        """Return unique key for the entry."""
        return (self.sensor_id, self.sequence)

    @property
    def preceding_key(self) -> tuple[int, int]:
        """Return preceding key for the entry."""
        return (self.sensor_id, self.sequence - 1)

    @property
    def direction(self) -> str | None:
        """Return direction of glucose change."""
        directions = {
            0: "Flat",
            1: "FortyFiveUp",
            2: "SingleUp",
            3: "DoubleUp",
            4: "FortyFiveDown",
            5: "SingleDown",
            6: "DoubleDown",
            8: "Flat",  # both 0 and 8 seem to show as Flat in the app
        }
        if self.glucose_rate not in directions:
            logger.warning(
                "unknown glucose rate %i on entry %i",
                self.glucose_rate,
                self.sequence,
            )
        return directions.get(self.glucose_rate)

    @property
    def nightscout_entry(self) -> dict[str, str | int]:
        """Return sensor status as Nightscout entry."""
        return {
            "type": "sgv",
            "date": self.unix_timestamp * 1000,
            "dateString": self.timestamp.isoformat(),
            "sgv": round(self.glucose * 18),
            "direction": self.direction or "NONE",
            "device": self.device_type,
        }

    @classmethod
    def from_easyview(cls, data: dict[str, Any]) -> SensorStatus:
        """Create a SensorStatus from EasyView sensor_status dictionary."""
        try:
            status = cls.Status(data["status"])
        except ValueError:
            logger.warning(
                "Unknown status '%s' (sensor=%i, sequence=%i)",
                data["status"],
                data["sensorId"],
                data["sequence"],
            )
            status = None
        return cls(
            app_name=data.get("appName"),
            battery_percent=data.get("batteryPercent"),
            current=data.get("current"),
            device_type=data["deviceType"],
            glucose=data["glucose"],
            glucose_rate=data["glucoseRate"],
            sensor_id=data["sensorId"],
            sequence=data["sequence"],
            serial=data["serial"],
            status=status,
            update_time=data["updateTime"],
        )

    @classmethod
    def from_download(
        cls,
        record: tuple[str, float, float, float, str, float],
        device_type,
    ) -> SensorStatus:
        """Create a SensorStatus from EasyView download record."""
        pattern = r"^(?P<uid>\d+)-(?P<serial>\d+)-(?P<sensorId>\d+)-(?P<sequence>\d+)$"
        match = re.match(pattern, record[0])
        if not match:
            raise ValueError("invalid EasyView download record")
        status = {
            "C": cls.Status.NORMAL,
            "H": cls.Status.WARMING_UP,
            "XC": cls.Status.NEEDS_CALIBRATION,
        }
        if record[4] not in status:
            logger.warning(
                "Unknown status '%s' in EasyView download record (sensor=%i, sequence=%i)",
                record[4],
                int(match.group("sensorId")),
                int(match.group("sequence")),
            )
        return cls(
            device_type=device_type,
            glucose=record[3],
            glucose_rate=round(record[5]),
            sensor_id=int(match.group("sensorId")),
            sequence=int(match.group("sequence")),
            serial=int(match.group("serial")),
            status=status.get(record[4]),
            update_time=record[1],
        )

    @classmethod
    def from_timestamp(cls, timestamp: datetime, device_type: str) -> SensorStatus:
        """Create a SensorStatus with only timestamp set."""
        return cls(
            device_type=device_type,
            glucose=0,
            glucose_rate=0,
            sensor_id=0,
            sequence=0,
            serial=0,
            status=None,
            update_time=datetime.timestamp(timestamp),
        )


@dataclass
class PumpStatus:
    """Dataclass representing Medtrum pump status from EasyView."""

    update_time: float
    pump_id: int
    serial: int
    device_type: int

    # Bolus data
    bolus_deliveried: float
    bolus_deliveried_time: float
    bolus_type: int
    last_bolus_type: int

    # Basal data
    basal_deliveried: float
    basal_deliveried_time: float
    basal_rate: float
    basal_type: int
    basal_percent: float

    # Extended bolus
    extend_bolus_deliveried: float
    extend_bolus_duration: int
    extend_bolus_set: float

    # Status
    iob: float
    remaining_dose: float
    remaining_time: int
    running_mins: int
    state: int
    status: int

    # Auto mode
    auto_mode_enabled: bool
    auto_mode_sg_target: int
    bg_target: int

    # Exercise
    exercise_status: int
    exercise_duration: int
    exercise_remain_time: int

    app_name: str | None = None

    @property
    def unix_timestamp(self) -> int:
        """Return the timestamp as an integer."""
        return round(self.update_time)

    @property
    def timestamp(self) -> datetime:
        """Return the timestamp as a datetime object."""
        return datetime.fromtimestamp(self.unix_timestamp, tz=timezone.utc)

    @property
    def bolus_timestamp(self) -> datetime:
        """Return the bolus timestamp as a datetime object."""
        return datetime.fromtimestamp(round(self.bolus_deliveried_time), tz=timezone.utc)

    @property
    def basal_timestamp(self) -> datetime:
        """Return the basal timestamp as a datetime object."""
        return datetime.fromtimestamp(round(self.basal_deliveried_time), tz=timezone.utc)

    @classmethod
    def from_easyview(cls, data: dict[str, Any]) -> PumpStatus:
        """Create a PumpStatus from EasyView pump_status dictionary."""
        return cls(
            update_time=data.get("updateTime", 0),
            pump_id=data.get("pumpId", 0),
            serial=data.get("serial", 0),
            device_type=data.get("deviceType", 0),
            bolus_deliveried=data.get("bolusDeliveried", 0),
            bolus_deliveried_time=data.get("bolusDeliveriedTime", 0),
            bolus_type=data.get("bolusType", 0),
            last_bolus_type=data.get("lastBolusType", 0),
            basal_deliveried=data.get("basalDeliveried", 0),
            basal_deliveried_time=data.get("basalDeliveriedTime", 0),
            basal_rate=data.get("basalRate", 0),
            basal_type=data.get("basalType", 0),
            basal_percent=data.get("basalPercent", 0),
            extend_bolus_deliveried=data.get("extendBolusDeliveried", 0),
            extend_bolus_duration=data.get("extendBolusDuration", 0),
            extend_bolus_set=data.get("extendBolusSet", 0),
            iob=data.get("iob", 0),
            remaining_dose=data.get("remainingDose", 0),
            remaining_time=data.get("remainingTime", 0),
            running_mins=data.get("runningMins", 0),
            state=data.get("state", 0),
            status=data.get("status", 0),
            auto_mode_enabled=bool(data.get("AutoModeEnabledNew", 0)),
            auto_mode_sg_target=data.get("AutoModeSGTarget", 0),
            bg_target=data.get("bGTarget", 0),
            exercise_status=data.get("exerciseStatus", 0),
            exercise_duration=data.get("exerciseDuration", 0),
            exercise_remain_time=data.get("exerciseRemainTime", 0),
            app_name=data.get("appName"),
        )


@dataclass
class Treatment:
    """Dataclass representing a treatment entry (insulin, carbs, etc.)."""

    timestamp: float
    treatment_type: TreatmentType
    value: float
    notes: str = ""
    record_id: str = ""

    class TreatmentType(Enum):
        """Treatment types from EasyView."""
        BOLUS = "bolus"
        CARBS = "carbs"
        BASAL = "basal"
        TEMP_BASAL = "temp_basal"
        CORRECTION = "correction"
        EXERCISE = "exercise"
        NOTE = "note"
        IOB = "iob"
        UNKNOWN = "unknown"

    @property
    def unix_timestamp(self) -> int:
        """Return the timestamp as an integer."""
        return round(self.timestamp)

    @property
    def datetime(self) -> datetime:
        """Return the timestamp as a datetime object."""
        return datetime.fromtimestamp(self.unix_timestamp, tz=timezone.utc)

    @property
    def key(self) -> str:
        """Return unique key for the treatment."""
        return f"{self.unix_timestamp}-{self.treatment_type.value}-{self.value}"

    @property
    def nightscout_treatment(self) -> dict[str, Any]:
        """Return treatment as Nightscout treatment entry."""
        base = {
            "created_at": self.datetime.isoformat(),
            "date": self.unix_timestamp * 1000,
            "enteredBy": "EasyView/Medtrum",
        }

        if self.treatment_type == self.TreatmentType.BOLUS:
            base.update({
                "eventType": "Bolus",
                "insulin": self.value,
            })
        elif self.treatment_type == self.TreatmentType.CARBS:
            base.update({
                "eventType": "Carb Correction",
                "carbs": self.value,
            })
        elif self.treatment_type == self.TreatmentType.CORRECTION:
            base.update({
                "eventType": "Correction Bolus",
                "insulin": self.value,
            })
        elif self.treatment_type == self.TreatmentType.BASAL:
            base.update({
                "eventType": "Temp Basal",
                "absolute": self.value,
            })
        elif self.treatment_type == self.TreatmentType.TEMP_BASAL:
            base.update({
                "eventType": "Temp Basal",
                "percent": self.value,
            })
        elif self.treatment_type == self.TreatmentType.EXERCISE:
            base.update({
                "eventType": "Exercise",
                "duration": self.value,
                "notes": self.notes or "Exercise mode active",
            })
        elif self.treatment_type == self.TreatmentType.NOTE:
            base.update({
                "eventType": "Note",
                "notes": self.notes or str(self.value),
            })
        else:
            base.update({
                "eventType": "Note",
                "notes": f"Unknown treatment: {self.value}",
            })

        if self.notes and self.treatment_type != self.TreatmentType.NOTE:
            base["notes"] = self.notes

        return base

    @classmethod
    def from_pump_status_bolus(cls, pump_status: PumpStatus) -> Treatment | None:
        """Create a bolus Treatment from PumpStatus."""
        if pump_status.bolus_deliveried <= 0:
            return None
        return cls(
            timestamp=pump_status.bolus_deliveried_time,
            treatment_type=cls.TreatmentType.BOLUS,
            value=pump_status.bolus_deliveried,
            notes=f"Bolus type: {pump_status.last_bolus_type}",
            record_id=f"pump-{pump_status.pump_id}-bolus-{round(pump_status.bolus_deliveried_time)}",
        )

    @classmethod
    def from_pump_status_basal(cls, pump_status: PumpStatus) -> Treatment | None:
        """Create a basal Treatment from PumpStatus (if temp basal active)."""
        # Only create if there's an active temp basal (basal_type indicates temp)
        if pump_status.basal_deliveried <= 0 or pump_status.basal_type == 0:
            return None
        return cls(
            timestamp=pump_status.basal_deliveried_time,
            treatment_type=cls.TreatmentType.TEMP_BASAL,
            value=pump_status.basal_percent if pump_status.basal_percent else pump_status.basal_rate,
            notes=f"Basal type: {pump_status.basal_type}",
            record_id=f"pump-{pump_status.pump_id}-basal-{round(pump_status.basal_deliveried_time)}",
        )

    @classmethod
    def from_pump_status_exercise(cls, pump_status: PumpStatus) -> Treatment | None:
        """Create an exercise Treatment from PumpStatus if exercise mode is active."""
        if pump_status.exercise_status == 0:
            return None
        return cls(
            timestamp=pump_status.update_time,
            treatment_type=cls.TreatmentType.EXERCISE,
            value=pump_status.exercise_duration,
            notes=f"Remaining: {pump_status.exercise_remain_time} min",
            record_id=f"pump-{pump_status.pump_id}-exercise-{round(pump_status.update_time)}",
        )

    @classmethod
    def from_easyview_bolus(cls, data: dict[str, Any]) -> Treatment:
        """Create a Treatment from EasyView bolus record."""
        return cls(
            timestamp=data.get("time", data.get("timestamp", 0)),
            treatment_type=cls.TreatmentType.BOLUS,
            value=data.get("bolus", data.get("insulin", data.get("value", 0))),
            notes=data.get("notes", data.get("remark", "")),
            record_id=str(data.get("id", data.get("recordId", ""))),
        )

    @classmethod
    def from_easyview_carbs(cls, data: dict[str, Any]) -> Treatment:
        """Create a Treatment from EasyView carbs record."""
        return cls(
            timestamp=data.get("time", data.get("timestamp", 0)),
            treatment_type=cls.TreatmentType.CARBS,
            value=data.get("carbs", data.get("carb", data.get("value", 0))),
            notes=data.get("notes", data.get("remark", "")),
            record_id=str(data.get("id", data.get("recordId", ""))),
        )

    @classmethod
    def from_download_record(cls, record: tuple, record_type: str) -> Treatment:
        """Create a Treatment from EasyView download tuple record."""
        if len(record) < 3:
            raise ValueError("Invalid download record format")

        type_mapping = {
            "bo": cls.TreatmentType.BOLUS,
            "bolus": cls.TreatmentType.BOLUS,
            "ca": cls.TreatmentType.CARBS,
            "carbs": cls.TreatmentType.CARBS,
            "food": cls.TreatmentType.CARBS,
        }

        treatment_type = type_mapping.get(record_type.lower(), cls.TreatmentType.UNKNOWN)

        return cls(
            timestamp=float(record[1]),
            treatment_type=treatment_type,
            value=float(record[2]),
            notes=str(record[3]) if len(record) > 3 else "",
            record_id=str(record[0]),
        )


def with_retry(delay: int):
    """Decorator to retry on session Timeout or ConnectionError."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            while True:
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.ReadTimeout:
                    logger.info("Network timeout, retrying")
                except requests.exceptions.ConnectionError:
                    logger.info("Network connection error, retrying")
                time.sleep(delay)

        return wrapper

    return decorator


class EasyViewBase(ABC):
    """Abstract base class for EasyView API interaction."""

    BASE_URL = "https://easyview.medtrum.eu/v3/api/v2.0"

    _sensor_status: SensorStatus | None = None
    _pump_status: PumpStatus | None = None
    _last_raw_status: dict[str, Any] | None = None

    def __init__(self, username: str, password: str, timestamp: datetime | None = None) -> None:
        """Initialize with username, password and optional resume timestamp."""
        self.username = username
        self.password = password
        self.session: requests.Session = requests.Session()
        self.session.headers.update(
            {
                "DevInfo": "Android 14;Xiamoi vayu;Android 14",
                "AppTag": "v=1.2.70(112);n=eyfo;p=android",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
            }
        )
        proxy = get_proxy_url()
        if proxy:
            # trust_env aus: sonst ueberschreiben HTTP(S)_PROXY aus der Umgebung session.proxies
            self.session.trust_env = False
            self.session.proxies.update({"http": proxy, "https": proxy})
            logger.info("PROXY: EasyView requests via %s", mask_proxy(proxy))
        self.resume_timestamp = timestamp
        self._queue: list[SensorStatus] = []
        self._next_interval = datetime.now(timezone.utc)
        self._uploaded_treatment_keys: set[str] = set()

    def __enter__(self):
        """Context manager entry, opens connection to EasyView."""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit, closes connection to EasyView."""
        self.close()

    def __iter__(self) -> Iterator[SensorStatus]:
        return self

    def __next__(self) -> SensorStatus:
        """Returns next SensorStatus from EasyView"""
        while not self._queue:
            cur_stat = self.sensor_status
            delta = (self._next_interval - datetime.now(timezone.utc)).total_seconds()
            if delta > 0:
                time.sleep(delta)
            self._next_interval = datetime.now(timezone.utc) + timedelta(seconds=30)

            new_stat = self._fetch_current_status()
            if new_stat is None:
                continue

            if new_stat.key == cur_stat.key:
                logger.info(
                    "no new data on EasyView (sensor=%i, sequence=%i)",
                    cur_stat.sensor_id,
                    cur_stat.sequence,
                )
                continue
            if new_stat.preceding_key != cur_stat.key:
                for s in self.history(cur_stat.timestamp, new_stat.timestamp):
                    if new_stat.key > s.key > cur_stat.key:
                        self._queue.append(s)
            self._queue.append(new_stat)
            self._next_interval = max(
                new_stat.timestamp + timedelta(seconds=150), self._next_interval
            )

        self.sensor_status = self._queue.pop(0)
        return self.sensor_status

    @with_retry(delay=10)
    def _post(self, endpoint: str, data: dict) -> dict[str, Any]:
        """Send a POST request to the specified endpoint with the given data."""
        response = self.session.post(
            f"{self.BASE_URL}/{endpoint}", data=data, timeout=10
        )
        response.raise_for_status()
        answer = response.json()
        logger.info("Response POST: %s", answer)
        return answer

    @with_retry(delay=10)
    def _get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        """Send a GET request to the specified endpoint with the given parameters."""
        response = self.session.get(
            f"{self.BASE_URL}/{endpoint}", params=params, timeout=10
        )
        response.raise_for_status()
        answer = response.json()
        logger.info("Response GET: %s", answer)
        return answer

    @property
    def sensor_status(self) -> SensorStatus:
        """Return the last retrieved sensor status."""
        if self._sensor_status is None:
            current_status = self._get_initial_status()
            if current_status is None:
                raise ValueError("No active sensor found")

            if self.resume_timestamp is None:
                self._sensor_status = SensorStatus.from_timestamp(
                    current_status.timestamp - timedelta(hours=48),
                    current_status.device_type,
                )
            elif self.resume_timestamp != current_status.timestamp:
                self._sensor_status = SensorStatus.from_timestamp(
                    self.resume_timestamp, current_status.device_type
                )
            else:
                self._sensor_status = current_status
        return self._sensor_status

    @sensor_status.setter
    def sensor_status(self, value: SensorStatus):
        """Update sensor status"""
        if value.key == self.sensor_status.key:
            logger.info(
                "status is current (sensor=%i, sequence=%i)",
                value.sensor_id,
                value.sequence,
            )
            raise ValueError("sensor status is already current")
        if self.sensor_status.key > value.key:
            logger.error(
                "sensor status is outdated (sensor=%i, sequence=%i)",
                value.sensor_id,
                value.sequence,
            )
            raise ValueError("invalid sensor status")
        self._sensor_status = value
        logger.info(
            "sensor status updated (sensor=%i, sequence=%i)",
            self.sensor_status.sensor_id,
            self.sensor_status.sequence,
        )

    @property
    def pump_status(self) -> PumpStatus | None:
        """Return the last retrieved pump status."""
        return self._pump_status

    @abstractmethod
    def open(self) -> None:
        """Establish a connection to EasyView."""
        pass

    def close(self) -> None:
        """Closes the connection to EasyView."""
        logger.info("closed connection to EasyView")
        self.session.close()

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """Get CGM data from the EasyView API."""
        pass

    @abstractmethod
    def _get_initial_status(self) -> SensorStatus | None:
        """Get the initial sensor status for initialization."""
        pass

    @abstractmethod
    def _fetch_current_status(self) -> SensorStatus | None:
        """Fetch current sensor status from API."""
        pass

    @abstractmethod
    def get_downloads(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Get historical sensor status data from EasyView API."""
        pass

    @abstractmethod
    def _get_target_username(self) -> str:
        """Get the username to use for API calls (own or monitored user)."""
        pass

    def history(self, start, end) -> Iterator[SensorStatus]:
        """Returns iterator of SensorStatus objects for requested period."""
        downloads = self.get_downloads(start, end)["data"]
        device_type = self.sensor_status.device_type
        for rec in map(tuple, downloads):
            try:
                yield SensorStatus.from_download(rec, device_type=device_type)
            except ValueError:
                pass

    def get_current_treatments(self) -> list[Treatment]:
        """Get current treatments from the last pump_status."""
        treatments = []

        if self._pump_status is None:
            logger.info("No pump_status available for treatments")
            return treatments

        # Log raw pump_status treatment-relevant fields
        logger.info(
            "pump_status treatment data: "
            "bolusDeliveried=%.2f, bolusDeliveriedTime=%s, bolusType=%d, lastBolusType=%d, "
            "basalDeliveried=%.2f, basalDeliveriedTime=%s, basalType=%d, "
            "exerciseStatus=%d, exerciseDuration=%d",
            self._pump_status.bolus_deliveried,
            datetime.fromtimestamp(self._pump_status.bolus_deliveried_time, tz=timezone.utc).isoformat(),
            self._pump_status.bolus_type,
            self._pump_status.last_bolus_type,
            self._pump_status.basal_deliveried,
            datetime.fromtimestamp(self._pump_status.basal_deliveried_time, tz=timezone.utc).isoformat(),
            self._pump_status.basal_type,
            self._pump_status.exercise_status,
            self._pump_status.exercise_duration,
        )

        # Extract bolus if present and not already uploaded
        bolus = Treatment.from_pump_status_bolus(self._pump_status)
        if bolus:
            already_uploaded = bolus.key in self._uploaded_treatment_keys
            logger.info(
                "Extracted bolus treatment: value=%.2f, time=%s, key=%s, already_uploaded=%s",
                bolus.value,
                bolus.datetime.isoformat(),
                bolus.key,
                already_uploaded,
            )
            if not already_uploaded:
                treatments.append(bolus)
                self._uploaded_treatment_keys.add(bolus.key)
        else:
            logger.info("No bolus to extract (bolusDeliveried <= 0)")

        # Extract exercise mode if active (only once per exercise session)
        exercise = Treatment.from_pump_status_exercise(self._pump_status)
        if exercise:
            already_uploaded = exercise.key in self._uploaded_treatment_keys
            logger.info(
                "Extracted exercise treatment: duration=%.0f, remaining=%s, already_uploaded=%s",
                exercise.value,
                exercise.notes,
                already_uploaded,
            )
            if not already_uploaded:
                treatments.append(exercise)
                self._uploaded_treatment_keys.add(exercise.key)

        logger.info("Total new treatments to upload: %d", len(treatments))
        return treatments

    def get_treatments_from_download(self, start: datetime, end: datetime) -> list[Treatment]:
        """Get historical treatment data via download endpoint."""
        treatments = []
        target_user = self._get_target_username()

        # Try fetching bolus data with flag "bo"
        try:
            bolus_params = {
                "flag": "bo",
                "st": (start + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "et": (end - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "user_name": target_user,
            }
            bolus_data = self._get("download", bolus_params)
            logger.info("Bolus download response: %s", bolus_data)
            if "data" in bolus_data and bolus_data["data"]:
                for rec in bolus_data["data"]:
                    try:
                        if isinstance(rec, dict):
                            treatments.append(Treatment.from_easyview_bolus(rec))
                        else:
                            treatments.append(Treatment.from_download_record(tuple(rec), "bolus"))
                    except (ValueError, IndexError) as e:
                        logger.warning("Failed to parse bolus record: %s", e)
        except Exception as e:
            logger.info("Bolus download not available or failed: %s", e)

        # Try fetching carbs data with flag "ca"
        try:
            carbs_params = {
                "flag": "ca",
                "st": (start + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "et": (end - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
                "user_name": target_user,
            }
            carbs_data = self._get("download", carbs_params)
            logger.info("Carbs download response: %s", carbs_data)
            if "data" in carbs_data and carbs_data["data"]:
                for rec in carbs_data["data"]:
                    try:
                        if isinstance(rec, dict):
                            treatments.append(Treatment.from_easyview_carbs(rec))
                        else:
                            treatments.append(Treatment.from_download_record(tuple(rec), "carbs"))
                    except (ValueError, IndexError) as e:
                        logger.warning("Failed to parse carbs record: %s", e)
        except Exception as e:
            logger.info("Carbs download not available or failed: %s", e)

        treatments.sort(key=lambda t: t.timestamp)
        return treatments


class EasyFollow(EasyViewBase):
    """Class that interacts with the EasyView API from a Follow account."""

    _cached_uid: int | None = None

    @functools.cached_property
    def cgm_username(self) -> str:
        """Get the username of the user carrying the CGM."""
        status = self.get_status()
        return status["data"]["items"][0]["username"]

    def _get_target_username(self) -> str:
        """Get the username of the monitored CGM user."""
        return self.cgm_username

    def open(self) -> None:
        """Establish a connection to EasyView."""
        data = {
            "user_name": self.username,
            "password": self.password,
            "user_type": "P",
        }
        result = self._post("login", data=data)
        self._cached_uid = result.get("uid")
        logger.info("logged in to EasyView as %s (uid=%s)", self.username, self._cached_uid)

    def get_status(self) -> dict[str, Any]:
        """Get CGM data from the EasyView API (v2.1 monitor/connections)."""
        # Use v2.1 endpoint for monitor connections
        connections = self._get("../v2.1/monitor/connections?per_page=9999")
        logger.info("connections %s", connections)
        return connections

    def _get_initial_status(self) -> SensorStatus | None:
        """Get the initial sensor status for initialization."""
        status = self.get_status()
        self._last_raw_status = status

        items = status.get("data", {}).get("items", [])
        if len(items) != 1:
            logger.error(
                "Follower should have exactly one CGM user, got %i",
                len(items),
            )
            raise ValueError("Account should follow exactly one CGM user.")

        monitor = items[0]

        # Extract pump status if available
        if "pump_status" in monitor:
            self._pump_status = PumpStatus.from_easyview(monitor["pump_status"])
            logger.info(
                "pump_status extracted: IOB=%.2f, lastBolus=%.2f at %s, "
                "reservoir=%.2f, autoMode=%s, state=%d",
                self._pump_status.iob,
                self._pump_status.bolus_deliveried,
                self._pump_status.bolus_timestamp.isoformat(),
                self._pump_status.remaining_dose,
                self._pump_status.auto_mode_enabled,
                self._pump_status.state,
            )

        if "sensor_status" not in monitor:
            return None
        return SensorStatus.from_easyview(monitor["sensor_status"])

    def _fetch_current_status(self) -> SensorStatus | None:
        """Fetch current sensor status from API."""
        raw_status = self.get_status()
        self._last_raw_status = raw_status

        if raw_status.get("error") != 0:
            logger.error("Easyview API returned error: %s", raw_status)
            return None

        items = raw_status.get("data", {}).get("items", [])
        if not items:
            logger.warning("no monitored users found in EasyView account")
            return None

        monitor = items[0]

        # Update pump status
        if "pump_status" in monitor:
            self._pump_status = PumpStatus.from_easyview(monitor["pump_status"])

        if "sensor_status" not in monitor:
            logger.warning("no active sensor found in EasyView account")
            return None
        return SensorStatus.from_easyview(monitor["sensor_status"])

    def get_downloads(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Get historical sensor status data from EasyView API.
        
        Note: The v3 API may not support the download endpoint.
        Returns empty data if endpoint is not available.
        """
        params = {
            "flag": "sg",
            "st": (start + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "et": (end - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "user_name": self.cgm_username,
        }
        try:
            download = self._get("download", params)
            logger.info("Download %s", download)
            return download
        except Exception as e:
            # v3 API doesn't have download endpoint - return empty data
            logger.warning("Download endpoint not available (v3 API): %s", e)
            return {"data": []}


class EasyCGM(EasyViewBase):
    """Class that interacts with the EasyView API from a CGM user account."""

    def _get_target_username(self) -> str:
        """Get own username for API calls."""
        return self.username

    def open(self) -> None:
        """Establish a connection to EasyView as CGM user."""
        data = {
            "user_name": self.username,
            "password": self.password,
            "user_type": "P",
        }
        result = self._post("login", data=data)
        if result.get("error", 0) != 0 or not result.get("uid"):
            raise ConnectionError(f"Login failed: {result}")
        self._cgm_uid = result["uid"]
        logger.info("logged in to EasyView as CGM user %s (uid=%s)", self.username, self._cgm_uid)

    def get_status(self) -> dict[str, Any]:
        """Get own CGM/pump status from the v3 API (v2.1/monitor/<uid>/status, like the web portal)."""
        day = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        param = base64.b64encode(json.dumps({"ts": [day, day + 86400], "tz": 0}).encode()).decode()
        answer = self._get(f"../v2.1/monitor/{self._cgm_uid}/status", {"param": param})
        if answer.get("error") != 0:
            return {"res": "ERR", "msg": answer}
        data = answer.get("data") or {}
        return {"res": "OK", **data}

    def _get_initial_status(self) -> SensorStatus | None:
        """Get the initial sensor status for initialization."""
        status = self.get_status()
        self._last_raw_status = status

        if status.get("res") == "ERR":
            logger.error("EasyView API error: %s", status.get("msg"))
            return None

        # Extract pump status if available
        if "pump_status" in status:
            self._pump_status = PumpStatus.from_easyview(status["pump_status"])

        # CGM user account returns sensor_status directly (not in monitorlist)
        if "sensor_status" in status:
            return SensorStatus.from_easyview(status["sensor_status"])

        # Fallback: check if data is in a different structure
        if "cgmdata" in status and "sensor_status" in status["cgmdata"]:
            return SensorStatus.from_easyview(status["cgmdata"]["sensor_status"])

        logger.warning("No active sensor found in CGM user account")
        return None

    def _fetch_current_status(self) -> SensorStatus | None:
        """Fetch current sensor status from API."""
        raw_status = self.get_status()
        self._last_raw_status = raw_status

        if raw_status.get("res") == "ERR":
            logger.error("Easyview API returned: %s.", raw_status.get("msg"))
            return None

        # Update pump status
        if "pump_status" in raw_status:
            self._pump_status = PumpStatus.from_easyview(raw_status["pump_status"])

        # CGM user account returns sensor_status directly
        if "sensor_status" in raw_status:
            return SensorStatus.from_easyview(raw_status["sensor_status"])

        # Fallback: check alternative structure
        if "cgmdata" in raw_status and "sensor_status" in raw_status["cgmdata"]:
            return SensorStatus.from_easyview(raw_status["cgmdata"]["sensor_status"])

        logger.warning("no active sensor found in EasyView CGM account")
        return None

    def get_downloads(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Get historical sensor status data from EasyView API.
        
        Note: The v3 API may not support the download endpoint.
        Returns empty data if endpoint is not available.
        """
        params = {
            "flag": "sg",
            "st": (start + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "et": (end - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "user_name": self.username,
        }
        try:
            download = self._get("download", params)
            logger.info("Download %s", download)
            return download
        except Exception as e:
            # v3 API doesn't have download endpoint - return empty data
            logger.warning("Download endpoint not available (v3 API): %s", e)
            return {"data": []}


class NightScout:
    """Class that interacts with Nightscout to sync CGM data."""

    def __init__(self, url, api_secret):
        self.session = requests.Session()
        # Nightscout immer direkt, auch wenn HTTP(S)_PROXY in der Umgebung steht
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-secret": api_secret,
            }
        )
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()

    @property
    def timestamp(self) -> datetime | None:
        """Get last sensor value timestamp from Nightscout."""
        response = self.session.get(
            f"{self.url}/api/v1/entries.json", params={"count": 1}, timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data:
            return datetime.fromtimestamp(data[0]["date"] / 1000, tz=timezone.utc)
        return None

    @property
    def last_treatment_timestamp(self) -> datetime | None:
        """Get last treatment timestamp from Nightscout."""
        try:
            response = self.session.get(
                f"{self.url}/api/v1/treatments.json", params={"count": 1}, timeout=10
            )
            response.raise_for_status()
            data = response.json()
            if data:
                if "date" in data[0]:
                    return datetime.fromtimestamp(data[0]["date"] / 1000, tz=timezone.utc)
                elif "created_at" in data[0]:
                    return datetime.fromisoformat(data[0]["created_at"].replace("Z", "+00:00"))
        except Exception as e:
            logger.warning("Failed to get last treatment timestamp: %s", e)
        return None

    @with_retry(delay=10)
    def add(self, sensor_status: SensorStatus) -> None:
        """Add a sensor value to Nightscout."""
        if sensor_status.status is SensorStatus.Status.WARMING_UP:
            logger.info(
                "sensor is warming up (sensor=%i, sequence=%i)",
                sensor_status.sensor_id,
                sensor_status.sequence,
            )
        else:
            response = self.session.post(
                f"{self.url}/api/v1/entries.json",
                json=[sensor_status.nightscout_entry],
                timeout=10,
            )
            response.raise_for_status()
            logger.info(
                "submitted sensor status to nightscout (sensor=%i, sequence=%i)",
                sensor_status.sensor_id,
                sensor_status.sequence,
            )

    @with_retry(delay=10)
    def add_treatment(self, treatment: Treatment) -> bool:
        """Add a treatment to Nightscout. Returns True if added."""
        ns_treatment = treatment.nightscout_treatment
        logger.info(
            "Uploading treatment to Nightscout: %s",
            ns_treatment,
        )

        response = self.session.post(
            f"{self.url}/api/v1/treatments.json",
            json=[ns_treatment],
            timeout=10,
        )
        response.raise_for_status()
        logger.info(
            "submitted treatment to nightscout: %s %.2f at %s",
            treatment.treatment_type.value,
            treatment.value,
            treatment.datetime.isoformat(),
        )
        return True

    def add_treatments(self, treatments: list[Treatment]) -> int:
        """Add multiple treatments to Nightscout. Returns count of added treatments."""
        added = 0
        for treatment in treatments:
            try:
                if self.add_treatment(treatment):
                    added += 1
            except Exception as e:
                logger.warning("Failed to add treatment: %s", e)
        return added

    def update_pump_status(self, pump_status: PumpStatus) -> None:
        """Update Nightscout with current pump status (IOB, etc.)."""
        # Create a devicestatus entry for the pump
        device_status = {
            "device": f"Medtrum/{pump_status.app_name or 'EasyPatch'}",
            "created_at": pump_status.timestamp.isoformat(),
            "pump": {
                "iob": {
                    "iob": pump_status.iob,
                    "timestamp": pump_status.timestamp.isoformat(),
                },
                "reservoir": pump_status.remaining_dose,
                "status": {
                    "status": "normal" if pump_status.state == 32 else "suspended",
                    "timestamp": pump_status.timestamp.isoformat(),
                },
            },
        }

        # Add auto-mode info if enabled
        if pump_status.auto_mode_enabled:
            device_status["pump"]["auto_mode"] = {
                "enabled": True,
                "target": pump_status.auto_mode_sg_target,
            }

        try:
            response = self.session.post(
                f"{self.url}/api/v1/devicestatus.json",
                json=[device_status],
                timeout=10,
            )
            response.raise_for_status()
            logger.info("updated pump status in nightscout (IOB=%.2f)", pump_status.iob)
        except Exception as e:
            logger.warning("Failed to update pump status: %s", e)


def detect_account_type(username: str, password: str) -> str:
    """Follow-Konto (hat Verbindungen) oder eigenes Konto (keine Verbindungen) erkennen."""
    probe = EasyFollow(username, password)
    try:
        probe.open()
        status = probe.get_status()
        items = ((status or {}).get("data") or {}).get("items") or []
    finally:
        probe.session.close()
    detected = "follow" if items else "cgm"
    logger.info("account_type erkannt: %s (%d Verbindung(en))", detected, len(items))
    return detected


def create_easyview_client(
    username: str,
    password: str,
    account_type: str = "follow",
    timestamp: datetime | None = None
) -> EasyViewBase:
    """
    Factory function to create the appropriate EasyView client.

    Args:
        username: EasyView username
        password: EasyView password
        account_type: Either "follow" for follower accounts or "cgm" for CGM user accounts
        timestamp: Optional resume timestamp

    Returns:
        EasyFollow or EasyCGM instance
    """
    if account_type.lower() in ("auto", "follow"):
        detected = detect_account_type(username, password)
        if account_type.lower() == "follow" and detected != "follow":
            logger.warning("account_type=follow gesetzt, aber keine Verbindungen - nutze cgm (eigenes Konto)")
        account_type = detected
    if account_type.lower() == "cgm":
        return EasyCGM(username, password, timestamp)
    elif account_type.lower() == "follow":
        return EasyFollow(username, password, timestamp)
    else:
        raise ValueError(f"Unknown account type: {account_type}. Use 'follow' or 'cgm'.")


def main():
    """Main function to sync CGM data and treatments from EasyView to Nightscout."""

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)-7s - %(message)s",
    )

    username = os.getenv("username")
    password = os.getenv("password")
    ns_url = os.getenv("url")
    api_secret = os.getenv("secret")
    account_type = os.getenv("account_type", "auto")
    sync_treatments = os.getenv("sync_treatments", "true").lower() == "true"
    sync_pump_status = os.getenv("sync_pump_status", "true").lower() == "true"

    with NightScout(ns_url, api_secret) as ns:
        client = create_easyview_client(username, password, account_type, ns.timestamp)

        with client as ev:
            for sensor_status in ev:
                ns.add(sensor_status)

                # Sync treatments from pump_status
                if sync_treatments:
                    treatments = ev.get_current_treatments()
                    if treatments:
                        added = ns.add_treatments(treatments)
                        if added > 0:
                            logger.info("synced %i new treatments to nightscout", added)

                # Update pump status (IOB, reservoir, etc.)
                if sync_pump_status and ev.pump_status:
                    ns.update_pump_status(ev.pump_status)


if __name__ == "__main__":
    main()
