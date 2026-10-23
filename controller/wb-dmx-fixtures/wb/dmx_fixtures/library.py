"""The on-controller fixture library and the confed schema generated from it.

Profiles live as OFL JSON files under LIBRARY_DIR/<manufacturer>/<fixture>.json. The confed
schema offers only the profiles in the library: json-editor renders a static form, so the
fixture→modes dependency is expressed as one oneOf branch per library entry.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from . import ofl

LIBRARY_DIR = Path("/mnt/data/wb-dmx-fixtures/library")
OFL_RAW_URL = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/fixtures/{key}.json"
KEY_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")


def list_profiles(library_dir: Path = LIBRARY_DIR) -> dict[str, dict[str, Any]]:
    """key -> fixture JSON for every profile in the library, sorted by key."""
    out: dict[str, dict[str, Any]] = {}
    if not library_dir.is_dir():
        return out
    for path in sorted(library_dir.glob("*/*.json")):
        key = f"{path.parent.name}/{path.stem}"
        try:
            out[key] = ofl.load_fixture(path)
        except (OSError, ValueError):
            continue
    return out


def import_profile(key: str, library_dir: Path = LIBRARY_DIR, timeout_s: float = 20.0) -> Path:
    """Download one fixture from OFL into the library. Returns the file path."""
    if not KEY_RE.match(key):
        raise ValueError(f"fixture key must look like manufacturer/fixture, got {key!r}")
    with urllib.request.urlopen(OFL_RAW_URL.format(key=key), timeout=timeout_s) as resp:   # nosec B310 - fixed https host
        data = resp.read()
    fixture = json.loads(data.decode("utf-8"))
    if "modes" not in fixture or "availableChannels" not in fixture:
        raise ValueError(f"{key}: not an OFL fixture file")
    path = library_dir / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fixture_branch(key: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """One oneOf option of the fixtures array: this fixture type with its modes."""
    modes = ofl.mode_names(fixture)
    manufacturer = key.split("/")[0]
    return {
        "title": f"{fixture.get('name', key)} ({manufacturer})",
        "type": "object",
        "properties": {
            "fixture": {"type": "string", "enum": [key], "default": key, "options": {"hidden": True}},
            "id": {
                "type": "string", "title": "Device id", "propertyOrder": 1,
                "pattern": "^[a-z0-9_]{1,32}$",
                "description": "MQTT device id suffix: dmx_<id>. Lowercase letters, digits and underscores.",
            },
            "name": {"type": "string", "title": "Name", "propertyOrder": 2},
            "mode": {
                "type": "string", "title": "DMX mode", "enum": modes, "default": modes[0] if modes else "",
                "propertyOrder": 3,
            },
            "address": {
                "type": "integer", "title": "Start address", "minimum": 1, "maximum": 512, "default": 1,
                "propertyOrder": 4,
            },
        },
        "required": ["fixture", "id", "mode", "address"],
    }


def build_schema(profiles: dict[str, dict[str, Any]], config_path: str, service: str) -> dict[str, Any]:
    branches = [fixture_branch(k, f) for k, f in profiles.items()]
    if not branches:
        branches = [{
            "title": "No profiles in the library",
            "type": "object",
            "properties": {
                "fixture": {"type": "string", "title": "Fixture (library is empty)", "options": {"hidden": True}},
                "id": {"type": "string", "title": "Device id"},
            },
            "required": ["id"],
        }]
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "title": "DMX fixtures (WB-MGU gateway)",
        "description": "Fixtures driven over a WB-MGU DMX gateway. Profiles come from the Open Fixture Library; "
                       "add more with: wb-dmx-fixtures import <manufacturer/fixture>",
        "configFile": {"path": config_path, "service": service},
        "properties": {
            "gateway": {
                "type": "object", "title": "Gateway", "propertyOrder": 1,
                "properties": {
                    "host": {"type": "string", "title": "Address", "propertyOrder": 1},
                    "port": {"type": "integer", "title": "Modbus TCP port", "default": 502, "minimum": 1,
                             "maximum": 65535, "propertyOrder": 2},
                    "unit_id": {"type": "integer", "title": "Unit id", "default": 1, "minimum": 0, "maximum": 255,
                                "propertyOrder": 3},
                    "write_interval_ms": {
                        "type": "integer", "title": "Minimum interval between writes, ms", "default": 25,
                        "minimum": 0, "maximum": 1000, "propertyOrder": 4,
                        "description": "Changes arriving faster than this are merged into one write per fixture.",
                    },
                },
                "required": ["host"],
            },
            "fixtures": {
                "type": "array", "title": "Fixtures", "propertyOrder": 2,
                "items": {"headerTemplate": "{{self.name}} @ {{self.address}}", "oneOf": branches},
            },
        },
        "required": ["gateway", "fixtures"],
        "translations": {
            "ru": {
                "DMX fixtures (WB-MGU gateway)": "DMX-приборы (шлюз WB-MGU)",
                "Gateway": "Шлюз", "Address": "Адрес", "Modbus TCP port": "Порт Modbus TCP",
                "Unit id": "Unit id", "Minimum interval between writes, ms": "Минимальный интервал записи, мс",
                "Fixtures": "Приборы", "Device id": "Идентификатор устройства", "Name": "Название",
                "DMX mode": "Режим DMX", "Start address": "Стартовый адрес",
                "No profiles in the library": "В библиотеке нет профилей",
            }
        },
    }


def write_schema(schema: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
