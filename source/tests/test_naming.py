import pytest

from exactcut.errors import ValidationError
from exactcut.naming import build_material_filename, validate_material_filename


def test_author_is_creator1():
    filename = build_material_filename("20260902", "typezxraw", "张三", "AGENT", 1, "101460")
    assert filename == "20260902_typezxraw_张三_agent_1_#101460#.mp4"
    assert validate_material_filename(filename)["creator1"] == "张三"


def test_author_cannot_contain_underscore():
    with pytest.raises(ValidationError):
        build_material_filename("20260902", "typezxraw", "张_三", "AGENT", 1, "101460")

