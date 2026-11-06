"""ATProto OAuth adapter; the protocol library handles PKCE, PAR and DPoP."""

from contextvars import ContextVar
from urllib.parse import urlencode

from atproto_oauth import OAuthClient
from atproto_oauth.exceptions import OAuthTokenError
from atproto_oauth.metadata import discover_authserver_from_pds_async

from .storage import OAuthStore

COLLECTION = "io.github.12-last-but-not-least.publication"
SCOPE = f"atproto repo:{COLLECTION}?action=create&action=delete"
refresh_did = ContextVar("refresh_did", default=None)


class ArchiveOAuthClient(OAuthClient):
    # 0.1.0 validates the identity on callback; also bind refresh responses to
    # that identity before allowing the library to persist rotated credentials.
    async def _make_token_request(self, *args, **kwargs):
        nonce, response = await super()._make_token_request(*args, **kwargs)
        if kwargs.get("params", {}).get("grant_type") == "refresh_token" and response.is_success:
            if response.json().get("sub") != refresh_did.get():
                raise OAuthTokenError("Refreshed account identity changed")
        return nonce, response

    async def refresh_session(self, session):
        identity = await self._id_resolver.did.resolve_atproto_data(session.did, force_refresh=True)
        issuer = await discover_authserver_from_pds_async(identity.pds)
        if issuer.rstrip("/") != session.authserver_iss or identity.pds.rstrip("/") != session.pds_url.rstrip("/"):
            raise OAuthTokenError("Account hosting changed; sign in again")
        token = refresh_did.set(session.did)
        try:
            return await super().refresh_session(session)
        finally:
            refresh_did.reset(token)


def make_client(database, browser_key, origin):
    store = OAuthStore(database, browser_key)
    callback = origin + "/auth/callback"
    # ATProto's loopback exception allows a real login without a public server.
    client_id = "http://localhost?" + urlencode({"redirect_uri": callback, "scope": SCOPE})
    return ArchiveOAuthClient(client_id=client_id, redirect_uri=callback, scope=SCOPE,
                              state_store=store, session_store=store)
