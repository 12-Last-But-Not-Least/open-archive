"""Small, standard-library helpers shared by the local scripts."""

import json
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "open-archive/0.1 (+https://github.com/12-Last-But-Not-Least/open-archive)"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def request_json(url, payload=None, token=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        # Never include request headers, passwords, or tokens in error output.
        try:
            message = json.loads(error.read()).get("message", "Request failed")
        except (ValueError, AttributeError):
            message = "Request failed"
        raise RuntimeError(f"HTTP {error.code}: {message}") from None


def arxiv_id(value):
    candidate = re.sub(r"^arxiv:", "", value.strip(), flags=re.I)
    if candidate.startswith(("https://", "http://")):
        parsed = urlparse(candidate)
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ValueError("Use an arxiv.org URL or an arXiv identifier.")
        candidate = re.sub(r"^/(abs|pdf|html)/", "", parsed.path)
    candidate = re.sub(r"\.pdf$", "", candidate)
    candidate = re.sub(r"v\d+$", "", candidate)
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})", candidate):
        raise ValueError("Invalid arXiv identifier.")
    return candidate


def resolve_identity(identifier):
    identifier = identifier.strip().removeprefix("@")
    if identifier.startswith("did:"):
        did = identifier
    else:
        response = request_json("https://bsky.social/xrpc/com.atproto.identity.resolveHandle?" + urlencode({"handle": identifier}))
        did = response["did"]
    if did.startswith("did:plc:") and re.fullmatch(r"did:plc:[a-z2-7]{24}", did):
        document = request_json("https://plc.directory/" + did)
    elif did.startswith("did:web:"):
        # Demo supports domain-only did:web identities, not arbitrary DID paths.
        domain = did[len("did:web:"):]
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", domain) or "." not in domain:
            raise ValueError("Only domain-only did:web identities are supported.")
        document = request_json("https://" + domain + "/.well-known/did.json")
    else:
        raise ValueError("Unsupported ATProto DID.")
    if document.get("id") != did:
        raise ValueError("DID document identity does not match.")
    for service in document.get("service", []):
        if service.get("type") == "AtprotoPersonalDataServer":
            endpoint = service.get("serviceEndpoint", "")
            parsed = urlparse(endpoint)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                return did, endpoint.rstrip("/")
    raise ValueError("No HTTPS PDS found for this account.")
