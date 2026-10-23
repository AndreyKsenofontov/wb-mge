"""The wb-dmx-fixtures daemon: fixture MQTT devices on the controller, DMX writes to the gateway."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from types import FrameType
from typing import Any, Optional, Union

from wb_common.mqtt_client import MQTTClient

from .config import Fixture, Group, Setup, address_map
from .mapping import KIND_DIRECT, KIND_LEVEL, KIND_RGB, Control, FixtureMapping
from .modbus_tcp import DmxGatewayClient, ModbusError, merge_ranges

LOG = logging.getLogger("wb-dmx-fixtures")

DRIVER = "wb-dmx-fixtures"
SERVICE_DEVICE = "wb_dmx_fixtures"
SERVICE_TITLE = {"en": "DMX fixtures", "ru": "DMX-приборы"}
SERVICE_CONTROLS = ("status", "gateway", "fixtures", "addresses", "library", "blackout")
GATEWAY_RETRY_S = 5.0
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
CONFIG_ERROR_PUBLISH_TIMEOUT_S = 2.0
STATUS_META = {"type": "text", "readonly": True, "order": 1, "title": {"en": "Status", "ru": "Состояние"}}

Device = Union[Fixture, Group]


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
        client.publish(f"{dev}/meta", json.dumps({"driver": DRIVER, "title": SERVICE_TITLE}, ensure_ascii=False),
                       retain=True)
        client.publish(f"{dev}/controls/status/meta", json.dumps(STATUS_META, ensure_ascii=False), retain=True)
        client.publish(f"{dev}/controls/status", f"config error: {text}", retain=True)
        info = client.publish(f"{dev}/meta/error", "r", retain=True)
        info.wait_for_publish(timeout=CONFIG_ERROR_PUBLISH_TIMEOUT_S)   # stop() would drop a queued message
        client.stop()
    except (ConnectionError, OSError) as e:
        LOG.warning("cannot report the config error over MQTT: %s", e)


class DmxFixturesService:
    def __init__(self, config: dict[str, Any], setup: Setup, library_status: str = "") -> None:
        gw = config["gateway"]
        self._gateway = DmxGatewayClient(str(gw.get("host", "")), int(gw.get("port", 502)), int(gw.get("unit_id", 1)))
        self._write_interval_s = int(gw.get("write_interval_ms", 25)) / 1000
        self._setup = setup
        self._fixtures = {f.device_id: f for f in setup.fixtures}
        self._groups = {g.device_id: g for g in setup.groups}
        self._library_status = library_status
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

    def _devices(self) -> list[Device]:
        return [*self._groups.values(), *self._fixtures.values()]

    # ---- MQTT publishing -------------------------------------------------------------------

    def _pub(self, topic: str, payload: Any, retain: bool = True) -> None:
        self._client.publish(topic, payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                             retain=retain)

    def _publish_service_device(self) -> None:
        dev = f"/devices/{SERVICE_DEVICE}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": SERVICE_TITLE})
        self._pub(f"{dev}/controls/status/meta", STATUS_META)
        self._pub(f"{dev}/controls/gateway/meta", {"type": "text", "readonly": True, "order": 2,
                                                   "title": {"en": "Gateway", "ru": "Шлюз"}})
        self._pub(f"{dev}/controls/gateway", f"{self._gateway.host}:{self._gateway.port}" if self._gateway.host else "")
        self._pub(f"{dev}/controls/fixtures/meta", {"type": "value", "readonly": True, "order": 3,
                                                    "title": {"en": "Fixtures", "ru": "Приборов"}})
        self._pub(f"{dev}/controls/fixtures", str(len(self._fixtures)))
        self._pub(f"{dev}/controls/addresses/meta", {"type": "text", "readonly": True, "order": 4,
                                                     "title": {"en": "DMX addresses", "ru": "DMX-адреса"}})
        self._pub(f"{dev}/controls/addresses", address_map(self._setup))
        self._pub(f"{dev}/controls/library/meta", {"type": "text", "readonly": True, "order": 5,
                                                   "title": {"en": "Fixture models", "ru": "Модели приборов"}})
        self._pub(f"{dev}/controls/library", self._library_status)
        self._pub(f"{dev}/controls/blackout/meta", {"type": "pushbutton", "order": 6,
                                                    "title": {"en": "Blackout", "ru": "Погасить всё"}})
        self._pub(f"{dev}/meta/error", self._error_text)
        self._set_status("starting")

    def _set_status(self, text: str, error: str = "") -> None:
        self._error_text = error
        self._pub(f"/devices/{SERVICE_DEVICE}/controls/status", text)
        self._pub(f"/devices/{SERVICE_DEVICE}/meta/error", error)

    def _publish_device(self, dev_obj: Device) -> None:
        dev = f"/devices/{dev_obj.device_id}"
        mapping = dev_obj.mapping
        profile = mapping.profile
        if isinstance(dev_obj, Group):
            title = {"en": f"{dev_obj.name} (all)", "ru": f"{dev_obj.name} (все)"}
            info = (f"{profile.name} · {profile.mode} · {len(dev_obj.members)} × {profile.footprint} · "
                    f"DMX {dev_obj.address}–{dev_obj.last_address}")
        else:
            title = {"en": dev_obj.name, "ru": dev_obj.name}
            info = f"{profile.name} · {profile.mode} · DMX {dev_obj.address}–{dev_obj.last_address}"
        self._pub(f"{dev}/meta", {"driver": DRIVER, "title": title})
        self._pub(f"{dev}/meta/error", "")
        for c in mapping.controls:
            self._pub(f"{dev}/controls/{c.id}/meta", control_meta(c))
            self._pub(f"{dev}/controls/{c.id}", str(mapping.state[c.id]))
        self._pub(f"{dev}/controls/profile/meta", {"type": "text", "readonly": True, "order": len(mapping.controls) + 1,
                                                   "title": {"en": "Profile", "ru": "Профиль"}})
        self._pub(f"{dev}/controls/profile", info)

    def _unpublish_all(self) -> None:
        """Clear the retained topics so stopped devices do not linger in the UI."""
        for dev_obj in self._devices():
            dev = f"/devices/{dev_obj.device_id}"
            for cid in [c.id for c in dev_obj.mapping.controls] + ["profile"]:
                self._pub(f"{dev}/controls/{cid}/meta", "")
                self._pub(f"{dev}/controls/{cid}", "")
            self._pub(f"{dev}/meta/error", "")
            self._pub(f"{dev}/meta", "")
        dev = f"/devices/{SERVICE_DEVICE}"
        for cid in SERVICE_CONTROLS:
            self._pub(f"{dev}/controls/{cid}/meta", "")
            self._pub(f"{dev}/controls/{cid}", "")
        self._pub(f"{dev}/meta/error", "")
        self._pub(f"{dev}/meta", "")

    def _set_all_error(self, error: str) -> None:
        for dev_obj in self._devices():
            self._pub(f"/devices/{dev_obj.device_id}/meta/error", error)

    # ---- MQTT callbacks ----------------------------------------------------------------------

    def _on_connect(self, _client: MQTTClient, _userdata: Any, _flags: Any, rc: int) -> None:
        if rc != 0:
            LOG.error("MQTT connect failed, rc=%s", rc)
            return
        LOG.info("MQTT connected")
        self._publish_service_device()
        for dev_obj in self._devices():
            self._publish_device(dev_obj)
        self._mark_dirty(*self._fixtures)
        # An MQTT wildcard must span a whole level, so every device is subscribed and the
        # handler keeps only our own ids.
        self._client.subscribe("/devices/+/controls/+/on")

    def _set_and_publish(self, device_id: str, mapping: FixtureMapping, control_id: str, payload: str) -> bool:
        if not mapping.set_value(control_id, payload):
            return False
        self._pub(f"/devices/{device_id}/controls/{control_id}", str(mapping.state[control_id]))
        return True

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
        group = self._groups.get(device_id)
        if group is not None:
            if not self._set_and_publish(device_id, group.mapping, control_id, payload):
                LOG.warning("%s/%s: rejected value %r", device_id, control_id, payload)
                return
            for member in group.members:
                self._set_and_publish(member.device_id, member.mapping, control_id, payload)
            self._mark_dirty(*(m.device_id for m in group.members))
            return
        fx = self._fixtures.get(device_id)
        if fx is None:
            return
        if not self._set_and_publish(device_id, fx.mapping, control_id, payload):
            LOG.warning("%s/%s: rejected value %r", device_id, control_id, payload)
            return
        self._mark_dirty(device_id)

    def _blackout(self) -> None:
        for dev_obj in self._devices():
            for c in dev_obj.mapping.controls:
                if c.kind in (KIND_DIRECT, KIND_LEVEL):
                    self._set_and_publish(dev_obj.device_id, dev_obj.mapping, c.id, "0")
                elif c.kind == KIND_RGB:
                    self._set_and_publish(dev_obj.device_id, dev_obj.mapping, c.id, "0;0;0")
        self._mark_dirty(*self._fixtures)
        try:
            self._gateway.blackout()
        except ModbusError as e:
            LOG.warning("blackout: %s", e)

    def _signal_handler(self, _signum: int, _frame: Optional[FrameType]) -> None:
        LOG.info("stop requested")
        self._stop.set()
        self._wake.set()

    # ---- gateway writer ----------------------------------------------------------------------

    def _mark_dirty(self, *device_ids: str) -> None:
        with self._dirty_lock:
            self._dirty.update(device_ids)
        self._wake.set()

    def _flush(self) -> None:
        with self._dirty_lock:
            pending = list(self._dirty)
            self._dirty.clear()
        if not pending:
            return
        if not self._gateway.host:
            # A freshly installed package has no gateway yet: stay up so the service device and the
            # confed form are there, and say what is missing instead of retrying a connection to "".
            if self._gateway_ok is not False:
                self._gateway_ok = False
                self._set_status("gateway address is not configured", "w")
            return
        blocks = [(self._fixtures[d].address, bytes(self._fixtures[d].mapping.render())) for d in pending]
        try:
            for start, data in merge_ranges(blocks):
                self._gateway.write_channels(start, data)
        except ModbusError as e:
            LOG.warning("gateway write failed: %s", e)
            self._mark_dirty(*pending)
            if self._gateway_ok is not False:
                self._gateway_ok = False
                self._set_status(f"gateway unreachable: {e}", "w")
                self._set_all_error("w")
            self._wake.clear()
            self._wake.wait(GATEWAY_RETRY_S)
            return
        if self._gateway_ok is not True:
            self._gateway_ok = True
            self._set_status("ok")
            self._set_all_error("")

    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            # Coalesce the burst of changes a slider drag produces into one write.
            if self._write_interval_s > 0:
                time.sleep(self._write_interval_s)
            self._flush()

    # ---- lifecycle ---------------------------------------------------------------------------

    def run(self) -> int:
        LOG.info("starting: %d fixture(s) in %d group(s), gateway %s:%d", len(self._fixtures), len(self._groups),
                 self._gateway.host, self._gateway.port)
        LOG.info("addresses: %s", address_map(self._setup))
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
