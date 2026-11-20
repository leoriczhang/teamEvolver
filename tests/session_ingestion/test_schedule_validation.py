"""Invalid schedule inputs are domain validation errors, never provider/server failures."""
import pytest

from session_ingestion.schedule import validate_schedule

VALID = {"enabled": True, "time": "02:00", "timezone": "Asia/Shanghai", "window": "previous_day"}


@pytest.mark.parametrize("value", [None, [], "daily", 1,
                                  {**VALID, "timezone": "Definitely/NotAZone"},
                                  {**VALID, "timezone": None}, {**VALID, "timezone": {}},
                                  {**VALID, "time": 2}, {**VALID, "time": []}, {**VALID, "time": "25:00"}])
def test_invalid_schedule_is_a_value_error(value):
    with pytest.raises(ValueError):
        validate_schedule(value)


def test_valid_schedule_remains_unchanged():
    assert validate_schedule(VALID) == VALID
