from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers import auth_router
from yuxi.storage.postgres.models_business import Base, ModelProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class _Response:
    def __init__(self, payload: object):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, response: _Response | None = None, error: Exception | None = None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def get(self, url: str, headers: dict[str, str]):
        del url, headers
        if self._error:
            raise self._error
        return self._response


async def test_saas_model_sync_refreshes_cache_after_commit(monkeypatch):
    events: list[str] = []

    class FakeDB:
        async def commit(self):
            events.append("commit")

    async def refresh_cache():
        events.append("refresh")

    monkeypatch.setattr(auth_router, "_refresh_model_cache", refresh_cache)

    await auth_router._commit_saas_changes_and_refresh_model_cache(FakeDB())

    assert events == ["commit", "refresh"]


async def test_upsert_saas_model_provider_updates_models_from_newapi(monkeypatch, db):
    provider = ModelProvider(
        provider_id="saas-newapi",
        display_name="SaaS NewAPI",
        provider_type="openai",
        base_url="https://old.example/v1",
        api_key="old-key",
        enabled_models=[{"id": "old-model", "type": "chat"}],
        is_enabled=True,
        is_builtin=False,
    )
    db.add(provider)
    await db.commit()

    response = _Response({"data": [{"id": "new-model", "name": "New Model"}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client(response=response))

    await auth_router._upsert_saas_model_provider(db, "new-key", "https://newapi.example")

    assert provider.base_url == "https://newapi.example"
    assert provider.api_key == "new-key"
    assert provider.enabled_models == [
        {
            "id": "new-model",
            "type": "chat",
            "source": "remote",
            "display_name": "New Model",
            "request_body_overrides": {"thinking": {"type": "disabled"}},
        }
    ]


async def test_upsert_saas_model_provider_keeps_existing_models_when_newapi_fails(monkeypatch, db):
    existing_models = [{"id": "existing-model", "type": "chat"}]
    provider = ModelProvider(
        provider_id="saas-newapi",
        display_name="SaaS NewAPI",
        provider_type="openai",
        base_url="https://old.example/v1",
        api_key="old-key",
        enabled_models=existing_models,
        is_enabled=True,
        is_builtin=False,
    )
    db.add(provider)
    await db.commit()

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _Client(error=RuntimeError("NewAPI unavailable")),
    )

    await auth_router._upsert_saas_model_provider(db, "new-key", "https://newapi.example")

    assert provider.enabled_models == existing_models
