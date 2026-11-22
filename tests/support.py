"""Shared deterministic OAuth, PDS and search doubles for local tests only."""
from datetime import datetime, timedelta, timezone

import httpx
from atproto_oauth.models import OAuthSession, OAuthState
from cryptography.hazmat.primitives.asymmetric import ec

from backend.oauth import SCOPE
from backend.papers import COLLECTION
from backend.storage import OAuthStore

ORIGIN = "http://127.0.0.1:8000"
DID = "did:plc:rqu4asr46qcalfx5b2mxqzvx"
OTHER = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"

PAPER = {"id": "arxiv:2606.14083", "arxivId": "2606.14083", "title": "N2D", "authors": ["Bryce Hackel"],
         "publishedAt": "2026-06-12T00:00:00Z", "url": "https://arxiv.org/abs/2606.14083",
         "researcherIds": ["bryce-hackel"], "labIds": ["ucsd-arc"]}


class FakeOAuth:
    def __init__(self, database, browser_key, origin, network):
        self.state_store = self.session_store = OAuthStore(database, browser_key)
        self.network = network
        self.origin = origin

    async def start_authorization(self, handle):
        import secrets
        self.network["state"] = state = secrets.token_urlsafe(32)
        await self.state_store.save_state(OAuthState(
            state=state, pkce_verifier="test-verifier", redirect_uri=self.origin + "/auth/callback",
            scope=SCOPE, authserver_iss="https://bsky.social", dpop_private_key=ec.generate_private_key(ec.SECP256R1()),
            dpop_authserver_nonce="test-nonce", did=DID if handle == "bhackel.bsky.social" else OTHER, handle=handle))
        return "https://bsky.social/oauth/authorize?request_uri=test", state

    async def handle_callback(self, code, state, iss):
        saved = await self.state_store.get_state(state)
        if not saved or iss != saved.authserver_iss or code != "test-code":
            raise ValueError("Invalid callback")
        await self.state_store.delete_state(state)
        session = OAuthSession(
            did=saved.did, handle=saved.handle, pds_url="https://example.test", authserver_iss=iss,
            access_token="test-secret-access", refresh_token="test-secret-refresh", dpop_private_key=saved.dpop_private_key,
            dpop_authserver_nonce="test-nonce", scope=SCOPE, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        await self.session_store.save_session(session)
        return session

    async def revoke_session(self, session):
        self.network["revoked"].append(session.did)
        await self.session_store.delete_session(session.did)

    async def make_authenticated_request(self, session, method, url, **kwargs):
        self.network["requests"].append((session.did, method, url, kwargs))
        records = self.network["records"].setdefault(session.did, [])
        if url.endswith("listRecords"):
            data = {"records": records}
        elif url.endswith("createRecord"):
            body = kwargs["json"]
            if body["record"]["title"] == "Fail":
                return httpx.Response(503, request=httpx.Request(method, url))
            uri = f'at://{session.did}/{COLLECTION}/{body["rkey"]}'
            records.append({"uri": uri, "value": body["record"], "cid": "test-cid"})
            data = {"uri": uri}
        elif url.endswith("deleteRecord"):
            rkey = kwargs["json"]["rkey"]
            records[:] = [r for r in records if not r["uri"].endswith("/" + rkey)]
            data = {}
        else:
            raise AssertionError(url)
        return httpx.Response(200, json=data, request=httpx.Request(method, url))


class FakeSearch:
    def __init__(self):
        self.calls = []

    async def search(self, author):
        self.calls.append(author)
        return [{**PAPER, "title": "New paper", "id": "arxiv:2606.99999", "arxivId": "2606.99999"}]
