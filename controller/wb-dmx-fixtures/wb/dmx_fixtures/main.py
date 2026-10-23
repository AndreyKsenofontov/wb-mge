"""Command line entry point of wb-dmx-fixtures.

    wb-dmx-fixtures [-c CONFIG]                    run the service (what the systemd unit does)
    wb-dmx-fixtures import <manufacturer/fixture | link>
                                                   download OFL profiles into the library
    wb-dmx-fixtures update-schema                  regenerate the confed schema from the library
    wb-dmx-fixtures show <manufacturer/fixture> [--mode MODE]
                                                   print the controls a fixture gets
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from . import library, ofl
from .config import ConfigError, load_setup
from .mapping import build_mapping
from .version import get_version

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 6

CONFIG_FILEPATH = "/etc/wb-dmx-fixtures.conf"
# wb-mqtt-confed loads /usr/share/wb-mqtt-confed/schemas (packaged) and
# /var/lib/wb-mqtt-confed/schemas (generated at run time); ours is regenerated on every import.
SCHEMA_FILEPATH = "/var/lib/wb-mqtt-confed/schemas/wb-dmx-fixtures.schema.json"
SERVICE_NAME = "wb-dmx-fixtures"

LOG = logging.getLogger("wb-dmx-fixtures")


def write_schema(schema_path: Path, profiles: dict) -> bool:
    return library.write_schema(library.build_schema(profiles, CONFIG_FILEPATH, SERVICE_NAME), schema_path)


def update_schema(schema_path: Path, library_dir: Path) -> int:
    profiles = library.list_profiles(library_dir)
    write_schema(schema_path, profiles)
    print(f"schema {schema_path}: {len(profiles)} profile(s)")
    return EXIT_SUCCESS


def cmd_import(keys: list[str], schema_path: Path, library_dir: Path) -> int:
    rc = EXIT_SUCCESS
    for key in keys:
        try:
            path = library.import_profile(key, library_dir)
            fixture = ofl.load_fixture(path)
            print(f"{key}: {fixture.get('name')} - modes: {', '.join(ofl.mode_names(fixture))}")
        except (OSError, ValueError) as e:
            print(f"{key}: {e}", file=sys.stderr)
            rc = EXIT_FAILURE
    update_schema(schema_path, library_dir)
    return rc


def cmd_show(key: str, mode: Optional[str], library_dir: Path) -> int:
    profiles = library.list_profiles(library_dir)
    if key not in profiles:
        print(f"{key} is not in the library ({library_dir})", file=sys.stderr)
        return EXIT_FAILURE
    try:
        profile = ofl.build_profile(profiles[key], key, mode)
    except ofl.ProfileError as e:
        print(e, file=sys.stderr)
        return EXIT_FAILURE
    mapping = build_mapping(profile)
    print(f"{profile.name} [{profile.mode}], footprint {profile.footprint}")
    for c in mapping.controls:
        span = f"0..{c.max}" if c.type == "range" else c.type
        chans = ", ".join(f"{ch.name}@{ch.offset + 1}" + (f"+fine" if ch.fine_offsets else "") for ch in c.channels)
        print(f"  {c.id:28s} {c.type:5s} {span:9s} <- {chans}   default={mapping.state[c.id]!r}")
        for value, title in c.enum.items():
            print(f"      {value:>3s}: {title['en']}")
    print("render:", list(mapping.render()))
    return EXIT_SUCCESS


def library_status(profiles: dict, result: library.ModelsResult) -> str:
    """Text of the service device's "Fixture models" control."""
    parts = [f"{len(profiles)} in the library"]
    if result.imported:
        parts.append("downloaded: " + ", ".join(result.imported))
    parts += [f"{key}: {e}" for key, e in result.errors.items()]
    return "; ".join(parts)


def run_service(config_path: Path, library_dir: Path, schema_path: Path) -> int:
    # The MQTT client (wb_common) exists only on a controller; the library commands must work
    # without it, so the service module is imported here and not at module level.
    from .service import DmxFixturesService, publish_config_error  # pylint: disable=import-outside-toplevel

    def config_error(text: str) -> int:
        print(f"configuration: {text}", file=sys.stderr)
        publish_config_error(text)
        return EXIT_CONFIG_ERROR

    if not config_path.is_file():
        return config_error(f"file not found: {config_path}")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except ValueError as e:
        return config_error(f"{config_path} is not valid JSON: {e}")
    if not isinstance(config.get("gateway"), dict):
        return config_error("gateway section is required")

    # Models first: the form only offers what is in the library, so a model added in the form is
    # downloaded here and the regenerated schema shows it after a page reload.
    result = library.ensure_models(list(config.get("models") or []), library_dir)
    for key in result.imported:
        LOG.info("model %s downloaded", key)
    for key, e in result.errors.items():
        LOG.warning("model %s: %s", key, e)
    profiles = library.list_profiles(library_dir)
    try:
        if write_schema(schema_path, profiles):
            LOG.info("confed schema updated: %d profile(s)", len(profiles))
    except OSError as e:
        LOG.warning("cannot write the confed schema %s: %s", schema_path, e)

    used = {str(entry.get("fixture", "")) for entry in config.get("fixtures", [])}
    retry = [key for key, e in result.errors.items() if e.transient and key in used and key not in profiles]
    if retry:
        # A network hiccup is not a config error: exit with a plain failure so systemd retries.
        print(f"cannot download {', '.join(retry)}; retrying later", file=sys.stderr)
        return EXIT_FAILURE
    try:
        setup = load_setup(config, profiles, {key: str(e) for key, e in result.errors.items()})
    except (ConfigError, TypeError, ValueError) as e:   # a hand-edited value of a wrong type, too
        return config_error(str(e))
    return DmxFixturesService(config, setup, library_status(profiles, result)).run()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wb-dmx-fixtures", description="DMX fixtures over a WB-MGU gateway")
    parser.add_argument("--version", action="version", version=get_version())
    parser.add_argument("-c", "--config", default=CONFIG_FILEPATH, help="configuration file")
    parser.add_argument("--library", default=str(library.LIBRARY_DIR), help="fixture library directory")
    parser.add_argument("--schema", default=SCHEMA_FILEPATH, help="generated confed schema path")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="command")
    p_import = sub.add_parser("import", help="download OFL profiles into the library")
    p_import.add_argument("keys", nargs="+", metavar="manufacturer/fixture|link")
    sub.add_parser("update-schema", help="regenerate the confed schema from the library")
    p_show = sub.add_parser("show", help="print the controls a profile maps to")
    p_show.add_argument("key", metavar="manufacturer/fixture")
    p_show.add_argument("--mode")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    library_dir = Path(args.library)
    if args.command == "import":
        return cmd_import(args.keys, Path(args.schema), library_dir)
    if args.command == "update-schema":
        return update_schema(Path(args.schema), library_dir)
    if args.command == "show":
        return cmd_show(args.key, args.mode, library_dir)
    return run_service(Path(args.config), library_dir, Path(args.schema))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
