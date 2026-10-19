"""Check Tela-owned JSON schemas against actual token/profile validation."""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from tela.core.models import CapabilityToken
from tela.shell.builtin_tools import _validate_profile_list_payload


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def _validator(filename: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


TOKEN = {
    "token_id": "tok_test",
    "profile_id": "dev",
    "persona_ref": "persona.dev",
    "instance_id": "inst-1",
    "issued_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-12-31T23:59:59Z",
    "token_version": "0.1.0",
    "signature": "test-signature",
}


@pytest.mark.parametrize(
    ("updates", "removed", "valid"),
    [
        ({}, None, True),
        ({"max_depth": 0}, None, True),
        ({"max_depth": 3}, None, True),
        ({}, "profile_id", False),
        ({}, "signature", False),
        ({"profile_name": "dev"}, None, False),
        ({"extra": True}, None, False),
        ({"profile_id": None}, None, False),
        ({"profile_id": 7}, None, False),
        ({"token_id": "bad"}, None, False),
        ({"token_version": "99"}, None, False),
        ({"max_depth": -1}, None, False),
        ({"max_depth": True}, None, False),
        ({"max_depth": "1"}, None, False),
        ({"issued_at": "not-a-date"}, None, False),
    ],
)
def test_token_schema_and_runtime_agree(updates, removed, valid: bool) -> None:
    payload = {**TOKEN, **updates}
    if removed is not None:
        del payload[removed]
    assert _validator("capability_token.schema.json").is_valid(payload) is valid
    if valid:
        token = CapabilityToken.model_validate(payload)
        assert token.profile_id == "dev"
        _validator("capability_token.schema.json").validate(
            token.model_dump(exclude_none=True)
        )
    else:
        with pytest.raises(ValueError):
            CapabilityToken.model_validate(payload)


@pytest.mark.parametrize(
    ("updates", "removed", "valid"),
    [
        ({}, None, True),
        ({"capabilities": {}}, None, True),
        ({"capabilities": {"fs": "destructive"}}, None, True),
        ({}, "profile_id", False),
        ({}, "capabilities", False),
        ({}, "default", False),
        ({"profile_name": "dev"}, None, False),
        ({"profile_id": None}, None, False),
        ({"default": "true"}, None, False),
        ({"capabilities": {"fs": "write"}}, None, False),
        ({"capabilities": []}, None, False),
    ],
)
def test_profile_list_schema_and_runtime_agree(updates, removed, valid: bool) -> None:
    entry = {"profile_id": "dev", "capabilities": {"fs": "read_only"}, "default": True}
    entry.update(deepcopy(updates))
    if removed is not None:
        del entry[removed]
    payload = [entry]
    assert _validator("tela_profile_list.schema.json").is_valid(payload) is valid
    if valid:
        assert _validate_profile_list_payload(payload) == payload
    else:
        with pytest.raises(RuntimeError):
            _validate_profile_list_payload(payload)


def test_profile_list_allows_empty_registry_without_default() -> None:
    _validator("tela_profile_list.schema.json").validate([])
    assert _validate_profile_list_payload([]) == []
