"""The stored input choice."""

import pytest

from vproc.inputs import InputChoice, matches, spec_for

QUAD_2 = {"index": 1, "name": "DeckLink Quad HDMI Recorder (2)", "persistent_id": 3255474417}


def test_default_until_chosen_and_back(tmp_path):
    choice = InputChoice(tmp_path / "input.json", default="0")
    assert choice.device == "0"
    choice.set("id:3255474417")
    assert InputChoice(tmp_path / "input.json", default="0").device == "id:3255474417"
    choice.set(None)
    assert InputChoice(tmp_path / "input.json", default="0").device == "0"


@pytest.mark.parametrize("bad", ["", "Quad", "id:", "id:12a", "1; rm"])
def test_rejects_anything_but_an_index_or_id(tmp_path, bad):
    with pytest.raises(ValueError):
        InputChoice(tmp_path / "input.json", default="0").set(bad)


def test_ignores_a_damaged_file(tmp_path):
    (tmp_path / "input.json").write_text('{"device": "--list"}')
    assert InputChoice(tmp_path / "input.json", default="0").device == "0"
    (tmp_path / "input.json").write_text("not json")
    assert InputChoice(tmp_path / "input.json", default="0").device == "0"


def test_selectors():
    assert spec_for(QUAD_2) == "id:3255474417"
    assert spec_for({"index": 3, "persistent_id": -1}) == "3"
    assert matches("id:3255474417", QUAD_2) and matches("1", QUAD_2)
    assert not matches("id:1", QUAD_2) and not matches("Quad", QUAD_2)
