"""Open Fixture Library (OFL) fixture profiles.

Reads a fixture JSON as published by https://open-fixture-library.org (MIT licensed data) and
flattens one of its modes into a list of DMX channels with their capabilities. Only the parts of
the OFL schema needed to drive a fixture are modelled: channels, fine channel aliases, the
capability list of a channel and the handful of capability fields the control mapping looks at.
Matrix fixtures with template channels are expanded for the pixels a mode names explicitly;
`matrixChannels` insert blocks are not supported yet and raise ProfileError.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DMX_MAX = 255


class ProfileError(ValueError):
    """The fixture JSON cannot be turned into a channel list."""


@dataclass
class Capability:
    """One DMX value range of a channel and what the fixture does in it."""

    type: str
    dmx_start: int
    dmx_end: int
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Short human name of the capability, built from the fields OFL provides."""
        d = self.data
        if "comment" in d:
            return str(d["comment"])
        t = self.type
        if t == "ShutterStrobe":
            effect = str(d.get("shutterEffect", "Strobe"))
            if "speedStart" in d or "speed" in d:
                return f"{effect} {_span(d, 'speed')}".strip()
            return effect
        if t == "WheelSlot":
            return f"Slot {d.get('slotNumber', '?')}"
        if t == "WheelShake":
            return f"Shake slot {d.get('slotNumber', '?')}"
        if t == "WheelSlotRotation" or t == "WheelRotation":
            return f"Rotation {_span(d, 'speed') or _span(d, 'angle')}".strip()
        if t == "ColorPreset":
            return "Color preset"
        if t == "ColorTemperature":
            return f"Color temperature {_span(d, 'colorTemperature')}".strip()
        if t == "Effect":
            return str(d.get("effectName", d.get("effectPreset", "Effect")))
        if t == "Maintenance":
            return str(d.get("parameter", "Maintenance"))
        if t == "NoFunction":
            return "Off"
        if t == "Prism":
            return "Prism"
        if t == "Generic":
            return "Generic"
        return t

    @property
    def is_continuous(self) -> bool:
        """True when the value inside the range means something (speed, angle, intensity...)."""
        d = self.data
        if any(k.endswith("Start") for k in d):
            return True
        return self.type in {
            "Intensity", "ColorIntensity", "Pan", "Tilt", "PanContinuous", "TiltContinuous",
            "Zoom", "Focus", "Iris", "Frost", "Fog", "FogOutput", "Speed", "StrobeSpeed",
            "StrobeDuration", "EffectSpeed", "EffectDuration", "EffectParameter", "BeamAngle",
            "BeamPosition", "Rotation", "PanTiltSpeed", "SoundSensitivity", "Time", "Generic",
            "BladeInsertion", "BladeRotation", "BladeSystemRotation", "PrismRotation",
            "WheelRotation", "WheelSlotRotation", "WheelShake", "IrisEffect", "FrostEffect",
        }


@dataclass
class Channel:
    """One DMX channel of a mode: its name, offset in the fixture and capabilities."""

    name: str
    offset: int                      # 0-based offset from the fixture start address
    capabilities: list[Capability]
    fine_offsets: list[int] = field(default_factory=list)   # offsets of the fine bytes, coarsest first
    default_value: int = 0
    color: Optional[str] = None      # ColorIntensity color name (Red, Green, ...) for single-capability channels

    @property
    def resolution_bits(self) -> int:
        return 8 * (1 + len(self.fine_offsets))

    @property
    def max_value(self) -> int:
        return (1 << self.resolution_bits) - 1

    @property
    def single(self) -> Optional[Capability]:
        """The capability when the channel has exactly one covering the whole range."""
        if len(self.capabilities) == 1 and self.capabilities[0].dmx_start == 0 and self.capabilities[0].dmx_end == DMX_MAX:
            return self.capabilities[0]
        return None


@dataclass
class Profile:
    """A fixture in one mode."""

    key: str                         # "manufacturer/fixture" as in OFL
    name: str
    manufacturer: str
    mode: str
    channels: list[Channel]
    categories: list[str] = field(default_factory=list)

    @property
    def footprint(self) -> int:
        """Number of DMX addresses the mode occupies."""
        return max((c.offset + 1 + len(c.fine_offsets) for c in self.channels), default=0)


def _span(d: dict[str, Any], key: str) -> str:
    if f"{key}Start" in d:
        return f"{d[f'{key}Start']}→{d[f'{key}End']}"
    if key in d:
        return str(d[key])
    return ""


def _capabilities_of(chan: dict[str, Any]) -> list[Capability]:
    if "capability" in chan:
        cap = chan["capability"]
        return [Capability(str(cap.get("type", "Generic")), 0, DMX_MAX, {k: v for k, v in cap.items() if k != "type"})]
    caps = []
    for cap in chan.get("capabilities", []):
        start, end = cap.get("dmxRange", [0, DMX_MAX])
        caps.append(Capability(str(cap.get("type", "Generic")), int(start), int(end),
                               {k: v for k, v in cap.items() if k not in ("type", "dmxRange")}))
    return caps or [Capability("Generic", 0, DMX_MAX)]


