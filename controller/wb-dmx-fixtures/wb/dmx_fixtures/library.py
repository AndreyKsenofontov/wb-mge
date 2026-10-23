"""The on-controller fixture library and the confed schema generated from it.

Profiles live as OFL JSON files under LIBRARY_DIR/<manufacturer>/<fixture>.json. They get there
from the `models` list of the configuration (links to open-fixture-library.org pages, OFL keys or
URLs of any OFL-format JSON), from `wb-dmx-fixtures import`, or by hand (a custom profile is a
file under LIBRARY_DIR/custom/). The confed schema offers only the profiles in the library:
json-editor renders a static form, so the fixture→modes dependency is expressed as one oneOf
branch per library entry.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import ofl

LIBRARY_DIR = Path("/mnt/data/wb-dmx-fixtures/library")
OFL_RAW_URL = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/fixtures/{key}.json"
OFL_SITE_HOST = "open-fixture-library.org"
OFL_GITHUB_PATH_RE = re.compile(r"/OpenLightingProject/open-fixture-library/(?:blob/|raw/)?[^/]+/fixtures/"
                                r"([a-z0-9-]+)/([a-z0-9-]+)\.json$")
KEY_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
CUSTOM_MANUFACTURER = "custom"
DOWNLOAD_TIMEOUT_S = 20.0
HTTP_NOT_FOUND = 404
# homeui's json-editor hides optional properties missing from the config; this shows them anyway.
SHOW_OPTIONAL = {"wb": {"show_editor": True}}

# json-editor renders descriptions as HTML, so angle brackets would vanish from them.
SCHEMA_DESCRIPTION = ("Fixtures driven over a WB-MGU DMX gateway. Fixture models come from the Open Fixture "
                      "Library: paste the link to a fixture page into Fixture models, save, reload the page, "
                      "and the model appears in the list of fixtures.")
MODELS_DESCRIPTION = ("Links to fixture pages on open-fixture-library.org (or keys like cameo/q-spot-40-rgbw, "
                      "or links to an OFL-format JSON file). Missing models are downloaded when the settings "
                      "are saved; reload the page afterwards.")
ID_DESCRIPTION = ("MQTT device id: dmx_par for par. With several fixtures they get par1, par2, … and dmx_par "
                  "controls them all. Lowercase letters, digits and underscores.")
COUNT_DESCRIPTION = "Number of identical fixtures; they take consecutive addresses."
ADDRESS_DESCRIPTION = "First DMX channel; 0 places the fixtures right after the previous entry."
WRITE_INTERVAL_DESCRIPTION = "Changes arriving faster than this are merged into one write."

FetchFn = Callable[[str], bytes]


class ModelError(ValueError):
    """A model cannot be imported. `transient` is set for network failures worth a retry."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass
class ModelRef:
    key: str                         # library key: manufacturer/fixture
    url: str                         # where the JSON is downloaded from


def parse_model_ref(ref: str) -> ModelRef:
    """Library key and download URL of a `models` entry.

    Accepts an OFL key, a fixture page on open-fixture-library.org, a file in the OFL GitHub
    repository, or any https link to an OFL-format JSON (stored as custom/<file name>).
    """
    ref = ref.strip()
    if KEY_RE.match(ref):
        return ModelRef(ref, OFL_RAW_URL.format(key=ref))
    parsed = urllib.parse.urlparse(ref)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ModelError(f"{ref!r}: expected an open-fixture-library.org link, a manufacturer/fixture key "
                           "or a link to a JSON file")
    path = parsed.path.rstrip("/")
    if parsed.netloc.endswith(OFL_SITE_HOST):
        key = path.strip("/")
        if key.endswith(".json"):
            key = key[: -len(".json")]
        if not KEY_RE.match(key):
            raise ModelError(f"{ref!r}: not a fixture page (expected {OFL_SITE_HOST}/manufacturer/fixture)")
        return ModelRef(key, OFL_RAW_URL.format(key=key))
    m = OFL_GITHUB_PATH_RE.search(path)
    if m and parsed.netloc.endswith(("github.com", "githubusercontent.com")):
        key = f"{m.group(1)}/{m.group(2)}"
        return ModelRef(key, OFL_RAW_URL.format(key=key))
    stem = re.sub(r"[^a-z0-9-]+", "-", Path(path).stem.lower()).strip("-")
    if not stem:
        raise ModelError(f"{ref!r}: cannot derive a profile name from the link")
    return ModelRef(f"{CUSTOM_MANUFACTURER}/{stem}", ref)


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp:   # nosec B310 - https links
            return resp.read()
    except urllib.error.HTTPError as e:
        # 404 is a wrong link; other statuses (rate limit, 5xx) may pass on a retry.
        raise ModelError(f"download failed: HTTP {e.code}", transient=e.code != HTTP_NOT_FOUND) from e
    except (urllib.error.URLError, OSError) as e:
        raise ModelError(f"download failed: {getattr(e, 'reason', e)}", transient=True) from e


