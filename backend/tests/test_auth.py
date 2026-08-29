"""登录、刷新、登出、当前用户与存储安全的认证集成测试。"""

import hashlib

import httpx
from sqlalchemy import select

from app.domains.identity.models import RefreshToken, User
from app.domains.identity.service import create_user
from app.infrastructure.database.engine import async_session_factory

PASSWORD = "Secret123!"


async def _create_user(
    username: str = "alice", password: str = PASSWORD, is_active: bool = True
) -> User:
    async with async_session_factory() as session:
        user = await create_user(session, username, password, is_active)
        await session.commit()
        return user


async def _login(
    client: httpx.AsyncClient, username: str = "alice", password: str = PASSWORD
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


async def test_login_success(client: httpx.AsyncClient) -> None:
    await _create_user()
    resp = await _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] > 0


async def test_login_wrong_password(client: httpx.AsyncClient) -> None:
    await _create_user()
    resp = await _login(client, password="wrong-password")
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"


async def test_login_disabled_user(client: httpx.AsyncClient) -> None:
    await _create_user(is_active=False)
    resp = await _login(client)
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_DISABLED"


async def test_me_with_access_token(client: httpx.AsyncClient) -> None:
    user = await _create_user()
    tokens = (await _login(client)).json()
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(user.id)
    assert body["username"] == "alice"
    assert body["is_active"] is True
    assert "password_hash" not in body


async def test_refresh_rotation(client: httpx.AsyncClient) -> None:
    """刷新即轮换：旧 Refresh Token 立即作废，新令牌可用。"""
    await _create_user()
    tokens = (await _login(client)).json()

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 200
    rotated = resp.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "REFRESH_TOKEN_INVALID"

    # 新 access token 可用
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}
    )
    assert me.status_code == 200


async def test_logout_revokes_refresh_token(client: httpx.AsyncClient) -> None:
    """登出后原 Refresh Token 无法再换取 Access Token。"""
    await _create_user()
    tokens = (await _login(client)).json()

    resp = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 200

    again = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert again.status_code == 401
    assert again.json()["code"] == "REFRESH_TOKEN_INVALID"


async def test_no_plaintext_password_or_refresh_token(client: httpx.AsyncClient) -> None:
    """库中只存哈希：密码为 Argon2，Refresh Token 为 SHA-256。"""
    await _create_user()
    tokens = (await _login(client)).json()

    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        assert user.password_hash.startswith("$argon2")
        assert PASSWORD not in user.password_hash

        records = (await session.execute(select(RefreshToken))).scalars().all()
        assert len(records) == 1
        expected_hash = hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
        assert records[0].token_hash == expected_hash
        assert records[0].token_hash != tokens["refresh_token"]


async def test_disabled_user_token_rejected(client: httpx.AsyncClient) -> None:
    """已签发的 Access Token 在用户被禁用后立即失效。"""
    user = await _create_user()
    tokens = (await _login(client)).json()

    async with async_session_factory() as session:
        db_user = await session.get(User, user.id)
        db_user.is_active = False
        await session.commit()

    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_DISABLED"


async def test_token_version_bump_invalidates_access_token(
    client: httpx.AsyncClient,
) -> None:
    """提升 users.token_version 后旧 Access Token 失效。"""
    user = await _create_user()
    tokens = (await _login(client)).json()

    async with async_session_factory() as session:
        db_user = await session.get(User, user.id)
        db_user.token_version += 1
        await session.commit()

    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_TOKEN"
