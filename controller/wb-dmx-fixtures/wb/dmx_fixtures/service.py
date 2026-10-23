"""The wb-dmx-fixtures daemon: fixture MQTT devices on the controller, DMX writes to the gateway."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Optional

from wb_common.mqtt_client import MQTTClient

from . import library, ofl
from .mapping import KIND_DIRECT, KIND_LEVEL, KIND_RGB, Control, FixtureMapping, build_mapping
from .modbus_tcp import DmxGatewayClient, ModbusError

LOG = logging.getLogger("wb-dmx-fixtures")

DRIVER = "wb-dmx-fixtures"
SERVICE_DEVICE = "wb_dmx_fixtures"
DEVICE_PREFIX = "dmx_"
DMX_UNIVERSE_SIZE = 512
GATEWAY_RETRY_S = 5.0
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 6
CONFIG_ERROR_PUBLISH_TIMEOUT_S = 2.0


@dataclass
class Fixture:
    id: str
    name: str
    address: int
    mapping: FixtureMapping

    @property
    def device_id(self) -> str:
        return f"{DEVICE_PREFIX}{self.id}"

    @property
    def last_address(self) -> int:
        return self.address + self.mapping.profile.footprint - 1


class ConfigError(ValueError):
    """The configuration cannot be turned into fixtures."""


def load_fixtures(config: dict[str, Any], library_dir: Path = library.LIBRARY_DIR) -> list[Fixture]:
    profiles = library.list_profiles(library_dir)
    fixtures: list[Fixture] = []
    for entry in config.get("fixtures", []):
        key = entry.get("fixture", "")
        if key not in profiles:
            raise ConfigError(f"fixture {entry.get('id')!r}: profile {key!r} is not in the library")
        try:
            profile = ofl.build_profile(profiles[key], key, entry.get("mode"))
        except ofl.ProfileError as e:
            raise ConfigError(str(e)) from e
        fixture = Fixture(id=str(entry["id"]), name=str(entry.get("name") or profile.name),
                          address=int(entry.get("address", 1)), mapping=build_mapping(profile))
        if fixture.last_address > DMX_UNIVERSE_SIZE:
            raise ConfigError(f"fixture {fixture.id!r}: address {fixture.address} + footprint "
                              f"{profile.footprint} exceeds {DMX_UNIVERSE_SIZE}")
        fixtures.append(fixture)
    ids = [f.id for f in fixtures]
    if len(ids) != len(set(ids)):
        raise ConfigError("fixture ids must be unique")
    for a in fixtures:
        for b in fixtures:
            if a is not b and a.address <= b.last_address and b.address <= a.last_address and a.id < b.id:
                raise ConfigError(f"fixtures {a.id!r} ({a.address}..{a.last_address}) and {b.id!r} "
                                  f"({b.address}..{b.last_address}) overlap")
    return fixtures


def control_meta(c: Control) -> dict[str, Any]:
    meta: dict[str, Any] = {"type": c.type, "order": c.order, "readonly": False,
                            "title": {"en": c.title_en, "ru": c.title_ru}}
    if c.type == "range":
        meta["min"] = 0
        meta["max"] = c.max
    if c.enum:
        meta["enum"] = c.enum
    return meta


def publish_config_error(text: str) -> None:
    """Leave the error on the service device so a bad config saved in the web UI is visible there."""
    client = MQTTClient(f"{DRIVER}-config")
    try:
        client.start()
        dev = f"/devices/{SERVICE_DEVICE}"
        client.publish(f"{dev}/meta", json.dumps({"driver": DRIVER, "title": {"en": "DMX fixtures", "ru": "DMX-приборы"}},
                                                 ensure_ascii=False), retain=True)
        client.publish(f"{dev}/controls/status/meta", json.dumps({"type": "text", "readonly": True, "order": 1,
                                                                  "title": {"en": "Status", "ru": "Состояние"}},
                                                                 ensure_ascii=False), retain=True)
        client.publish(f"{dev}/controls/status", f"config error: {text}", retain=True)
        info = client.publish(f"{dev}/meta/error", "r", retain=True)
        info.wait_for_publish(timeout=CONFIG_ERROR_PUBLISH_TIMEOUT_S)   # stop() would drop a queued message
        client.stop()
    except (ConnectionError, OSError) as e:
        LOG.warning("cannot report the config error over MQTT: %s", e)


class DmxFixturesService:
    def __init__(self, config: dict[str, Any], fixtures: list[Fixture]) -> None:
        gw = config["gateway"]
        self._gateway = DmxGatewayClient(str(gw.get("host", "")), int(gw.get("port", 502)), int(gw.get("unit_id", 1)))
        self._write_interval_s = int(gw.get("write_interval_ms", 25)) / 1000
        self._fixtures = {f.device_id: f for f in fixtures}
        self._dirty: set[str] = set()
        self._dirty_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._gateway_ok: Optional[bool] = None
        self._client = MQTTClient(DRIVER)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.will_set(f"/devices/{SERVICE_DEVICE}/meta/error", "p", retain=True)
        self._error_text = ""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    # ---- MQTT publishing -------------------------------------------------------------------

    def _pub(self, topic: str, payload: Any, retain: bool = True) -> None:
        self._client.publish(topic, payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                             retain=retain)

    def _publish_service_device(self) -> None:
        dev = f"/devices/{SERVICE_DEVICE}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": {"en": "DMX fixtures", "ru": "DMX-приборы"}})
        self._pub(f"{dev}/controls/status/meta", {"type": "text", "readonly": True, "order": 1,
                                                  "title": {"en": "Status", "ru": "Состояние"}})
        self._pub(f"{dev}/controls/gateway/meta", {"type": "text", "readonly": True, "order": 2,
                                                   "title": {"en": "Gateway", "ru": "Шлюз"}})
        self._pub(f"{dev}/controls/gateway", f"{self._gateway.host}:{self._gateway.port}" if self._gateway.host else "")
        self._pub(f"{dev}/controls/fixtures/meta", {"type": "value", "readonly": True, "order": 3,
                                                    "title": {"en": "Fixtures", "ru": "Приборов"}})
        self._pub(f"{dev}/controls/fixtures", str(len(self._fixtures)))
        self._pub(f"{dev}/controls/blackout/meta", {"type": "pushbutton", "order": 4,
                                                    "title": {"en": "Blackout", "ru": "Погасить всё"}})
        self._pub(f"{dev}/meta/error", self._error_text)
        self._set_status("starting")

    def _set_status(self, text: str, error: str = "") -> None:
        self._error_text = error
        self._pub(f"/devices/{SERVICE_DEVICE}/controls/status", text)
        self._pub(f"/devices/{SERVICE_DEVICE}/meta/error", error)

    def _publish_fixture(self, fx: Fixture) -> None:
        dev = f"/devices/{fx.device_id}"
        profile = fx.mapping.profile
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": {"en": fx.name, "ru": fx.name}})
        self._pub(f"{dev}/meta/error", "")
        for c in fx.mapping.controls:
            self._pub(f"{dev}/controls/{c.id}/meta", control_meta(c))
            self._pub(f"{dev}/controls/{c.id}", str(fx.mapping.state[c.id]))
        info_order = len(fx.mapping.controls) + 1
        self._pub(f"{dev}/controls/profile/meta", {"type": "text", "readonly": True, "order": info_order,
                                                   "title": {"en": "Profile", "ru": "Профиль"}})
        self._pub(f"{dev}/controls/profile",
                  f"{profile.name} · {profile.mode} · DMX {fx.address}–{fx.last_address}")

    def _unpublish_all(self) -> None:
        """Clear the retained topics so stopped devices do not linger in the UI."""
        for fx in self._fixtures.values():
            dev = f"/devices/{fx.device_id}"
            for c in fx.mapping.controls:
                self._pub(f"{dev}/controls/{c.id}/meta", "")
                self._pub(f"{dev}/controls/{c.id}", "")
            self._pub(f"{dev}/controls/profile/meta", "")
            self._pub(f"{dev}/controls/profile", "")
            self._pub(f"{dev}/meta/error", "")
            self._pub(f"{dev}/meta", "")
        dev = f"/devices/{SERVICE_DEVICE}"
        for cid in ("status", "gateway", "fixtures", "blackout"):
            self._pub(f"{dev}/controls/{cid}/meta", "")
            self._pub(f"{dev}/controls/{cid}", "")
        self._pub(f"{dev}/meta/error", "")
        self._pub(f"{dev}/meta", "")

    # ---- MQTT callbacks ----------------------------------------------------------------------

    def _on_connect(self, _client: MQTTClient, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc != 0:
            LOG.error("MQTT connect failed, rc=%s", rc)
            return
        LOG.info("MQTT connected")
        self._publish_service_device()
        for fx in self._fixtures.values():
            self._publish_fixture(fx)
            self._mark_dirty(fx.device_id)
        # An MQTT wildcard must span a whole level, so every device is subscribed and the
        # handler keeps only our own ids.
        self._client.subscribe("/devices/+/controls/+/on")

    def _on_message(self, _client: MQTTClient, _userdata: Any, msg: Any) -> None:
        parts = msg.topic.split("/")
        # ['', 'devices', <dev>, 'controls', <ctrl>, 'on']
        if len(parts) != 6:
            return
        device_id, control_id = parts[2], parts[4]
        payload = msg.payload.decode("utf-8", "replace")
        if device_id == SERVICE_DEVICE:
            if control_id == "blackout":
                self._blackout()
            return
        fx = self._fixtures.get(device_id)
        if fx is None:
            return
        if not fx.mapping.set_value(control_id, payload):
            LOG.warning("%s/%s: rejected value %r", device_id, control_id, payload)
            return
        self._pub(f"/devices/{device_id}/controls/{control_id}", str(fx.mapping.state[control_id]))
        self._mark_dirty(device_id)

    def _blackout(self) -> None:
        for fx in self._fixtures.values():
            for c in fx.mapping.controls:
                if c.kind in (KIND_DIRECT, KIND_LEVEL):
                    fx.mapping.set_value(c.id, "0")
                elif c.kind == KIND_RGB:
                    fx.mapping.set_value(c.id, "0;0;0")
                self._pub(f"/devices/{fx.device_id}/controls/{c.id}", str(fx.mapping.state[c.id]))
            self._mark_dirty(fx.device_id)
        try:
            self._gateway.blackout()
        except ModbusError as e:
            LOG.warning("blackout: %s", e)

    def _signal_handler(self, _signum: int, _frame: Optional[FrameType]) -> None:
        LOG.info("stop requested")
        self._stop.set()
        self._wake.set()

    # ---- gateway writer ----------------------------------------------------------------------

    def _mark_dirty(self, device_id: str) -> None:
        with self._dirty_lock:
            self._dirty.add(device_id)
        self._wake.set()

    def _flush(self) -> None:
        with self._dirty_lock:
            pending = list(self._dirty)
            self._dirty.clear()
        if not self._gateway.host:
            # A freshly installed package has no gateway yet: stay up so the service device and the
            # confed form are there, and say what is missing instead of retrying a connection to "".
            if self._gateway_ok is not False:
                self._gateway_ok = False
                self._set_status("gateway address is not configured", "w")
            return
        for device_id in pending:
            fx = self._fixtures[device_id]
            data = bytes(fx.mapping.render())
            try:
                self._gateway.write_channels(fx.address, data)
                if self._gateway_ok is not True:
                    self._gateway_ok = True
                    self._set_status("ok")
                    for other in self._fixtures.values():
                        self._pub(f"/devices/{other.device_id}/meta/error", "")
            except ModbusError as e:
                LOG.warning("gateway write failed for %s: %s", device_id, e)
                with self._dirty_lock:
                    self._dirty.add(device_id)
                if self._gateway_ok is not False:
                    self._gateway_ok = False
                    self._set_status(f"gateway unreachable: {e}", "w")
                    for other in self._fixtures.values():
                        self._pub(f"/devices/{other.device_id}/meta/error", "w")
                self._wake.clear()
                self._wake.wait(GATEWAY_RETRY_S)
                return

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            # Coalesce the burst of changes a slider drag produces into one write per fixture.
            if self._write_interval_s > 0:
                time.sleep(self._write_interval_s)
            self._flush()

    # ---- lifecycle ---------------------------------------------------------------------------

    def run(self) -> int:
        LOG.info("starting: %d fixture(s), gateway %s:%d", len(self._fixtures), self._gateway.host, self._gateway.port)
        try:
            # Synchronous connect: an unreachable broker raises and systemd restarts the unit.
            self._client.start()
        except (ConnectionError, OSError) as e:
            LOG.error("MQTT broker unavailable: %s", e)
            return EXIT_FAILURE
        writer = threading.Thread(target=self._writer_loop, name="dmx-writer", daemon=True)
        writer.start()
        self._stop.wait()
        writer.join(timeout=5)
        self._unpublish_all()
        self._gateway.close()
        self._client.stop()
        LOG.info("stopped")
        return EXIT_SUCCESS
