from exactcut.naming import validate_material_filename


def test_fbbm_filename_regex():
    parsed = validate_material_filename("20260902_typezxraw_张三_AGENT_12_#101460#.mp4")
    assert parsed["date"] == "20260902"
    assert parsed["novel_id"] == "101460"
    assert parsed["seq"] == "12"

