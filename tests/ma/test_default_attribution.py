"""An MA connection can retain unidentified Sessions without claiming an owner."""
import pytest

from team_ma.schemas import validate_config

BASE = {"display_name": "MA", "entry_agent_id": "agent", "environment_id": "env",
        "ma_secret_ref": "secret-one", "tenant_token_ref": "secret-two"}


@pytest.mark.parametrize("raw", [BASE, {**BASE, "default_user_id": ""}, {**BASE, "default_user_id": "  "}])
def test_connection_allows_no_default_owner(raw):
    assert validate_config(raw)["default_user_id"] == ""


def test_explicit_default_is_preserved():
    assert validate_config({**BASE, "default_user_id": "employee42"})["default_user_id"] == "employee42"


@pytest.mark.parametrize("value", [None, [], 42])
def test_invalid_default_types_are_rejected(value):
    with pytest.raises(ValueError, match="default_user_id"):
        validate_config({**BASE, "default_user_id": value})
