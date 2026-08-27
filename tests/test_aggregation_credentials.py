from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from teamEvolver.aggregation.service import MemoryAggregationService
from teamEvolver.aggregation.sources import (
    AccountSourceBuilder,
    AccountUserCredential,
    SourceExpansionError,
)
from teamEvolver.config import TeamEvolverConfig


class _FakeResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict:
        return {
            "status": "ok",
            "result": [
                {
                    "user_id": "admin",
                    "role": "admin",
                    "api_key": "admin-secret",
                },
                {
                    "user_id": "alice",
                    "role": "user",
                    "api_key": "alice-secret",
                },
                {
                    "user_id": "team",
                    "role": "user",
                    "api_key": "team-secret",
                },
            ],
        }


def _service(tmp_path) -> MemoryAggregationService:
    return MemoryAggregationService(
        TeamEvolverConfig(
            aggregation_state_dir=str(tmp_path),
            sharing_viking_endpoint="https://openviking.example",
            sharing_viking_account="default",
            sharing_viking_user="team",
        )
    )


def test_admin_user_inventory_reads_plaintext_keys_without_exposing_repr(
    monkeypatch,
) -> None:
    captured = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, params, headers):
            captured.update(url=url, params=params, headers=headers)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    builder = AccountSourceBuilder(
        endpoint="https://openviking.example",
        api_key="admin-secret",
        account_id="default",
        excluded_user_ids=frozenset({"team"}),
    )

    records = asyncio.run(builder.list_account_user_credentials())

    assert [record.user_id for record in records] == ["admin", "alice", "team"]
    assert records[1].api_key == "alice-secret"
    assert "alice-secret" not in repr(records[1])
    assert captured["params"] == {"limit": 1_000, "offset": 0}
    assert captured["headers"]["X-API-Key"] == "admin-secret"


def test_admin_user_inventory_paginates_beyond_ten_thousand(monkeypatch) -> None:
    total_users = 10_005
    offsets: list[int] = []

    class PageResponse:
        status_code = 200
        text = ""

        def __init__(self, users: list[dict]) -> None:
            self._users = users

        def json(self) -> dict:
            return {"status": "ok", "result": self._users}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, _url, *, params, headers):
            del headers
            offset = int(params["offset"])
            limit = int(params["limit"])
            offsets.append(offset)
            end = min(total_users, offset + limit)
            return PageResponse(
                [
                    {
                        "user_id": f"user-{index:05d}",
                        "role": "user",
                        "api_key": f"key-{index:05d}",
                    }
                    for index in range(offset, end)
                ]
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    builder = AccountSourceBuilder(
        endpoint="https://openviking.example",
        api_key="admin-secret",
        account_id="default",
        account_user_limit=20_000,
        account_user_page_size=1_000,
    )

    records = asyncio.run(builder.list_account_user_credentials())

    assert len(records) == total_users
    assert records[0].user_id == "user-00000"
    assert records[-1].user_id == "user-10004"
    assert offsets == list(range(0, 11_000, 1_000))


def test_api_key_mode_assigns_existing_user_keys_and_admin_merge_identity(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    run = service.new_run(
        "default",
        auth_mode="api_key",
        endpoint="https://openviking.example",
    )
    records = [
        AccountUserCredential(
            user_id="admin",
            role="admin",
            api_key="admin-secret",
        ),
        AccountUserCredential(
            user_id="alice",
            role="user",
            api_key="alice-secret",
        ),
        AccountUserCredential(
            user_id="bob",
            role="user",
            api_key="bob-secret",
        ),
        AccountUserCredential(
            user_id="team",
            role="user",
            api_key="team-secret",
        ),
    ]

    credentials = service._resolve_execution_credentials(
        run=run,
        records=records,
        requested_user_ids=["alice", "bob"],
        bootstrap_api_key="admin-secret",
    )

    assert credentials.users == ["alice", "bob"]
    assert credentials.user_api_keys == {
        "alice": "alice-secret",
        "bob": "bob-secret",
    }
    assert credentials.merge_user_id == "admin"
    assert credentials.merge_api_key == "admin-secret"
    assert "alice-secret" not in repr(credentials)


def test_api_key_mode_fails_closed_when_only_key_prefixes_are_available(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    run = service.new_run(
        "default",
        auth_mode="api_key",
        endpoint="https://openviking.example",
    )
    records = [
        AccountUserCredential(
            user_id="admin",
            role="admin",
            key_prefix="admin-prefix",
        ),
        AccountUserCredential(
            user_id="alice",
            role="user",
            key_prefix="alice-prefix",
        ),
    ]

    with pytest.raises(SourceExpansionError, match="key rotation was not attempted"):
        service._resolve_execution_credentials(
            run=run,
            records=records,
            requested_user_ids=["alice"],
            bootstrap_api_key="admin-secret",
        )

    assert "admin-prefix" not in json.dumps(run.to_public())
    assert "alice-prefix" not in json.dumps(run.to_public())
