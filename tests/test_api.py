import json

import httpx
import pytest

from marvin_mcp_server.api import MarvinAPI, MarvinAPIError, MissingTokenError


def make_api(
    handler: httpx.MockTransport,
    *,
    api_token: str | None = "api",
    full: str | None = "full",
) -> MarvinAPI:
    return MarvinAPI(
        api_token=api_token,
        full_access_token=full,
        min_interval=0,
        client=httpx.AsyncClient(transport=handler),
    )


@pytest.mark.anyio
async def test_add_task_uses_api_token_and_disables_autocomplete() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"_id": "new", "db": "Tasks", "title": "x"})

    api = make_api(httpx.MockTransport(handler))
    result = await api.add_task({"title": "x #notAProject"})
    assert result["_id"] == "new"
    req = captured[0]
    assert req.url.path == "/api/addTask"
    assert req.headers["X-API-Token"] == "api"
    assert req.headers["X-Auto-Complete"] == "false"
    assert "X-Full-Access-Token" not in req.headers


@pytest.mark.anyio
async def test_update_doc_builds_setters_with_field_updates() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"_id": "t1", "db": "Tasks", "title": "new"})

    api = make_api(httpx.MockTransport(handler))
    await api.update_doc("t1", {"title": "new", "labelIds": ["a"]})
    body = json.loads(captured[0].content)
    assert captured[0].headers["X-Full-Access-Token"] == "full"
    assert body["itemId"] == "t1"
    keys = [s["key"] for s in body["setters"]]
    assert keys == [
        "title",
        "fieldUpdates.title",
        "labelIds",
        "fieldUpdates.labelIds",
        "updatedAt",
    ]
    stamps = {
        s["val"]
        for s in body["setters"]
        if s["key"].startswith(("fieldUpdates", "updatedAt"))
    }
    assert len(stamps) == 1
    assert next(iter(stamps)) > 1_700_000_000_000


@pytest.mark.anyio
async def test_missing_tokens() -> None:
    api = make_api(
        httpx.MockTransport(lambda _r: httpx.Response(200)), api_token=None, full=None
    )
    with pytest.raises(MissingTokenError, match="API_TOKEN"):
        await api.test()
    with pytest.raises(MissingTokenError, match="FULL_ACCESS"):
        await api.get_doc("x")


@pytest.mark.anyio
async def test_http_error_is_raised_with_body() -> None:
    api = make_api(
        httpx.MockTransport(lambda _r: httpx.Response(401, text="bad token"))
    )
    with pytest.raises(MarvinAPIError, match=r"401.*bad token") as exc_info:
        await api.me()
    assert exc_info.value.status == 401


@pytest.mark.anyio
async def test_test_endpoint_strips_quotes() -> None:
    api = make_api(httpx.MockTransport(lambda _r: httpx.Response(200, text='"OK"')))
    assert await api.test() == "OK"
