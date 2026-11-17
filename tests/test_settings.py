import json

import pytest

from vproc.settings import OPTIONS, PresetStore, validate_values


def test_validation():
    assert validate_values({"target-peak": "120"}) == {"target-peak": 120}
    assert validate_values({"target-peak": "auto"}) == {"target-peak": "auto"}
    assert validate_values({"hdr-peak-percentile": 99.995}) == {"hdr-peak-percentile": 99.995}
    assert validate_values({"deband": "yes"}) == {"deband": True}
    assert validate_values({"tone-mapping": "bt.2390"}) == {"tone-mapping": "bt.2390"}
    for bad in ({"target-peak": 5}, {"tone-mapping": "st2084"}, {"tone-mapping-peak": 100},
                {"input-ipc-server": "/tmp/x"}, {"deband": "maybe"}, {"brightness": True}):
        with pytest.raises(ValueError):
            validate_values(bad)


def test_schema_is_serialisable():
    json.dumps([o.to_dict() for o in OPTIONS.values()])


def test_store_roundtrip_and_atomic_save(tmp_path):
    path = tmp_path / "presets.json"
    store = PresetStore(path)
    assert store.state.active == "SDR"
    store.create("Night", copy_from="HDR to SDR", values={"target-peak": 60})
    store.update("Night", {"tone-mapping": "bt.2390"})
    store.set_auto({"hdr10": "Night"})
    store.save()
    assert not list(tmp_path.glob(".presets-*"))

    again = PresetStore(path)
    assert again.values("Night")["target-peak"] == 60
    assert again.values("Night")["tone-mapping"] == "bt.2390"
    assert again.state.auto["hdr10"] == "Night"


def test_rename_and_delete_keep_references_consistent(tmp_path):
    store = PresetStore(tmp_path / "p.json")
    store.state.active = "HDR to SDR"
    store.rename("HDR to SDR", "Projector HDR")
    assert store.state.active == "Projector HDR"
    assert store.state.auto["hdr10"] == "Projector HDR"
    store.delete("Projector HDR")
    assert store.state.active == "SDR"
    assert store.state.auto["hdr10"] is None
    with pytest.raises(ValueError):
        store.delete("SDR")
    with pytest.raises(ValueError):
        store.create("bad/name")


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{not json")
    assert PresetStore(path).state.active == "SDR"
    path.write_text(json.dumps({"presets": {"A": {"target-peak": 1}, "B": {"scale": "spline36"}}, "active": "A"}))
    store = PresetStore(path)
    assert list(store.state.presets) == ["B"] and store.state.active == "B"
