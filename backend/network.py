"""Public-internet HTTP transport for untrusted ATProto discovery endpoints."""

import asyncio
import ipaddress
import re
import socket
from types import SimpleNamespace

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


def public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped or address.sixtofour or address
    return address.is_global and not address.is_multicast


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.backend = AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve once and connect to the checked address. Re-resolving the
        # hostname at connect time would permit DNS rebinding into private LANs.
        try:
            async with asyncio.timeout(timeout or 10):
                answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, TimeoutError) as error:
            raise httpcore.ConnectError("Could not resolve remote host") from error
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not public_address(address) for address in addresses):
            raise httpcore.ConnectError("Remote endpoint must have public IP addresses")
        # Try IPv4 before IPv6 on hosts without an IPv6 route.
        addresses.sort(key=lambda address: ":" in address)
        last_error = None
        for address in addresses:
            try:
                return await self.backend.connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        raise last_error

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("Unix sockets are not remote ATProto endpoints")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class PublicTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False, limits=httpx.Limits(max_connections=10))
        # httpx 0.28.1 does not expose network_backend. This small adapter is
        # pinned to httpx/httpcore versions in requirements.txt and tested.
        self._pool._network_backend = PublicNetworkBackend()

    async def handle_async_request(self, request):
        if request.url.scheme != "https" or request.url.username or request.url.password:
            raise httpx.ConnectError("Remote endpoints must use HTTPS without URL credentials", request=request)
        # httpcore retains the original hostname for TLS certificate/SNI checks;
        # only its socket connection uses the validated IP address.
        return await super().handle_async_request(request)


def public_client(timeout=10, max_redirects=3, **kwargs):
    return httpx.AsyncClient(transport=PublicTransport(), timeout=timeout, follow_redirects=True,
                             max_redirects=max_redirects, trust_env=False)


async def get_json(url, **kwargs):
    async with public_client() as client:
        response = await client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()


class HandleResolver:
    async def resolve(self, handle):
        # The public resolver supports handles hosted by other providers too.
        data = await get_json("https://bsky.social/xrpc/com.atproto.identity.resolveHandle", params={"handle": handle})
        return data["did"]


class DidResolver:
    async def resolve_atproto_data(self, did, force_refresh=False):
        if re.fullmatch(r"did:plc:[a-z2-7]{24}", did):
            url = "https://plc.directory/" + did
        elif re.fullmatch(r"did:web:[a-zA-Z0-9.-]+", did):
            url = "https://" + did[8:] + "/.well-known/did.json"
        else:
            raise ValueError("Unsupported DID")
        document = await get_json(url)
        if document.get("id") != did:
            raise ValueError("DID document identity mismatch")
        pds = next((service.get("serviceEndpoint") for service in document.get("service", [])
                    if service.get("type") == "AtprotoPersonalDataServer"
                    and service.get("id") in ("#atproto_pds", did + "#atproto_pds")), None)
        if not isinstance(pds, str) or not pds.startswith("https://"):
            raise ValueError("Account has no HTTPS PDS")
        handle = next((alias[5:] for alias in document.get("alsoKnownAs", [])
                       if isinstance(alias, str) and alias.startswith("at://")), "")
        return SimpleNamespace(pds=pds, handle=handle)


class Resolver:
    handle = HandleResolver()
    did = DidResolver()