def _default_value(chan: dict[str, Any]) -> int:
    v = chan.get("defaultValue", 0)
    if isinstance(v, str):
        m = re.match(r"^(\d+(?:\.\d+)?)%$", v)
        return round(float(m.group(1)) * DMX_MAX / 100) if m else 0
    return int(v)


def _expand_template(name: str, pixel: str) -> str:
    return name.replace("$pixelKey", pixel)


def load_fixture(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def mode_names(fixture: dict[str, Any]) -> list[str]:
    return [str(m["name"]) for m in fixture.get("modes", [])]


def build_profile(fixture: dict[str, Any], key: str, mode_name: Optional[str] = None) -> Profile:
    """Flatten `mode_name` (default: the first mode) of the fixture into a Profile."""
    modes = fixture.get("modes") or []
    if not modes:
        raise ProfileError(f"{key}: fixture has no modes")
    mode = modes[0] if mode_name is None else next((m for m in modes if m.get("name") == mode_name), None)
    if mode is None:
        raise ProfileError(f"{key}: no mode named {mode_name!r}; available: {mode_names(fixture)}")

    available = dict(fixture.get("availableChannels") or {})
    templates = dict(fixture.get("templateChannels") or {})

    # Resolve every entry of the mode's channel list to its definition, expanding matrix templates
    # for pixel keys that appear in the list as "<Template> <pixel>"... OFL writes them as
    # "$pixelKey"-substituted names, so a "Red 1" entry matches the template "Red $pixelKey".
    def lookup(entry: str) -> tuple[str, dict[str, Any]]:
        if entry in available:
            return entry, available[entry]
        for tname, tdef in templates.items():
            pattern = "^" + re.escape(tname).replace(re.escape("$pixelKey"), r"(.+)") + "$"
            m = re.match(pattern, entry)
            if m:
                pixel = m.group(1)
                expanded = json.loads(json.dumps(tdef).replace("$pixelKey", pixel))
                return entry, expanded
        raise ProfileError(f"{key}: channel {entry!r} of mode {mode.get('name')!r} is not defined")

    # Fine channel aliases point back at their coarse channel.
    alias_of: dict[str, tuple[str, int]] = {}
    for cname, cdef in list(available.items()):
        for i, alias in enumerate(cdef.get("fineChannelAliases", [])):
            alias_of[alias] = (cname, i)
    for tname, tdef in templates.items():
        for i, alias in enumerate(tdef.get("fineChannelAliases", [])):
            alias_of[alias] = (tname, i)

    channels: dict[str, Channel] = {}
    order: list[str] = []
    for offset, entry in enumerate(mode.get("channels") or []):
        if entry is None:
            continue   # unused address inside the footprint
        if isinstance(entry, dict):
            raise ProfileError(f"{key}: matrix insert blocks are not supported (mode {mode.get('name')!r})")
        entry = str(entry)
        alias = alias_of.get(entry)
        if alias is None:
            # A pixel-expanded alias: "Red fine 1" -> template "Red fine $pixelKey"
            for alias_name, (base, idx) in alias_of.items():
                if "$pixelKey" in alias_name:
                    pattern = "^" + re.escape(alias_name).replace(re.escape("$pixelKey"), r"(.+)") + "$"
                    m = re.match(pattern, entry)
                    if m:
                        alias = (_expand_template(base, m.group(1)), idx)
                        break
        if alias is not None:
            base_name, idx = alias
            base = channels.get(base_name)
            if base is None:
                raise ProfileError(f"{key}: fine channel {entry!r} precedes its coarse channel {base_name!r}")
            while len(base.fine_offsets) <= idx:
                base.fine_offsets.append(-1)
            base.fine_offsets[idx] = offset
            continue
        name, cdef = lookup(entry)
        caps = _capabilities_of(cdef)
        color = None
        if len(caps) == 1 and caps[0].type == "ColorIntensity":
            color = str(caps[0].data.get("color", "")) or None
        channels[name] = Channel(name=name, offset=offset, capabilities=caps,
                                 default_value=_default_value(cdef), color=color)
        order.append(name)

    for ch in channels.values():
        if any(o < 0 for o in ch.fine_offsets):
            raise ProfileError(f"{key}: channel {ch.name!r} lists a fine alias the mode does not use")

    manufacturer = key.split("/")[0]
    return Profile(key=key, name=str(fixture.get("name", key)), manufacturer=manufacturer,
                   mode=str(mode.get("name")), channels=[channels[n] for n in order],
                   categories=[str(c) for c in fixture.get("categories", [])])
