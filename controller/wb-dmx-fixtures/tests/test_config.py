import json
import shutil
from pathlib import Path

import pytest

from wb.dmx_fixtures import library
from wb.dmx_fixtures.config import ConfigError, address_map, load_setup, member_id
from wb.dmx_fixtures.modbus_tcp import merge_ranges

OFL_DIR = Path(__file__).parent / "ofl"
PROFILES = library.list_profiles(OFL_DIR)
QSPOT = "cameo/q-spot-40-rgbw"        # 9 channels in "9-channel"
HEAD = "stairville/mh-x50"            # 14 channels in "14ch"


def entry(id_, fixture=QSPOT, mode="9-channel", **kw):
    return {"id": id_, "fixture": fixture, "mode": mode, **kw}


def test_single_entry_keeps_its_id_and_has_no_group():
    setup = load_setup({"fixtures": [entry("par1", address=1)]}, PROFILES)
    assert [f.id for f in setup.fixtures] == ["par1"]
    assert setup.groups == []


def test_count_makes_consecutive_fixtures_and_a_group():
    setup = load_setup({"fixtures": [entry("par", name="Par", count=4, address=10)]}, PROFILES)
    assert [(f.id, f.name, f.address) for f in setup.fixtures] == [
        ("par1", "Par 1", 10), ("par2", "Par 2", 19), ("par3", "Par 3", 28), ("par4", "Par 4", 37)]
    (group,) = setup.groups
    assert group.device_id == "dmx_par" and group.address == 10 and group.last_address == 45
    assert all(f.group == "par" for f in setup.fixtures)
    # Members and the group share control ids, so a group write maps onto every member.
    assert [c.id for c in group.mapping.controls] == [c.id for c in setup.fixtures[0].mapping.controls]


def test_auto_address_follows_the_previous_entry():
    config = {"fixtures": [entry("par", count=3), entry("head", fixture=HEAD, mode="14ch", address=0),
                           entry("spare", address=100), entry("tail")]}
    setup = load_setup(config, PROFILES)
    assert [(f.id, f.address) for f in setup.fixtures] == [
        ("par1", 1), ("par2", 10), ("par3", 19), ("head", 28), ("spare", 100), ("tail", 109)]
    assert address_map(setup) == "par1..par3 DMX 1-27; head DMX 28-41; spare DMX 100-108; tail DMX 109-117"
    single_first = load_setup({"fixtures": [entry("solo", address=1), entry("par", count=2)]}, PROFILES)
    assert address_map(single_first) == "solo DMX 1-9; par1..par2 DMX 10-27"


def test_member_ids_of_an_id_ending_in_a_digit_are_separated():
    assert member_id("par", 2) == "par2"
    assert member_id("row1", 2) == "row1_2"


def test_overlap_is_reported_with_both_ranges():
    config = {"fixtures": [entry("par", count=2, address=1), entry("head", fixture=HEAD, mode="14ch", address=10)]}
    with pytest.raises(ConfigError, match=r"'par2' \(10\.\.18\) and 'head' \(10\.\.23\) overlap"):
        load_setup(config, PROFILES)


def test_universe_overflow_and_duplicate_ids_are_rejected():
    with pytest.raises(ConfigError, match="past 512"):
        load_setup({"fixtures": [entry("par", count=57)]}, PROFILES)
    with pytest.raises(ConfigError, match="repeated: par1"):
        load_setup({"fixtures": [entry("par", count=2), entry("par1")]}, PROFILES)


def test_missing_profile_mentions_the_download_error():
    with pytest.raises(ConfigError, match="not in the library: download failed: HTTP 404"):
        load_setup({"fixtures": [entry("x", fixture="acme/nope")]}, PROFILES,
                   {"acme/nope": "download failed: HTTP 404"})


