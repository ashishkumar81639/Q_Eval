import httpx
import pytest
import respx

from qeval.client import AgentError, AuthError, NotFoundError, QClient, ScenarioError

BASE = "https://q.test"


async def nosleep(_):
    return None


def client():
    return QClient(BASE, "tok", sleep=nosleep)


@respx.mock
async def test_create_and_message():
    respx.post(f"{BASE}/accounts").mock(
        return_value=httpx.Response(201, json={"account_id": "acc_1", "refs": {"a": "s_1"}, "state": {}})
    )
    route = respx.post(f"{BASE}/accounts/acc_1/messages").mock(
        side_effect=[
            httpx.Response(409, json={"detail": "busy"}),
            httpx.Response(200, json={"respond": {"kind": "info"}, "latency_ms": 5}),
        ]
    )
    async with client() as c:
        res = await c.create_account({})
        assert res.account_id == "acc_1" and res.refs == {"a": "s_1"}
        m = await c.send_message("acc_1", "hi", "2026-10-01T10:00:00+05:30")
        assert m.raw["respond"]["kind"] == "info" and m.latency_ms == 5
    assert route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
@pytest.mark.parametrize(
    "status,exc", [(401, AuthError), (404, NotFoundError), (422, ScenarioError), (502, AgentError)]
)
async def test_errors(status, exc):
    respx.post(f"{BASE}/accounts/acc_1/messages").mock(
        return_value=httpx.Response(status, json={"detail": [{"msg": "x"}]})
    )
    async with client() as c:
        with pytest.raises(exc):
            await c.send_message("acc_1", "hi", "2026-10-01T10:00:00+05:30")


@respx.mock
async def test_422_detail_string_and_list():
    respx.post(f"{BASE}/accounts").mock(
        side_effect=[
            httpx.Response(422, json={"detail": "bad"}),
            httpx.Response(422, json={"detail": [{"loc": ["x"]}]}),
        ]
    )
    async with client() as c:
        with pytest.raises(ScenarioError) as e1:
            await c.create_account({})
        assert e1.value.detail == "bad"
        with pytest.raises(ScenarioError) as e2:
            await c.create_account({})
        assert e2.value.detail == [{"loc": ["x"]}]


@respx.mock
async def test_timeout_is_agent_error_no_retry():
    route = respx.post(f"{BASE}/accounts/acc_1/messages").mock(side_effect=httpx.ReadTimeout("slow"))
    async with client() as c:
        with pytest.raises(AgentError):
            await c.send_message("acc_1", "hi", "2026-10-01T10:00:00+05:30")
    assert route.call_count == 1


@respx.mock
async def test_get_retries_connection_errors():
    route = respx.get(f"{BASE}/accounts/acc_1/state").mock(
        side_effect=[httpx.ConnectError("x"), httpx.Response(200, json={"ok": 1})]
    )
    async with client() as c:
        assert await c.get_state("acc_1") == {"ok": 1}
    assert route.call_count == 2


def test_token_not_in_error_text():
    from qeval.client import QError

    e = QError("x", status=500, body="b", request="POST /accounts")
    assert "tok" not in str(e)
