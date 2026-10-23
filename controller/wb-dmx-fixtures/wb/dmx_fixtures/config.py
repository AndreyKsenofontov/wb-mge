"""Turn the service configuration into fixtures and groups laid out on the DMX universe.

A `fixtures` entry describes `count` identical fixtures of one profile and mode. With count 1 the
entry is a single fixture with the configured id; with more it becomes fixtures <id>1..<id>N
(<id>_1.. when the id ends with a digit) placed back to back, plus a group device <id> whose
controls drive all of them. A start address of 0 (or none) places the entry right after the
previous one, so a rig of identical fixtures needs no address arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import ofl
from .mapping import FixtureMapping, build_mapping

DEVICE_PREFIX = "dmx_"
DMX_UNIVERSE_SIZE = 512
AUTO_ADDRESS = 0


class ConfigError(ValueError):
    """The configuration cannot be turned into fixtures."""


@dataclass
class Fixture:
    id: str
    name: str
    address: int
    mapping: FixtureMapping
    group: Optional[str] = None      # id of the group the fixture belongs to

    @property
    def device_id(self) -> str:
        return f"{DEVICE_PREFIX}{self.id}"

    @property
    def last_address(self) -> int:
        return self.address + self.mapping.profile.footprint - 1


@dataclass
class Group:
    """Several identical fixtures driven together through one device."""

    id: str
    name: str
    mapping: FixtureMapping          # same controls as a member; holds the last value sent to the group
    members: list[Fixture] = field(default_factory=list)

    @property
    def device_id(self) -> str:
        return f"{DEVICE_PREFIX}{self.id}"

    @property
    def address(self) -> int:
        return self.members[0].address

    @property
    def last_address(self) -> int:
        return self.members[-1].last_address


@dataclass
class Setup:
    fixtures: list[Fixture]
    groups: list[Group]


def member_id(base: str, index: int) -> str:
    """Id of the index-th (1-based) fixture of a group: par → par1, par1 → par1_1."""
    return f"{base}_{index}" if base[-1:].isdigit() else f"{base}{index}"


def load_setup(config: dict[str, Any], profiles: dict[str, dict[str, Any]],
               import_errors: Optional[dict[str, str]] = None) -> Setup:
    """Fixtures and groups of the configuration, checked for overlaps and the universe size.

    `profiles` is the library (key -> OFL fixture JSON); `import_errors` (key -> text) explains a
    profile that is missing because its download failed.
    """
    import_errors = import_errors or {}
    fixtures: list[Fixture] = []
    groups: list[Group] = []
    next_address = 1
    for n, entry in enumerate(config.get("fixtures", []), start=1):
        base_id = str(entry.get("id") or "")
        if not base_id:
            raise ConfigError(f"fixtures entry {n}: id is required")
        key = str(entry.get("fixture", ""))
        if key not in profiles:
            reason = f": {import_errors[key]}" if key in import_errors else ""
            raise ConfigError(f"fixture {base_id!r}: profile {key!r} is not in the library{reason}")
        try:
            profile = ofl.build_profile(profiles[key], key, entry.get("mode"))
        except ofl.ProfileError as e:
            raise ConfigError(f"fixture {base_id!r}: {e}") from e
        count = int(entry.get("count") or 1)
        if count < 1:
            raise ConfigError(f"fixture {base_id!r}: count must be at least 1")
        address = int(entry.get("address") or AUTO_ADDRESS)
        if address == AUTO_ADDRESS:
            address = next_address
        name = str(entry.get("name") or profile.name)

        if count == 1:
            members = [Fixture(base_id, name, address, build_mapping(profile))]
        else:
            members = [Fixture(member_id(base_id, i), f"{name} {i}", address + (i - 1) * profile.footprint,
                               build_mapping(profile), group=base_id)
                       for i in range(1, count + 1)]
            groups.append(Group(base_id, name, build_mapping(profile), members))
        last = members[-1]
        if last.last_address > DMX_UNIVERSE_SIZE:
            raise ConfigError(f"fixture {base_id!r}: {count} × {profile.footprint} channels from address "
                              f"{address} end at {last.last_address}, past {DMX_UNIVERSE_SIZE}")
        fixtures.extend(members)
        next_address = last.last_address + 1

    device_ids = [f.id for f in fixtures] + [g.id for g in groups]
    duplicates = sorted({i for i in device_ids if device_ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"device ids must be unique, repeated: {', '.join(duplicates)}")
    by_address = sorted(fixtures, key=lambda f: f.address)
    for a, b in zip(by_address, by_address[1:]):
        if b.address <= a.last_address:
            raise ConfigError(f"fixtures {a.id!r} ({a.address}..{a.last_address}) and {b.id!r} "
                              f"({b.address}..{b.last_address}) overlap")
    return Setup(fixtures, groups)


def address_map(setup: Setup) -> str:
    """Entries in address order, for the log and the service device: 'par1..par12 DMX 1-108; …'."""
    entries: list[tuple[int, str]] = []
    grouped = {g.id for g in setup.groups}
    for g in setup.groups:
        entries.append((g.address, f"{g.members[0].id}..{g.members[-1].id} DMX {g.address}-{g.last_address}"))
    for f in setup.fixtures:
        if f.group not in grouped:
            entries.append((f.address, f"{f.id} DMX {f.address}-{f.last_address}"))
    return "; ".join(text for _, text in sorted(entries))