@pytest.mark.parametrize("ref, key, url_tail", [
    ("cameo/q-spot-40-rgbw", "cameo/q-spot-40-rgbw", "/fixtures/cameo/q-spot-40-rgbw.json"),
    ("https://open-fixture-library.org/cameo/q-spot-40-rgbw", "cameo/q-spot-40-rgbw",
     "/fixtures/cameo/q-spot-40-rgbw.json"),
    (" https://open-fixture-library.org/eurolite/led-par-56-tcl/ ", "eurolite/led-par-56-tcl",
     "/fixtures/eurolite/led-par-56-tcl.json"),
    ("https://github.com/OpenLightingProject/open-fixture-library/blob/master/fixtures/stairville/mh-x50.json",
     "stairville/mh-x50", "/fixtures/stairville/mh-x50.json"),
    ("https://example.com/files/My Spot_v2.json", "custom/my-spot-v2", "https://example.com/files/My Spot_v2.json"),
])
def test_model_links_resolve_to_library_keys(ref, key, url_tail):
    parsed = library.parse_model_ref(ref)
    assert parsed.key == key and parsed.url.endswith(url_tail)


@pytest.mark.parametrize("ref", ["q-spot", "ftp://x/y.json", "https://open-fixture-library.org/cameo"])
def test_bad_model_links_are_rejected(ref):
    with pytest.raises(library.ModelError):
        library.parse_model_ref(ref)


def test_ensure_models_downloads_only_missing_and_classifies_errors(tmp_path):
    shutil.copytree(OFL_DIR / "cameo", tmp_path / "cameo")
    mh_json = (OFL_DIR / "stairville/mh-x50.json").read_bytes()
    calls = []

    def fetch(url):
        calls.append(url)
        if "mh-x50" in url:
            return mh_json
        if "broken" in url:
            return b"<html>not json</html>"
        if "offline" in url:
            raise library.ModelError("download failed: timed out", transient=True)
        raise library.ModelError("download failed: HTTP 404")

    result = library.ensure_models([QSPOT, "https://open-fixture-library.org/stairville/mh-x50", "acme/missing",
                                    "acme/broken", "acme/offline", "", "not a link"], tmp_path, fetch)
    assert result.imported == ["stairville/mh-x50"]
    assert (tmp_path / "stairville/mh-x50.json").read_bytes() == mh_json
    assert not any("q-spot" in u for u in calls)                 # already in the library
    assert set(result.errors) == {"acme/missing", "acme/broken", "acme/offline", "not a link"}
    assert result.errors["acme/offline"].transient and not result.errors["acme/missing"].transient
    assert "not a JSON file" in str(result.errors["acme/broken"])


def test_a_json_that_is_not_a_fixture_is_not_stored(tmp_path):
    def fetch(_url):
        return json.dumps({"name": "x"}).encode()
    result = library.ensure_models(["acme/thing"], tmp_path, fetch)
    assert "not an OFL fixture" in str(result.errors["acme/thing"])
    assert not (tmp_path / "acme/thing.json").exists()


def test_schema_has_models_count_and_auto_address():
    schema = library.build_schema(PROFILES, "/etc/x.conf", "x")
    assert schema["properties"]["models"]["items"]["type"] == "string"
    branch = schema["properties"]["fixtures"]["items"]["oneOf"][0]["properties"]
    assert branch["count"]["default"] == 1 and branch["address"]["default"] == 0 and branch["address"]["minimum"] == 0


def test_config_without_count_and_address_is_valid_for_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = library.build_schema(PROFILES, "/etc/x.conf", "x")
    config = {"gateway": {"host": "10.0.0.2"},
              "fixtures": [{"id": "par1", "fixture": QSPOT, "mode": "9-channel", "address": 1},
                           {"id": "par", "fixture": QSPOT, "mode": "9-channel"}]}
    jsonschema.validate(config, schema)
    with pytest.raises(jsonschema.ValidationError):              # an empty key matches no branch
        jsonschema.validate({**config, "fixtures": [{"id": "x", "fixture": "", "mode": "9-channel"}]}, schema)


def test_write_schema_skips_an_unchanged_file(tmp_path):
    schema = library.build_schema(PROFILES, "/etc/x.conf", "x")
    path = tmp_path / "s.json"
    assert library.write_schema(schema, path) is True
    assert library.write_schema(schema, path) is False


def test_adjacent_blocks_are_merged_into_one_write():
    blocks = [(19, b"\x03"), (1, b"\x01\x02"), (3, b"\x09"), (10, b"\x05")]
    assert merge_ranges(blocks) == [(1, b"\x01\x02\x09"), (10, b"\x05"), (19, b"\x03")]
