from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_renderer_has_no_forbidden_ffmpeg_options():
    source = (ROOT / "exactcut" / "renderer.py").read_text(encoding="utf-8")
    assert "shortest=1" not in source
    assert '"-to"' not in source

