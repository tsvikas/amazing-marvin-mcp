import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from tenacity import retry, retry_if_exception, stop_after_attempt

from amazing_marvin_mcp import api as api_module
from amazing_marvin_mcp.api import (
    BASE_URL,
    MarvinAPI,
    MarvinAPIError,
    MissingTokenError,
    is_transient,
)


@pytest.fixture
def api() -> MarvinAPI:
    return MarvinAPI(api_token="api", full_access_token="full", min_interval=0)


@pytest.fixture
def marvin() -> Iterator[respx.Router]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the same retry policy without the backoff sleeps."""
    monkeypatch.setattr(
        api_module,
        "transient_retry",
        retry(
            retry=retry_if_exception(is_transient),
            stop=stop_after_attempt(4),
            reraise=True,
        ),
    )


@pytest.mark.anyio
async def test_add_task_uses_api_token_and_disables_autocomplete(
    api: MarvinAPI, marvin: respx.Router
) -> None:
    route = marvin.post("/addTask").respond(
        json={"_id": "new", "db": "Tasks", "title": "x"}
    )
    result = await api.add_task({"title": "x #notAProject"})
    assert result["_id"] == "new"
    request = route.calls.last.request
    assert request.headers["X-API-Token"] == "api"
    assert request.headers["X-Auto-Complete"] == "false"
    assert "X-Full-Access-Token" not in request.headers


@pytest.mark.anyio
async def test_update_doc_builds_setters_with_field_updates(
    api: MarvinAPI, marvin: respx.Router
) -> None:
    route = marvin.post("/doc/update").respond(
        json={"_id": "t1", "db": "Tasks", "title": "new"}
    )
    await api.update_doc("t1", {"title": "new", "labelIds": ["a"]})
    request = route.calls.last.request
    body = json.loads(request.content)
    assert request.headers["X-Full-Access-Token"] == "full"
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
async def test_missing_tokens(marvin: respx.Router) -> None:
    api = MarvinAPI(api_token=None, full_access_token=None, min_interval=0)
    with pytest.raises(MissingTokenError, match="API_TOKEN"):
        await api.test()
    with pytest.raises(MissingTokenError, match="FULL_ACCESS"):
        await api.get_doc("x")
    assert not marvin.calls


@pytest.mark.anyio
async def test_http_error_is_raised_with_body(
    api: MarvinAPI, marvin: respx.Router
) -> None:
    marvin.get("/me").respond(401, text="bad token")
    with pytest.raises(MarvinAPIError, match=r"401.*bad token") as exc_info:
        await api.me()
    assert exc_info.value.status == 401


@pytest.mark.usefixtures("no_retry_wait")
@pytest.mark.anyio
async def test_transient_errors_are_retried(
    api: MarvinAPI, marvin: respx.Router
) -> None:
    route = marvin.get("/me")
    route.side_effect = [
        httpx.Response(503, text="busy"),
        httpx.ConnectError("boom"),
        httpx.Response(200, json={"email": "x"}),
    ]
    assert await api.me() == {"email": "x"}
    assert route.call_count == 3


@pytest.mark.usefixtures("no_retry_wait")
@pytest.mark.anyio
async def test_client_errors_are_not_retried(
    api: MarvinAPI, marvin: respx.Router
) -> None:
    route = marvin.get("/me").respond(404, text="nope")
    with pytest.raises(MarvinAPIError):
        await api.me()
    assert route.call_count == 1


@pytest.mark.anyio
async def test_test_endpoint_strips_quotes(
    api: MarvinAPI, marvin: respx.Router
) -> None:
    marvin.post("/test").respond(text='"OK"')
    assert await api.test() == "OK"
