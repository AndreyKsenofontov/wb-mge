from pathlib import Path

import pytest

from wb.dmx_fixtures import library, ofl
from wb.dmx_fixtures.mapping import build_mapping

OFL_DIR = Path(__file__).parent / "ofl"


def profile(key: str, mode: str):
    fixture = ofl.load_fixture(OFL_DIR / f"{key}.json")
    return ofl.build_profile(fixture, key, mode)


def test_rgb_par_folds_colors_into_one_rgb_control():
    m = build_mapping(profile("cameo/flat-par-can-tri-7x-3w-ir", "6-channel"))
    ids = [c.id for c in m.controls]
    assert "color" in ids
    color = m.control("color")
    assert color.type == "rgb" and [ch.name for ch in color.channels] == ["Red", "Green", "Blue"]
    assert m.control("master_dimmer").type == "range" and m.control("master_dimmer").max == 255
    # Red/Green/Blue must not also appear as separate sliders.
    assert not {"red", "green", "blue"} & set(ids)


def test_rgb_par_renders_color_and_dimmer():
    m = build_mapping(profile("cameo/flat-par-can-tri-7x-3w-ir", "6-channel"))
    assert m.set_value("color", "255;128;0")
    assert m.set_value("master_dimmer", "200")
    data = list(m.render())
    assert len(data) == 6
    assert data[0] == 200                      # Master Dimmer is channel 1 of the mode
    assert data[2:5] == [255, 128, 0]          # Red, Green, Blue are channels 3..5


def test_split_channel_gets_enum_mode_and_level_controls():
    m = build_mapping(profile("cameo/q-spot-40-rgbw", "9-channel"))
    strobe = m.control("strobe_functions_mode")
    assert strobe is not None and strobe.type == "value"
    assert strobe.enum["0"] == {"en": "Open", "ru": "Открыт"}
    assert strobe.enum["7"]["en"] == "Strobe 1Hz→20Hz"
    assert strobe.enum["8"]["en"] == "Open (2)"            # the second "Open" range is told apart
    assert m.control("strobe_functions").max == 100
    assert m.control("strobe_functions_mode_name") is None
    # Pick the first continuous capability and position inside it.
    ch = strobe.channels[0]
    cont = next(i for i, c in enumerate(ch.capabilities) if c.is_continuous)
    assert m.set_value("strobe_functions_mode", str(cont))
    assert m.set_value("strobe_functions", "50")
    cap = ch.capabilities[cont]
    expected = cap.dmx_start + round((cap.dmx_end - cap.dmx_start) * 0.5)
    assert m.render()[ch.offset] == expected


def test_discrete_only_channel_has_no_level_control():
    m = build_mapping(profile("stairville/mh-x50", "14ch"))
    programs = m.control("built_in_programs_mode")
    assert programs.type == "value" and programs.enum["1"]["en"] == "Program 1"
    assert m.control("built_in_programs") is None
    assert m.control("special_functions") is None
    assert m.set_value("built_in_programs_mode", "1")
    assert m.render()[programs.channels[0].offset] == 8     # start of the "Program 1" range


def test_controls_are_ordered_dimmer_color_position_first():
    m = build_mapping(profile("stairville/mh-x50", "14ch"))
    ids = [c.id for c in m.controls]
    assert ids[:3] == ["dimmer", "pan", "tilt"]
    assert ids[3:] == ["speed", "color_wheel_mode", "color_wheel", "shutter_mode", "shutter", "gobo_wheel_mode",
                       "gobo_wheel", "gobo_rotation_mode", "gobo_rotation", "special_functions_mode",
                       "built_in_programs_mode", "prism_mode", "prism", "focus"]
    assert [c.order for c in m.controls] == list(range(1, len(ids) + 1))
    par = build_mapping(profile("cameo/q-spot-40-rgbw", "9-channel"))
    assert [c.id for c in par.controls][:3] == ["dimmer", "color", "white"]


def test_pan_tilt_keep_16_bit_resolution():
    m = build_mapping(profile("stairville/mh-x50", "14ch"))
    pan = m.control("pan")
    assert pan.max == 65535
    assert not m.set_value("pan", "0x1234")                 # non-numeric strings are rejected
    assert m.set_value("pan", str(0x1234))
    data = m.render()
    ch = pan.channels[0]
    assert data[ch.offset] == 0x12 and data[ch.fine_offsets[0]] == 0x34
    dimmer = m.control("dimmer")
    assert dimmer.max == 255 and dimmer.channels[0].offset == 7


def test_16_bit_dimmer_is_driven_as_8_bit():
    m = build_mapping(profile("cameo/q-spot-40-rgbw", "9-channel"))
    dimmer = m.control("dimmer")
    assert dimmer.max == 255 and dimmer.channels[0].fine_offsets == [1]
    assert m.set_value("dimmer", "200")
    data = m.render()
    assert data[0] == 200 and data[1] == 0


def test_values_are_clamped_and_bad_input_rejected():
    m = build_mapping(profile("cameo/q-spot-40-rgbw", "4-channel"))
    assert m.set_value("color", "300;-5;12")
    assert m.state["color"] == "255;0;12"
    assert not m.set_value("color", "1;2")
    assert not m.set_value("white", "abc")
    assert m.set_value("white", "999") and m.state["white"] == 255
    assert not m.set_value("no_such_control", "1")


def test_footprint_and_defaults():
    p = profile("cameo/q-spot-40-rgbw", "9-channel")
    assert p.footprint == 9
    m = build_mapping(p)
    assert len(m.render()) == 9


def test_unknown_mode_raises():
    fixture = ofl.load_fixture(OFL_DIR / "stairville/mh-x50.json")
    with pytest.raises(ofl.ProfileError):
        ofl.build_profile(fixture, "stairville/mh-x50", "99ch")


def test_schema_offers_library_profiles_with_their_modes():
    profiles = library.list_profiles(OFL_DIR)
    assert set(profiles) == {"cameo/flat-par-can-tri-7x-3w-ir", "cameo/q-spot-40-rgbw", "stairville/mh-x50"}
    schema = library.build_schema(profiles, "/etc/x.conf", "x")
    branches = schema["properties"]["fixtures"]["items"]["oneOf"]
    assert len(branches) == 3
    mh = next(b for b in branches if b["properties"]["fixture"]["enum"] == ["stairville/mh-x50"])
    assert mh["properties"]["mode"]["enum"] == ["8ch", "14ch"]
    assert schema["configFile"]["path"] == "/etc/x.conf"