def check_fixture(key: str, fixture: Any) -> None:
    """Reject a JSON that is not an OFL fixture or has no mode the service can drive."""
    if not isinstance(fixture, dict) or "modes" not in fixture or not (
            "availableChannels" in fixture or "templateChannels" in fixture):
        raise ModelError(f"{key}: not an OFL fixture file")
    errors = []
    for mode in ofl.mode_names(fixture):
        try:
            ofl.build_profile(fixture, key, mode)
            return
        except ofl.ProfileError as e:
            errors.append(str(e))
    raise ModelError(f"{key}: no usable mode ({'; '.join(errors) or 'no modes'})")


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


def import_model(ref: ModelRef, library_dir: Path = LIBRARY_DIR, fetch: Optional[FetchFn] = None) -> Path:
    """Download one profile into the library (overwriting it). Returns the file path."""
    data = (fetch or _fetch)(ref.url)
    try:
        fixture = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ModelError(f"{ref.key}: not a JSON file") from e
    check_fixture(ref.key, fixture)
    path = library_dir / f"{ref.key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def import_profile(key: str, library_dir: Path = LIBRARY_DIR) -> Path:
    """Download an OFL key or link into the library (the `import` command)."""
    return import_model(parse_model_ref(key), library_dir)


@dataclass
class ModelsResult:
    imported: list[str]              # keys downloaded now
    errors: dict[str, ModelError]  # key (or the raw entry when it cannot be parsed) -> error


def ensure_models(refs: list[str], library_dir: Path = LIBRARY_DIR, fetch: Optional[FetchFn] = None) -> ModelsResult:
    """Download the `models` entries that are not in the library yet."""
    result = ModelsResult([], {})
    for raw in refs:
        if not str(raw).strip():
            continue
        try:
            ref = parse_model_ref(str(raw))
        except ModelError as e:
            result.errors[str(raw)] = e
            continue
        if (library_dir / f"{ref.key}.json").is_file():
            continue
        try:
            import_model(ref, library_dir, fetch)
            result.imported.append(ref.key)
        except ModelError as e:
            result.errors[ref.key] = e
    return result


