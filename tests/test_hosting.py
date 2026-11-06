import asyncio
import base64
import json
import socket

import httpcore
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.network import DidResolver, PublicNetworkBackend, PublicTransport, public_address
from backend.oauth import client_metadata, make_client
from backend.storage import Database

ORIGIN = "https://archive.example.com"


def test_hosted_oauth_metadata_and_signing_key_survive_restart(tmp_path):
    database = Database(tmp_path)
    metadata = client_metadata(database, ORIGIN)
    assert metadata["client_id"] == ORIGIN + "/oauth-client-metadata.json"
    assert metadata["redirect_uris"] == [ORIGIN + "/auth/callback"]
    assert metadata["token_endpoint_auth_method"] == "private_key_jwt"
    assert "d" not in metadata["jwks"]["keys"][0]
    assert "pem" not in json.dumps(metadata)
    assert client_metadata(Database(tmp_path), ORIGIN) == metadata

    client = make_client(database, "browser", ORIGIN)
    jwt = client._create_client_assertion("https://bsky.social")
    header, payload, signature = jwt.split(".")
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    claims = json.loads(decode(payload))
    assert claims["iss"] == metadata["client_id"]
    assert claims["aud"] == "https://bsky.social"
    jwk = metadata["jwks"]["keys"][0]
    assert json.loads(decode(header))["kid"] == jwk["kid"]
    public_key = ec.EllipticCurvePublicNumbers(int.from_bytes(decode(jwk["x"])), int.from_bytes(decode(jwk["y"])), ec.SECP256R1()).public_key()
    signature = decode(signature)
    der = encode_dss_signature(int.from_bytes(signature[:32]), int.from_bytes(signature[32:]))
    public_key.verify(der, (header + "." + payload).encode(), ec.ECDSA(hashes.SHA256()))


def test_https_cookies_origin_checks_and_public_directory(tmp_path):
    app = create_app(directory=tmp_path, origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/session")
        assert response.headers["set-cookie"].startswith("__Host-open_archive_session=")
        assert "Secure" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert response.headers["Cache-Control"] == "no-store"
        assert "access-control-allow-origin" not in response.headers
        csrf = response.json()["csrf"]
        assert client.post("/auth/logout", headers={"Origin": "http://127.0.0.1:8000", "X-CSRF-Token": csrf}).status_code == 403
        assert client.get("/oauth-client-metadata.json").status_code == 200
        assert client.get("/data/site.json").headers["access-control-allow-origin"] == "*"
        assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/auth/login", content=b"x" * 17000, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 413


def test_hosted_mode_requires_storage_and_railway_requires_real_volume(tmp_path, monkeypatch):
    monkeypatch.delenv("OPEN_ARCHIVE_STATE_DIR", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    with pytest.raises(ValueError, match="persistent state directory"):
        create_app(origin=ORIGIN)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "test-environment")
    with pytest.raises(ValueError, match="persistent Railway volume"):
        create_app(origin=ORIGIN, directory=tmp_path)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    app = create_app(origin=ORIGIN, directory=tmp_path / "archive")
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/health", headers={"Host": "healthcheck.railway.app"}).status_code == 200


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_private_and_metadata_addresses_are_blocked(address):
    assert not public_address(address)


def test_dns_results_are_checked_and_connections_use_checked_ips(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        calls = []
        backend = PublicNetworkBackend()

        async def connect(host, *args):
            calls.append(host)
            return "stream"

        async def resolve(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        monkeypatch.setattr(backend.backend, "connect_tcp", connect)
        assert await backend.connect_tcp("example.com", 443) == "stream"
        assert calls == ["93.184.216.34"]

        async def private(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        monkeypatch.setattr(loop, "getaddrinfo", private)
        with pytest.raises(httpcore.ConnectError, match="public IP"):
            await backend.connect_tcp("private.example.com", 443)
        assert len(calls) == 1
    asyncio.run(scenario())


def test_redirects_cannot_reach_http_or_url_credentials():
    async def scenario():
        async with httpx.AsyncClient(transport=PublicTransport()) as client:
            for url in ("http://example.com/", "https://user:password@example.com/"):
                with pytest.raises(httpx.ConnectError, match="HTTPS"):
                    await client.get(url)
    asyncio.run(scenario())


def test_safe_resolver_checks_did_identity(monkeypatch):
    from backend import network

    async def wrong_document(*args, **kwargs):
        return {"id": "did:plc:wrong"}

    monkeypatch.setattr(network, "get_json", wrong_document)
    with pytest.raises(ValueError, match="identity mismatch"):
        asyncio.run(DidResolver().resolve_atproto_data("did:plc:rqu4asr46qcalfx5b2mxqzvx"))


def test_anonymous_session_creation_is_rate_limited(tmp_path):
    with TestClient(create_app(directory=tmp_path), base_url="http://127.0.0.1:8000") as client:
        for _ in range(60):
            assert client.get("/api/session").status_code == 200
        assert client.get("/api/session").status_code == 429
