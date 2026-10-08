import pytest

from exactcut.errors import ValidationError
from exactcut.timecode import parse_range, parse_timepoint


@pytest.mark.parametrize(
    ("value", "expected"),
    [("15s", 15000), ("2m4s", 124000), ("00:55", 55000), ("01:19", 79000), ("1:02:03.5", 3723500)],
)
def test_parse_timepoint(value, expected):
    assert parse_timepoint(value) == expected


def test_parse_range():
    assert parse_range("55s-1m19s") == (55000, 79000)


def test_invalid_range():
    with pytest.raises(ValidationError):
        parse_range("2m-1m")