def fixture_branch(key: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """One oneOf option of the fixtures array: this fixture type with its modes."""
    modes = ofl.mode_names(fixture)
    manufacturer = key.split("/")[0]
    return {
        "title": f"{fixture.get('name', key)} ({manufacturer})",
        "type": "object",
        "properties": {
            # A hidden one-value enum becomes a select that json-editor leaves empty when the user switches
            # the oneOf branch (it carries the previous branch's value over), and the entry then matches no
            # branch. A plain string with a template is always re-filled with the key; the pattern keeps
            # the branches mutually exclusive for oneOf.
            "fixture": {"type": "string", "pattern": f"^{re.escape(key)}$", "default": key, "template": key,
                        "options": {"hidden": True}},
            "id": {
                "type": "string", "title": "Device id", "propertyOrder": 1,
                "pattern": "^[a-z0-9_]{1,32}$", "description": ID_DESCRIPTION,
            },
            "name": {"type": "string", "title": "Name", "propertyOrder": 2, "options": SHOW_OPTIONAL},
            "mode": {
                "type": "string", "title": "DMX mode", "enum": modes, "default": modes[0] if modes else "",
                "propertyOrder": 3,
            },
            "count": {
                "type": "integer", "title": "Count", "minimum": 1, "maximum": 512, "default": 1,
                "propertyOrder": 4, "description": COUNT_DESCRIPTION, "options": SHOW_OPTIONAL,
            },
            "address": {
                "type": "integer", "title": "Start address", "minimum": 0, "maximum": 512, "default": 0,
                "propertyOrder": 5, "description": ADDRESS_DESCRIPTION, "options": SHOW_OPTIONAL,
            },
        },
        # count and address are optional so configs written before they existed stay valid.
        "required": ["fixture", "id", "mode"],
        "options": {"disable_edit_json": True, "disable_properties": True},
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
    no_json = {"disable_edit_json": True, "disable_properties": True, "disable_collapse": True}
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "title": "DMX fixtures (WB-MGU gateway)",
        "description": SCHEMA_DESCRIPTION,
        "configFile": {"path": config_path, "service": service},
        "options": no_json,
        "properties": {
            "gateway": {
                "type": "object", "title": "Gateway", "propertyOrder": 1, "options": no_json,
                "properties": {
                    "host": {"type": "string", "title": "Address", "propertyOrder": 1},
                    "port": {"type": "integer", "title": "Modbus TCP port", "default": 502, "minimum": 1,
                             "maximum": 65535, "propertyOrder": 2},
                    "unit_id": {"type": "integer", "title": "Unit id", "default": 1, "minimum": 0, "maximum": 255,
                                "propertyOrder": 3},
                    "write_interval_ms": {
                        "type": "integer", "title": "Minimum interval between writes, ms", "default": 25,
                        "minimum": 0, "maximum": 1000, "propertyOrder": 4,
                        "description": WRITE_INTERVAL_DESCRIPTION,
                    },
                },
                "required": ["host"],
            },
            "models": {
                "type": "array", "title": "Fixture models", "propertyOrder": 2,
                "description": MODELS_DESCRIPTION, "format": "table",
                "items": {"type": "string", "title": "Link or key", "minLength": 3},
                "options": {"disable_array_reorder": True, **SHOW_OPTIONAL},
            },
            "fixtures": {
                "type": "array", "title": "Fixtures", "propertyOrder": 3,
                "items": {"headerTemplate": "{{self.name}} × {{self.count}}", "oneOf": branches},
            },
        },
        "required": ["gateway", "fixtures"],
        "translations": {
            "ru": {
                "DMX fixtures (WB-MGU gateway)": "DMX-приборы (шлюз WB-MGU)",
                "Gateway": "Шлюз", "Address": "Адрес", "Modbus TCP port": "Порт Modbus TCP",
                "Unit id": "Unit id", "Minimum interval between writes, ms": "Минимальный интервал записи, мс",
                "Fixture models": "Модели приборов", "Link or key": "Ссылка или ключ",
                "Fixtures": "Приборы", "Device id": "Идентификатор устройства", "Name": "Название",
                "DMX mode": "Режим DMX", "Count": "Количество", "Start address": "Стартовый адрес",
                "No profiles in the library": "В библиотеке нет профилей",
                SCHEMA_DESCRIPTION: "Приборы, подключённые через DMX-шлюз WB-MGU. Модели берутся из Open Fixture "
                                    "Library: вставьте ссылку на страницу прибора в «Модели приборов», сохраните, "
                                    "обновите страницу — модель появится в списке приборов.",
                MODELS_DESCRIPTION: "Ссылки на страницы приборов на open-fixture-library.org (или ключи вида "
                                    "cameo/q-spot-40-rgbw, или ссылки на JSON в формате OFL). Недостающие модели "
                                    "скачиваются при сохранении настроек; после этого обновите страницу.",
                ID_DESCRIPTION: "Идентификатор MQTT-устройства: dmx_par для par. Если приборов несколько, они "
                                "получают par1, par2, …, а dmx_par управляет всеми сразу. Строчные латинские "
                                "буквы, цифры и подчёркивание.",
                COUNT_DESCRIPTION: "Сколько одинаковых приборов; они займут адреса подряд.",
                ADDRESS_DESCRIPTION: "Первый DMX-канал; 0 — сразу после предыдущей записи.",
                WRITE_INTERVAL_DESCRIPTION: "Изменения, приходящие чаще, объединяются в одну запись.",
            }
        },
    }


def write_schema(schema: dict[str, Any], path: Path) -> bool:
    """Write the schema when it differs from the file. Returns True when the file changed.

    wb-mqtt-confed reloads a schema whenever its file is written, so an unchanged schema is not
    rewritten on every service start.
    """
    text = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True
