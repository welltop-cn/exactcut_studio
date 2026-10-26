from pathlib import Path

from PIL import Image

from exactcut.title_image import SCRIPT, build_prompt, cut_title, settings


def test_settings_follow_the_copied_image_script_and_runtime_defaults():
    values = settings({})
    assert values["image_model"] == "gemini-3.1-flash-image"
    assert values["retries"] == 5
    assert values["timeout_ms"] == 240000
    assert values["key_color"] == [0, 255, 0]
    assert SCRIPT.name == "generate_gemini_image.mjs"
    assert "gemini-3.1-flash-image" in SCRIPT.read_text(encoding="utf-8")


def test_prompt_keeps_the_title_and_limits_the_palette():
    prompt = build_prompt("The Alphas I Tamed\nWant Me in Bed")
    assert "The Alphas I Tamed\nWant Me in Bed" in prompt
    assert "RGB(0, 255, 0)" in prompt
    assert "gold serif title card" in prompt
    assert "No script" in prompt
    assert "magenta" in prompt


def test_cut_keeps_gold_and_drops_the_key_color_and_magenta_stroke(tmp_path: Path):
    source = tmp_path / "raw.png"
    Image.new("RGB", (5, 1), (0, 255, 0)).save(source)
    image = Image.open(source)
    image.putpixel((1, 0), (212, 175, 55))
    image.putpixel((2, 0), (245, 230, 200))
    image.putpixel((3, 0), (92, 58, 28))
    image.putpixel((4, 0), (190, 40, 180))
    image.save(source)
    plate = cut_title(source, tmp_path / "title.png")
    pixels = list(Image.open(plate).get_flattened_data())
    assert pixels[0][3] == 0
    assert pixels[1][3] == 255 and pixels[1][:3] == (212, 175, 55)
    assert pixels[2][3] == 255
    assert pixels[3][3] == 255
    assert pixels[4][3] == 0


def test_edge_white_is_removed_and_enclosed_white_stays(tmp_path: Path):
    source = tmp_path / "raw.png"
    image = Image.new("RGB", (5, 5), (255, 255, 255))
    for x in range(1, 4):
        image.putpixel((x, 1), (212, 175, 55))
        image.putpixel((x, 3), (212, 175, 55))
    image.putpixel((1, 2), (212, 175, 55))
    image.putpixel((3, 2), (212, 175, 55))
    image.save(source)
    pixels = list(Image.open(cut_title(source, tmp_path / "title.png")).get_flattened_data())
    assert pixels[0][3] == 0
    assert pixels[2 * 5 + 2][3] == 255
    assert pixels[2 * 5 + 2][:3] == (255, 255, 255)
