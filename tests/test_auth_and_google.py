"""Comprehensive tests for Neon Sync Authentication, SQLite DB, Google OAuth 2.0 + PKCE & API Keys."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from neon_sync.api import create_app
from neon_sync.auth import AuthDatabase, AuthService, GoogleProfile
from neon_sync.config import Settings


async def test_email_password_auth_sessions_profile_upgrade_and_api_keys(tmp_path: Path):
    auth_db_path = tmp_path / "auth.sqlite3"
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        auth_db_path=auth_db_path,
        pro_api_keys="neon-pro-local",
        min_free_disk_bytes=1024,
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # 1. Guest state initially
        me0 = await client.get("/v1/auth/me")
        assert me0.status_code == 200
        assert me0.json()["authenticated"] is False

        # 2. Register new account -> sets HttpOnly session cookie
        reg = await client.post(
            "/v1/auth/register",
            json={
                "name": "Anurag Singh",
                "email": "anurag@neonsync.dev",
                "password": "SuperSecretPassword123!",
            },
        )
        assert reg.status_code == 201
        assert "neon_sync_session=" in reg.headers.get("set-cookie", "")
        assert "httponly" in reg.headers.get("set-cookie", "").lower()
        rdata = reg.json()
        assert rdata["user"]["email"] == "anurag@neonsync.dev"
        assert rdata["user"]["tier"] == "free"

        # Duplicate registration rejected with 409
        dup = await client.post(
            "/v1/auth/register",
            json={"name": "Dup", "email": "ANURAG@neonsync.dev", "password": "SuperSecretPassword123!"},
        )
        assert dup.status_code == 409

        # 3. /v1/auth/me resolves authenticated user via HttpOnly session cookie
        me1 = await client.get("/v1/auth/me")
        assert me1.status_code == 200
        assert me1.json()["authenticated"] is True
        assert me1.json()["auth_method"] == "session"
        assert me1.json()["user"]["name"] == "Anurag Singh"

        # 4. Update profile & upgrade account permanently to Pro in SQLite
        upd = await client.patch("/v1/auth/me", json={"name": "Anurag S."})
        assert upd.status_code == 200
        assert upd.json()["name"] == "Anurag S."

        upg = await client.post("/v1/auth/upgrade", json={"pro_key": "neon-pro-local"})
        assert upg.status_code == 200
        assert upg.json()["tier"] == "pro"

        # Signed-in Pro user automatically receives Pro tier on /v1/tier without X-Neon-Pro-Key!
        tier = await client.get("/v1/tier")
        assert tier.status_code == 200
        assert tier.json()["tier"] == "pro"
        assert tier.json()["allow_4k"] is True

        # 5. Create a personal developer API key and authenticate with it in a clean client (no cookies!)
        key_resp = await client.post(
            "/v1/auth/api-keys",
            json={"name": "Backend Worker", "scopes": ["read", "write"]},
        )
        assert key_resp.status_code == 201
        raw_secret = key_resp.json()["secret"]
        key_id = key_resp.json()["api_key"]["id"]
        assert raw_secret.startswith("ns_live_")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as api_client:
            me_key = await api_client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {raw_secret}"},
            )
            assert me_key.status_code == 200
            assert me_key.json()["authenticated"] is True
            assert me_key.json()["auth_method"] == "api_key"
            assert me_key.json()["user"]["tier"] == "pro"

        # Revoke API key -> immediately rejected
        rev = await client.delete(f"/v1/auth/api-keys/{key_id}")
        assert rev.status_code == 200
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as api_client:
            me_after = await api_client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {raw_secret}"},
            )
            assert me_after.json()["authenticated"] is False

        # 6. Active sessions & audit activity recorded in SQLite
        sessions = await client.get("/v1/auth/sessions")
        assert sessions.status_code == 200
        assert len(sessions.json()) == 1
        assert sessions.json()[0]["is_current"] is True

        activity = await client.get("/v1/auth/activity")
        assert activity.status_code == 200
        ev_names = {e["event"] for e in activity.json()}
        assert {"register.password", "profile.updated", "account.upgraded", "api_key.created", "api_key.revoked"}.issubset(ev_names)

        # 7. Developer /v1/logs endpoint is removed (never exposes auth traffic)
        assert (await client.get("/v1/logs?limit=100")).status_code == 404

        # 8. Logout revokes session & clears cookie
        out = await client.post("/v1/auth/logout")
        assert out.status_code == 200
        me2 = await client.get("/v1/auth/me")
        assert me2.json()["authenticated"] is False

        # 9. Login back in with password (persisted in SQLite!)
        login = await client.post(
            "/v1/auth/login",
            json={"email": "anurag@neonsync.dev", "password": "SuperSecretPassword123!"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["tier"] == "pro"


async def test_google_oauth_pkce_flow_and_id_token_account_linking(tmp_path: Path):
    auth_db = AuthDatabase(tmp_path / "google_auth.sqlite3")
    settings = Settings(
        scratch_dir=tmp_path / "scratch",
        storage_dir=tmp_path / "storage",
        auth_db_path=tmp_path / "google_auth.sqlite3",
        google_client_id="test-google-client.apps.googleusercontent.com",
        google_client_secret="test-google-secret",
        google_redirect_uri=None,
        public_base_url=None,
        min_free_disk_bytes=1024,
    )

    exchanged_pkce: dict[str, str] = {}

    async def _fake_code_exchange(code: str, code_verifier: str, redirect_uri: str) -> GoogleProfile:
        exchanged_pkce["code"] = code
        exchanged_pkce["verifier"] = code_verifier
        exchanged_pkce["redirect_uri"] = redirect_uri
        return GoogleProfile(
            sub="google-sub-123456",
            email="creator@gmail.com",
            email_verified=True,
            name="Neon Creator",
            avatar_url="https://lh3.googleusercontent.com/a/avatar.png",
        )

    async def _fake_id_token(credential: str) -> GoogleProfile:
        assert credential == "fake-google-id-token-jwt-payload-123456"
        return GoogleProfile(
            sub="google-sub-123456",
            email="creator@gmail.com",
            email_verified=True,
            name="Neon Creator",
            avatar_url="https://lh3.googleusercontent.com/a/avatar.png",
        )

    auth_service = AuthService(
        settings,
        auth_db,
        google_code_exchanger=_fake_code_exchange,
        google_id_token_verifier=_fake_id_token,
    )
    app = create_app(settings, auth_service=auth_service)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        # 1. Providers endpoint reports Google enabled
        prov = await client.get("/v1/auth/providers")
        assert prov.status_code == 200
        assert prov.json()["google_enabled"] is True
        assert prov.json()["google_redirect_uri"] == "http://testserver/v1/auth/google/callback"

        # 2. Start Google OAuth 2.0 + PKCE redirect
        start = await client.get("/v1/auth/google/start?next=/")
        assert start.status_code == 302
        location = start.headers["location"]
        parsed = urlparse(location)
        qs = parse_qs(parsed.query)
        assert parsed.netloc == "accounts.google.com"
        assert qs["client_id"] == ["test-google-client.apps.googleusercontent.com"]
        assert qs["code_challenge_method"] == ["S256"]
        assert "state" in qs and len(qs["state"][0]) >= 20
        oauth_state = qs["state"][0]

        # 3. Complete Google callback -> creates user + links Google OAuth + sets HttpOnly cookie
        cb = await client.get(f"/v1/auth/google/callback?code=auth_code_xyz&state={oauth_state}")
        assert cb.status_code == 302
        assert "auth=google_ok" in cb.headers["location"]
        assert "neon_sync_session=" in cb.headers.get("set-cookie", "")
        assert exchanged_pkce["code"] == "auth_code_xyz"
        assert len(exchanged_pkce["verifier"]) >= 32

        # Replay of the exact same OAuth state MUST fail (atomic one-time consumption!)
        replay = await client.get(f"/v1/auth/google/callback?code=auth_code_xyz&state={oauth_state}")
        assert replay.status_code == 302
        assert "auth_error=ERR_OAUTH_INVALID_STATE" in replay.headers["location"]

        # 4. /v1/auth/me returns the Google user with verified email & Google provider
        me = await client.get("/v1/auth/me")
        assert me.status_code == 200
        u = me.json()["user"]
        assert u["email"] == "creator@gmail.com"
        assert u["email_verified"] is True
        assert "google" in u["providers"]
        assert u["avatar_url"] == "https://lh3.googleusercontent.com/a/avatar.png"

        # 5. Google One Tap / ID token endpoint resolves the exact same linked user (no duplicate!)
        gis = await client.post(
            "/v1/auth/google/token",
            json={"credential": "fake-google-id-token-jwt-payload-123456"},
        )
        assert gis.status_code == 200
        assert gis.json()["user"]["id"] == u["id"]

        # 6. Google-only user can optionally set a password too
        pw = await client.post("/v1/auth/password", json={"new_password": "AddedPassword99!"})
        assert pw.status_code == 200
        assert pw.json()["has_password"] is True
        assert "password" in pw.json()["providers"]
        assert "google" in pw.json()["providers"]
