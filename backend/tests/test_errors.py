"""统一错误格式测试（T2.1 验收，17.1 节）。"""

import httpx

ERROR_KEYS = {"code", "message", "request_id", "details"}


def _assert_error_shape(resp: httpx.Response, code: str) -> None:
    body = resp.json()
    assert set(body) == ERROR_KEYS
    assert body["code"] == code
    assert body["request_id"]  # 每请求生成非空 request_id
    assert isinstance(body["details"], dict)
    assert resp.headers.get("X-Request-ID") == body["request_id"]


async def test_business_exception_format(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert resp.status_code == 401
    _assert_error_shape(resp, "INVALID_CREDENTIALS")


async def test_404_format(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    _assert_error_shape(resp, "NOT_FOUND")


async def test_422_format(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={"username": "a"})  # 缺 password
    assert resp.status_code == 422
    _assert_error_shape(resp, "VALIDATION_ERROR")


async def test_unauthorized_format(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    _assert_error_shape(resp, "INVALID_TOKEN")
