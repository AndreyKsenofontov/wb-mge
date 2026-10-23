"""Map an OFL profile onto Wiren Board MQTT controls and render control values into DMX bytes.

The web UI of a controller knows a handful of control types (range, rgb, switch, pushbutton,
value, text), so the OFL capability model is reduced to three shapes:

* a channel with one capability over the whole range → one `range` control (16-bit when the
  mode carries fine channels); Red/Green/Blue intensity channels of one pixel are folded into a
  single `rgb` control;
* a channel split into capability ranges → a `<name>_mode` range that selects the capability,
  a read-only `<name>_mode_name` text echoing its label, and a `<name>` 0..100 % range that
  positions the value inside the selected capability when that capability is continuous;
* everything else keeps its OFL name, so an exotic channel is still a labelled slider rather
  than "Channel 7".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .ofl import DMX_MAX, Channel, Profile

RGB_COLORS = ("Red", "Green", "Blue")

# Russian titles for the channel names OFL uses most; anything else keeps its English name.
RU_TITLES = {
    "Dimmer": "Диммер", "Master Dimmer": "Общий диммер", "Intensity": "Яркость", "Color": "Цвет",
    "Pan": "Поворот (pan)", "Tilt": "Наклон (tilt)", "Shutter": "Затвор", "Strobe": "Строб",
    "Shutter / Strobe": "Затвор / строб", "Strobe functions": "Режимы строба", "Focus": "Фокус",
    "Zoom": "Зум", "Speed": "Скорость", "Pan/Tilt Speed": "Скорость pan/tilt", "Prism": "Призма",
    "Color Wheel": "Колесо цветов", "Gobo Wheel": "Колесо гобо", "Gobo Rotation": "Вращение гобо",
    "Color Macro": "Цветовые макросы", "Color Macros": "Цветовые макросы",
    "Color Macro Advanced": "Цветовые макросы (расш.)", "Color Temperature": "Цветовая температура",
    "Special Functions": "Служебные функции", "Built-in Programs": "Встроенные программы",
    "White": "Белый", "Amber": "Янтарный", "UV": "УФ", "Cyan": "Голубой", "Magenta": "Пурпурный",
    "Yellow": "Жёлтый", "Lime": "Лаймовый", "Indigo": "Индиго", "Warm White": "Тёплый белый",
    "Cold White": "Холодный белый", "Iris": "Ирис", "Frost": "Фрост", "Fog": "Дым",
}


@dataclass
class Control:
    """One MQTT control of a fixture device and how it maps onto DMX."""

    id: str
    title_en: str
    title_ru: str
    type: str                        # range | rgb | text
    kind: str                        # direct | rgb | mode | mode_name | level
    channels: list[Channel]          # channels the control writes (rgb: R, G, B in order)
    max: int = DMX_MAX
    readonly: bool = False
    order: int = 0
    default: Any = 0


@dataclass
class FixtureMapping:
    profile: Profile
    controls: list[Control]
    # Control id -> current value (int for range, "R;G;B" for rgb, str for text).
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for c in self.controls:
            self.state.setdefault(c.id, c.default)

    def control(self, control_id: str) -> Optional[Control]:
        return next((c for c in self.controls if c.id == control_id), None)

    def set_value(self, control_id: str, raw: str) -> bool:
        """Apply a value written over MQTT. Returns False when the value is rejected."""
        c = self.control(control_id)
        if c is None or c.readonly:
            return False
        if c.type == "rgb":
            parts = raw.split(";")
            if len(parts) != 3:
                return False
            try:
                rgb = [max(0, min(DMX_MAX, int(float(p)))) for p in parts]
            except ValueError:
                return False
            self.state[c.id] = ";".join(str(v) for v in rgb)
        else:
            try:
                value = int(float(raw))
            except ValueError:
                return False
            self.state[c.id] = max(0, min(c.max, value))
        self._refresh_mode_names()
        return True

    def _refresh_mode_names(self) -> None:
        for c in self.controls:
            if c.kind == "mode_name":
                mode = int(self.state.get(c.id[: -len("_name")], 0))
                caps = c.channels[0].capabilities
                self.state[c.id] = caps[mode].label if 0 <= mode < len(caps) else ""

    def render(self) -> bytearray:
        """DMX bytes of the fixture, channel defaults for anything no control writes."""
        out = bytearray(self.profile.footprint)
        for ch in self.profile.channels:
            _write_channel(out, ch, ch.default_value << (ch.resolution_bits - 8))
        for c in self.controls:
            if c.kind == "direct":
                _write_channel(out, c.channels[0], int(self.state[c.id]))
            elif c.kind == "rgb":
                parts = [int(p) for p in str(self.state[c.id]).split(";")]
                for ch, v in zip(c.channels, parts):
                    _write_channel(out, ch, v << (ch.resolution_bits - 8))
            elif c.kind == "mode":
                ch = c.channels[0]
                mode = int(self.state[c.id])
                cap = ch.capabilities[max(0, min(len(ch.capabilities) - 1, mode))]
                level_id = c.id[: -len("_mode")]
                level = int(self.state.get(level_id, 0)) if level_id in self.state else 0
                if cap.is_continuous:
                    value = cap.dmx_start + round((cap.dmx_end - cap.dmx_start) * level / 100)
                else:
                    value = cap.dmx_start
                _write_channel(out, ch, value << (ch.resolution_bits - 8))
        return out


def _write_channel(out: bytearray, ch: Channel, value: int) -> None:
    """Spread a value of ch.resolution_bits over the coarse and fine bytes."""
    value = max(0, min(ch.max_value, value))
    offsets = [ch.offset] + ch.fine_offsets
    for i, off in enumerate(offsets):
        shift = 8 * (len(offsets) - 1 - i)
        if 0 <= off < len(out):
            out[off] = (value >> shift) & 0xFF


def control_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "ch"
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}_{n}", n + 1
    taken.add(cid)
    return cid


def _ru(name: str) -> str:
    return RU_TITLES.get(name, name)


def build_mapping(profile: Profile) -> FixtureMapping:
    controls: list[Control] = []
    taken: set[str] = set()
    order = 0

    # Fold R/G/B intensity channels of one pixel into an rgb control. The pixel key is the channel
    # name with the color word removed, so "Red", "Green", "Blue" share "" and "Red 1".. share "1".
    rgb_groups: dict[str, dict[str, Channel]] = {}
    for ch in profile.channels:
        if ch.color in RGB_COLORS and ch.single is not None:
            key = re.sub(r"\s+", " ", ch.name.replace(ch.color, "")).strip()
            rgb_groups.setdefault(key, {})[ch.color] = ch
    folded: set[str] = set()
    for key, group in rgb_groups.items():
        if all(c in group for c in RGB_COLORS):
            chans = [group[c] for c in RGB_COLORS]
            folded.update(c.name for c in chans)
            title = "Color" if not key else f"Color {key}"
            order += 1
            default = ";".join(str(c.default_value) for c in chans)
            controls.append(Control(control_id(title, taken), title, _ru("Color") + (f" {key}" if key else ""),
                                    "rgb", "rgb", chans, order=order, default=default))

    for ch in profile.channels:
        if ch.name in folded:
            continue
        single = ch.single
        if single is not None:
            if single.type == "NoFunction":
                continue
            order += 1
            default = ch.default_value << (ch.resolution_bits - 8)
            controls.append(Control(control_id(ch.name, taken), ch.name, _ru(ch.name), "range", "direct", [ch],
                                    max=ch.max_value, order=order, default=default))
            continue
        # Capability ranges: mode selector + label + level.
        caps = ch.capabilities
        default_mode = next((i for i, c in enumerate(caps) if c.dmx_start <= ch.default_value <= c.dmx_end), 0)
        base_id = control_id(ch.name, taken)
        order += 1
        controls.append(Control(f"{base_id}_mode", f"{ch.name} mode", f"{_ru(ch.name)}: режим", "range", "mode", [ch],
                                max=len(caps) - 1, order=order, default=default_mode))
        taken.add(f"{base_id}_mode")
        order += 1
        controls.append(Control(f"{base_id}_mode_name", f"{ch.name} mode name", f"{_ru(ch.name)}: выбрано", "text",
                                "mode_name", [ch], readonly=True, order=order, default=caps[default_mode].label))
        taken.add(f"{base_id}_mode_name")
        if any(c.is_continuous for c in caps):
            order += 1
            cap = caps[default_mode]
            span = cap.dmx_end - cap.dmx_start
            level = round((ch.default_value - cap.dmx_start) * 100 / span) if span else 0
            controls.append(Control(base_id, f"{ch.name} level", f"{_ru(ch.name)}: уровень", "range", "level", [ch],
                                    max=100, order=order, default=max(0, min(100, level))))
    return FixtureMapping(profile, controls)
