"""Map an OFL profile onto Wiren Board MQTT controls and render control values into DMX bytes.

The web UI of a controller knows a handful of control types (range, rgb, switch, pushbutton,
value, text), so the OFL capability model is reduced to three shapes:

* a channel with one capability over the whole range → one `range` control; Red/Green/Blue
  intensity channels of one pixel are folded into a single `rgb` control. Pan and Tilt keep the
  full 16-bit resolution of their fine channels, every other channel is driven as 0..255 and its
  fine byte is derived from that;
* a channel split into capability ranges → a `<name>_mode` enum control (a drop-down in the web
  UI) that selects the capability, plus a `<name>` 0..100 % range that positions the value inside
  the selected capability when at least one capability of the channel is continuous;
* everything else keeps its OFL name, so an exotic channel is still a labelled slider rather
  than "Channel 7".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .ofl import DMX_MAX, Capability, Channel, Profile

RGB_COLORS = ("Red", "Green", "Blue")
POSITION_TYPES = {"Pan", "Tilt"}          # capability types that keep their 16-bit resolution

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

# Russian wording for the capability labels ofl.Capability.label builds; applied word by word
# so "Strobe 1Hz→20Hz" becomes "Строб 1Hz→20Hz" and an unknown effect name stays as it is.
RU_LABEL_WORDS = {
    "Off": "Выкл", "Open": "Открыт", "Closed": "Закрыт", "Strobe": "Строб", "Pulse": "Пульсация",
    "RampUp": "Нарастание", "RampDown": "Спад", "Slot": "Слот", "Shake": "Тряска", "Rotation": "Вращение",
    "stop": "стоп", "slow": "медленно", "fast": "быстро", "Prism": "Призма", "Blackout": "Затемнение",
    "Color": "Цвет", "preset": "пресет", "temperature": "температура", "default": "по умолчанию",
    "cold": "холодный", "warm": "тёплый", "Program": "Программа", "Music": "Музыка", "controlled": "управл.",
    "Sound": "Звук", "Control": "управление", "Reset": "Сброс", "Generic": "Значение", "Effect": "Эффект",
    "Maintenance": "Сервис", "ColorJump": "Смена цветов", "ColorFade": "Перелив цветов",
    "Red": "Красный", "Green": "Зелёный", "Blue": "Синий", "White": "Белый", "Amber": "Янтарный",
    "Yellow": "Жёлтый", "Cyan": "Голубой", "Magenta": "Пурпурный", "Pink": "Розовый", "Turquoise": "Бирюзовый",
    "Lavender": "Лавандовый", "Mauve": "Лиловый", "Warm": "Тёплый", "Cold": "Холодный",
}

# Order of the controls in the device card: the ones a user reaches for first go on top, the rest
# keep the order of the DMX mode.
ORDER_GROUPS = {"Intensity": 0, "rgb": 1, "ColorIntensity": 2, "Pan": 3, "Tilt": 4}
ORDER_OTHER = 5

# Control kinds (how the value maps onto DMX).
KIND_DIRECT = "direct"       # range control written into one channel
KIND_RGB = "rgb"             # rgb control written into three channels
KIND_MODE = "mode"           # enum control selecting a capability of a split channel
KIND_LEVEL = "level"         # 0..100 % position inside the selected capability
LEVEL_MAX = 100


@dataclass
class Control:
    """One MQTT control of a fixture device and how it maps onto DMX."""

    id: str
    title_en: str
    title_ru: str
    type: str                        # range | rgb | value
    kind: str                        # KIND_*
    channels: list[Channel]          # channels the control writes (rgb: R, G, B in order)
    max: int = DMX_MAX
    shift: int = 0                   # bits the control value is shifted left to fill the fine bytes
    order: int = 0
    default: Any = 0
    enum: dict[str, dict[str, str]] = field(default_factory=dict)   # value -> {"en":..., "ru":...}


@dataclass
class FixtureMapping:
    profile: Profile
    controls: list[Control]
    # Control id -> current value (int for range/value, "R;G;B" for rgb).
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for c in self.controls:
            self.state.setdefault(c.id, c.default)

    def control(self, control_id: str) -> Optional[Control]:
        return next((c for c in self.controls if c.id == control_id), None)

    def set_value(self, control_id: str, raw: str) -> bool:
        """Apply a value written over MQTT. Returns False when the value is rejected."""
        c = self.control(control_id)
        if c is None:
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
        return True

    def render(self) -> bytearray:
        """DMX bytes of the fixture, channel defaults for anything no control writes."""
        out = bytearray(self.profile.footprint)
        for ch in self.profile.channels:
            _write_channel(out, ch, ch.default_value << (ch.resolution_bits - 8))
        for c in self.controls:
            if c.kind == KIND_DIRECT:
                _write_channel(out, c.channels[0], int(self.state[c.id]) << c.shift)
            elif c.kind == KIND_RGB:
                parts = [int(p) for p in str(self.state[c.id]).split(";")]
                for ch, v in zip(c.channels, parts):
                    _write_channel(out, ch, v << (ch.resolution_bits - 8))
            elif c.kind == KIND_MODE:
                ch = c.channels[0]
                mode = int(self.state[c.id])
                cap = ch.capabilities[max(0, min(len(ch.capabilities) - 1, mode))]
                level_id = c.id[: -len("_mode")]
                level = int(self.state.get(level_id, 0)) if level_id in self.state else 0
                if cap.is_continuous:
                    value = cap.dmx_start + round((cap.dmx_end - cap.dmx_start) * level / LEVEL_MAX)
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


def label_ru(label: str) -> str:
    """Russian version of a capability label, word by word; unknown words are kept."""
    return " ".join(RU_LABEL_WORDS.get(word, word) for word in label.split(" "))


def capability_enum(caps: list[Capability]) -> dict[str, dict[str, str]]:
    """Enum meta of a mode control: capability index -> localized label, duplicates numbered."""
    out: dict[str, dict[str, str]] = {}
    seen: dict[str, int] = {}
    for i, cap in enumerate(caps):
        label = cap.label
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} ({seen[label]})"
        out[str(i)] = {"en": label, "ru": label_ru(label)}
    return out


def _group(ch: Channel, kind: str) -> int:
    if kind == KIND_RGB:
        return ORDER_GROUPS["rgb"]
    single = ch.single
    return ORDER_GROUPS.get(single.type, ORDER_OTHER) if single is not None else ORDER_OTHER


def build_mapping(profile: Profile) -> FixtureMapping:
    controls: list[Control] = []
    taken: set[str] = set()

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
            default = ";".join(str(c.default_value) for c in chans)
            controls.append(Control(control_id(title, taken), title, _ru("Color") + (f" {key}" if key else ""),
                                    "rgb", KIND_RGB, chans, default=default))

    for ch in profile.channels:
        if ch.name in folded:
            continue
        single = ch.single
        if single is not None:
            if single.type == "NoFunction":
                continue
            if single.type in POSITION_TYPES:
                shift, maximum, default = 0, ch.max_value, ch.default_value << (ch.resolution_bits - 8)
            else:
                shift, maximum, default = ch.resolution_bits - 8, DMX_MAX, ch.default_value
            controls.append(Control(control_id(ch.name, taken), ch.name, _ru(ch.name), "range", KIND_DIRECT, [ch],
                                    max=maximum, shift=shift, default=default))
            continue
        # Capability ranges: mode selector + level.
        caps = ch.capabilities
        default_mode = next((i for i, c in enumerate(caps) if c.dmx_start <= ch.default_value <= c.dmx_end), 0)
        base_id = control_id(ch.name, taken)
        taken.add(f"{base_id}_mode")
        controls.append(Control(f"{base_id}_mode", ch.name, _ru(ch.name), "value", KIND_MODE, [ch],
                                max=len(caps) - 1, default=default_mode, enum=capability_enum(caps)))
        if any(c.is_continuous for c in caps):
            cap = caps[default_mode]
            span = cap.dmx_end - cap.dmx_start
            level = round((ch.default_value - cap.dmx_start) * LEVEL_MAX / span) if span else 0
            controls.append(Control(base_id, f"{ch.name} level", f"{_ru(ch.name)}: уровень", "range", KIND_LEVEL,
                                    [ch], max=LEVEL_MAX, default=max(0, min(LEVEL_MAX, level))))

    # Stable sort: priority group first, DMX channel order inside a group.
    controls.sort(key=lambda c: (_group(c.channels[0], c.kind), c.channels[0].offset, c.kind == KIND_LEVEL))
    for i, c in enumerate(controls, start=1):
        c.order = i
    return FixtureMapping(profile, controls)
