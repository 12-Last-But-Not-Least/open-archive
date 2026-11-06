"""ATProto OAuth adapter; the protocol library handles PKCE, PAR and DPoP."""

from contextvars import ContextVar
import base64
import hashlib
from urllib.parse import urlencode

from atproto_oauth import OAuthClient
from atproto_oauth import client as protocol_client, metadata as protocol_metadata
from atproto_oauth.exceptions import OAuthTokenError
from atproto_oauth.metadata import discover_authserver_from_pds_async
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .network import Resolver, public_client
from .storage import OAuthStore

COLLECTION = "io.github.12-last-but-not-least.publication"
SCOPE = f"atproto repo:{COLLECTION}?action=create&action=delete"
refresh_did = ContextVar("refresh_did", default=None)

# Replace both network factory imports in pinned atproto-oauth 0.1.0. Its URL
# checks alone do not stop hostnames resolving to private addresses.
protocol_client.get_hardened_async_client = public_client
protocol_metadata.get_hardened_async_client = public_client


class ArchiveOAuthClient(OAuthClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._id_resolver = Resolver()

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


def signing_key(database):
    saved = database.get("oauth-client-signing-key")
    if saved is None:
        private_key = ec.generate_private_key(ec.SECP256R1())
        saved = {"pem": private_key.private_bytes(serialization.Encoding.PEM,
                 serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
        database.put("oauth-client-signing-key", saved, ttl=10 * 365 * 86400)
    private_key = serialization.load_pem_private_key(saved["pem"].encode(), password=None)
    numbers = private_key.public_key().public_numbers()
    encode = lambda n: base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()
    kid = hashlib.sha256(private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()[:24]
    return private_key, {"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig",
                         "kid": kid, "x": encode(numbers.x), "y": encode(numbers.y)}


def client_metadata(database, origin):
    metadata = {"client_id": origin + "/oauth-client-metadata.json", "client_name": "Open Archive",
                "client_uri": origin, "redirect_uris": [origin + "/auth/callback"],
                "scope": SCOPE, "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "application_type": "web",
                "token_endpoint_auth_method": "private_key_jwt", "token_endpoint_auth_signing_alg": "ES256",
                "dpop_bound_access_tokens": True, "jwks": {"keys": [signing_key(database)[1]]}}
    return metadata


def make_client(database, browser_key, origin):
    store = OAuthStore(database, browser_key)
    callback = origin + "/auth/callback"
    # ATProto's loopback exception allows a real login without a public server.
    options = {}
    if origin.startswith("http://127.0.0.1"):
        client_id = "http://localhost?" + urlencode({"redirect_uri": callback, "scope": SCOPE})
    else:
        client_id = origin + "/oauth-client-metadata.json"
        key, jwk = signing_key(database)
        options = {"client_secret_key": key, "client_secret_kid": jwk["kid"]}
    return ArchiveOAuthClient(client_id=client_id, redirect_uri=callback, scope=SCOPE,
                              state_store=store, session_store=store, **options)
