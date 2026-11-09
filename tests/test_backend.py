import asyncio
import copy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from atproto_oauth.models import OAuthSession, OAuthState
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from backend.app import COOKIE, create_app, normalized_name
from backend.oauth import ArchiveOAuthClient, SCOPE, refresh_did
from backend.papers import COLLECTION, parse_arxiv, normalize_doi, parse_crossref
from backend.storage import Database, OAuthStore

ORIGIN = "http://127.0.0.1:8000"
DID = "did:plc:rqu4asr46qcalfx5b2mxqzvx"
OTHER = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"
CONFIG = {"collection": COLLECTION, "researchers": [
    {"id": "bryce-hackel", "name": "Bryce Hackel", "did": DID, "handle": "bhackel.bsky.social"},
    {"id": "jay", "name": "Jay", "did": "", "handle": ""},
], "labs": [{"id": "ucsd-arc", "name": "ARC", "members": ["bryce-hackel"]}]}
PAPER = {"id": "arxiv:2606.14083", "arxivId": "2606.14083", "title": "N2D", "authors": ["Bryce Hackel"],
         "publishedAt": "2026-06-12T00:00:00Z", "url": "https://arxiv.org/abs/2606.14083",
         "researcherIds": ["bryce-hackel"], "labIds": ["ucsd-arc"]}


class FakeOAuth:
    def __init__(self, database, browser_key, origin, network):
        self.state_store = self.session_store = OAuthStore(database, browser_key)
        self.network = network

    async def start_authorization(self, handle):
        import secrets
        self.network["state"] = state = secrets.token_urlsafe(32)
        await self.state_store.save_state(OAuthState(
            state=state, pkce_verifier="test-verifier", redirect_uri=ORIGIN + "/auth/callback",
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
        return [{**PAPER, "title": "New paper", "id": "arxiv:2606.99999", "arxivId": "2606.99999", "labIds": []}]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    async def public_profile(url, **kwargs):
        did = kwargs["params"]["repo"]
        return {"uri": f"at://{did}/app.bsky.actor.profile/self", "value": {}}
    monkeypatch.setattr("backend.app.get_json", public_profile)
    async def empty_openalex(query, field):
        return []
    async def empty_datacite(query):
        return []
    monkeypatch.setattr("backend.app.search_openalex", empty_openalex)
    monkeypatch.setattr("backend.app.search_datacite", empty_datacite)
    network = {"requests": [], "records": {}, "revoked": []}
    searcher = FakeSearch()
    app = create_app(directory=tmp_path, config=copy.deepcopy(CONFIG), catalog=[copy.deepcopy(PAPER)],
                     searcher=searcher, oauth_factory=lambda *args: FakeOAuth(*args, network))
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app, network, searcher


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/session").json()["csrf"]}


def login(client, network, handle="bhackel.bsky.social"):
    response = client.post("/auth/login", json={"handle": handle}, headers=headers(client))
    assert response.status_code == 200
    response = client.get("/auth/callback", params={"code": "test-code", "state": network["state"], "iss": "https://bsky.social"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/account/"


def test_real_login_adapter_has_minimal_publication_permissions(tmp_path):
    from backend.oauth import make_client
    client = make_client(Database(tmp_path), "browser", ORIGIN)
    assert client.client_id.startswith("http://localhost?")
    assert client.redirect_uri == ORIGIN + "/auth/callback"
    assert client.scope == f"atproto repo:{COLLECTION}?action=create&action=delete"


def test_login_rotates_cookie_keeps_tokens_private_and_logs_out(setup):
    client, app, network, searcher = setup
    info = client.get("/api/session")
    old_cookie = client.cookies.get(COOKIE)
    assert "HttpOnly" in info.headers["set-cookie"]
    assert "SameSite=lax" in info.headers["set-cookie"]
    login(client, network)
    assert client.cookies.get(COOKIE) != old_cookie
    info = client.get("/api/session")
    assert info.json()["researcher"]["id"] == "bryce-hackel"
    assert "test-secret" not in info.text
    assert b"test-secret" not in app.state.database.path.read_bytes()
    assert searcher.calls == []
    assert client.post("/auth/logout", headers=headers(client)).status_code == 200
    assert client.get("/api/publications").status_code == 401
    assert network["revoked"] == [DID]


def test_csrf_unauthenticated_writes_and_untrusted_hosts_are_rejected(setup):
    client, _, network, _ = setup
    assert client.post("/auth/login", json={"handle": "bhackel.bsky.social"}).status_code == 403
    assert client.post("/api/publications", json={"papers": [{"id": PAPER["id"]}]}, headers=headers(client)).status_code == 401
    login(client, network)
    bad = {**headers(client), "Origin": "https://evil.example"}
    assert client.post("/auth/logout", headers=bad).status_code == 403
    assert client.post("/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 400


def test_callback_requires_same_browser_issuer_and_one_time_state(setup):
    client, app, network, _ = setup
    client.post("/auth/login", json={"handle": "bhackel.bsky.social"}, headers=headers(client))
    params = {"code": "test-code", "state": network["state"], "iss": "https://bsky.social"}
    with TestClient(app, base_url=ORIGIN) as stranger:
        stranger.get("/api/session")
        assert stranger.get("/auth/callback", params=params).status_code == 400
    response = client.get("/auth/callback", params={**params, "iss": "https://evil.example"}, follow_redirects=False)
    assert response.headers["location"] == "/account/?error=login"
    assert client.get("/auth/callback", params=params).status_code == 400
    assert client.get("/api/session").json()["user"] is None


def test_suggestions_do_not_publish_and_selected_import_is_idempotent(setup):
    client, _, network, searcher = setup
    login(client, network)
    assert client.get("/api/suggestions").json()["papers"][0]["id"] == PAPER["id"]
    assert client.get("/api/publications").json()["papers"] == []
    assert searcher.calls == []
    payload = {"papers": [{"id": PAPER["id"], "labIds": []}]}
    first = client.post("/api/publications", json=payload, headers=headers(client))
    assert first.json()["results"][0]["status"] == "added"
    assert client.post("/api/publications", json=payload, headers=headers(client)).json()["results"][0]["status"] == "already-added"
    writes = [r for r in network["requests"] if r[1] == "POST"]
    assert len(writes) == 1
    assert writes[0][3]["json"]["repo"] == DID
    assert writes[0][3]["json"]["record"]["labIds"] == []
    assert len(client.get("/api/publications").json()["papers"]) == 1


def test_arxiv_only_runs_on_request_and_import_cannot_choose_other_account(setup):
    client, _, network, searcher = setup
    login(client, network)
    data = client.post("/api/arxiv/search", json={"author": "Bryce Hackel"}, headers=headers(client)).json()
    assert searcher.calls == ["Bryce Hackel"]
    assert not network["records"]
    payload = {"papers": [{"id": data["papers"][0]["id"]}], "repo": OTHER}
    assert client.post("/api/publications", json=payload, headers=headers(client)).status_code == 422
    payload.pop("repo")
    assert client.post("/api/publications", json=payload, headers=headers(client)).json()["results"][0]["status"] == "added"
    unknown = {"papers": [{"id": "arxiv:unknown"}]}
    assert client.post("/api/publications", json=unknown, headers=headers(client)).status_code == 400


def test_delete_does_not_restore_imported_paper(setup):
    client, _, network, _ = setup
    login(client, network)
    client.get("/api/suggestions")
    payload = {"papers": [{"id": PAPER["id"]}]}
    client.post("/api/publications", json=payload, headers=headers(client))
    paper = client.get("/api/publications").json()["papers"][0]
    assert client.delete("/api/publications/" + paper["rkey"], headers=headers(client)).status_code == 200
    assert client.get("/api/publications").json()["papers"] == []
    assert client.get("/api/suggestions").json()["papers"]


def test_import_matches_existing_doi_even_when_suggestion_has_arxiv_id(setup):
    client, app, network, _ = setup
    login(client, network)
    # Seed an on-demand suggestion with both identifiers and a DOI-only PDS record.
    client.get("/api/suggestions")
    import hashlib
    browser_key = hashlib.sha256(client.cookies.get(COOKIE).encode()).hexdigest()
    app.state.database.put("suggestions:" + browser_key, {PAPER["id"]: {**PAPER, "doi": "10.1234/test"}})
    value = {**PAPER, "$type": COLLECTION, "doi": "10.1234/TEST"}
    value.pop("arxivId")
    network["records"][DID] = [{"uri": f"at://{DID}/{COLLECTION}/existing", "value": value}]
    result = client.post("/api/publications", json={"papers": [{"id": PAPER["id"]}]}, headers=headers(client))
    assert result.json()["results"][0]["status"] == "already-added"
    assert not [r for r in network["requests"] if r[1] == "POST"]


def test_partial_import_reports_successes_and_can_retry(setup):
    client, app, network, _ = setup
    login(client, network)
    import hashlib
    browser_key = hashlib.sha256(client.cookies.get(COOKIE).encode()).hexdigest()
    second = {**PAPER, "id": "arxiv:2606.99999", "arxivId": "2606.99999", "title": "Fail"}
    app.state.database.put("suggestions:" + browser_key, {PAPER["id"]: PAPER, second["id"]: second})
    payload = {"papers": [{"id": PAPER["id"]}, {"id": second["id"]}]}
    result = client.post("/api/publications", json=payload, headers=headers(client)).json()
    assert [r["status"] for r in result["results"]] == ["added", "failed"]
    result = client.post("/api/publications", json=payload, headers=headers(client)).json()
    assert [r["status"] for r in result["results"]] == ["already-added", "failed"]
    assert len(network["records"][DID]) == 1


def test_claim_links_matching_name_immediately(setup, monkeypatch):
    client, app, network, _ = setup
    async def profile(url, **kwargs):
        assert kwargs["params"] == {"repo": OTHER, "collection": "app.bsky.actor.profile", "rkey": "self"}
        return {"uri": f"at://{OTHER}/app.bsky.actor.profile/self", "value": {"displayName": "  JAY  "}}
    monkeypatch.setattr("backend.app.get_json", profile)
    login(client, network, "jay.bsky.social")
    assert client.post("/api/claims", json={"researcherId": "bryce-hackel"}, headers=headers(client)).status_code == 409
    app.state.database.request_claim(OTHER, "jay.bsky.social", "jay")
    response = client.post("/api/claims", json={"researcherId": "jay"}, headers=headers(client))
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert client.get("/data/site.json").json()["researchers"][1]["did"] == OTHER
    assert client.get("/api/session").json()["researcher"]["id"] == "jay"
    assert client.post("/api/claims", json={"researcherId": "jay"}, headers=headers(client)).status_code == 409


@pytest.mark.parametrize("name", ["Bryce Hackel", "", None, "Jay Smith"])
def test_claim_rejects_nonmatching_or_missing_names(setup, monkeypatch, name):
    client, app, network, _ = setup
    async def profile(*args, **kwargs):
        return {"uri": f"at://{OTHER}/app.bsky.actor.profile/self", "value": {"displayName": name}}
    monkeypatch.setattr("backend.app.get_json", profile)
    login(client, network, "jay.bsky.social")
    assert client.post("/api/claims", json={"researcherId": "jay"}, headers=headers(client)).status_code == 403
    assert app.state.database.claims() == []


def test_claim_rejects_duplicate_names(tmp_path, monkeypatch):
    config = copy.deepcopy(CONFIG)
    config["researchers"].append({"id": "other-jay", "name": " JAY ", "did": ""})
    network = {"requests": [], "records": {}, "revoked": []}
    async def profile(*args, **kwargs):
        return {"uri": f"at://{OTHER}/app.bsky.actor.profile/self", "value": {"displayName": "Jay"}}
    monkeypatch.setattr("backend.app.get_json", profile)
    app = create_app(directory=tmp_path, config=config, oauth_factory=lambda *args: FakeOAuth(*args, network))
    with TestClient(app, base_url=ORIGIN) as client:
        login(client, network, "jay.bsky.social")
        assert client.post("/api/claims", json={"researcherId": "jay"}, headers=headers(client)).status_code == 409
        assert app.state.database.claims() == []


def test_claim_does_not_link_when_profile_lookup_fails(setup, monkeypatch):
    client, app, network, _ = setup
    async def profile(*args, **kwargs):
        raise OSError("PDS unavailable")
    monkeypatch.setattr("backend.app.get_json", profile)
    login(client, network, "jay.bsky.social")
    assert client.post("/api/claims", json={"researcherId": "jay"}, headers=headers(client)).status_code == 502
    assert app.state.database.claims() == []


def test_claim_storage_protects_both_account_and_profile(tmp_path):
    database = Database(tmp_path)
    database.claim_by_name(OTHER, "jay.bsky.social", "jay")
    with pytest.raises(ValueError):
        database.claim_by_name(DID, "another.bsky.social", "jay")
    with pytest.raises(ValueError):
        database.claim_by_name(OTHER, "jay.bsky.social", "another-profile")
    assert Database(tmp_path).claims()[0]["did"] == OTHER


def test_name_normalization():
    assert normalized_name("  SOHAM\t Phadke ") == normalized_name("Soham Phadke")
    assert normalized_name("Jose\u0301") == normalized_name("Jos\u00e9")
    assert normalized_name("Jay Smith") != normalized_name("Jay")


@pytest.mark.parametrize("value", ["10.1109/GHTC55712.2022.9910606", "doi:10.1109/GHTC55712.2022.9910606",
                                    "https://doi.org/10.1109/GHTC55712.2022.9910606"])
def test_doi_inputs(value):
    assert normalize_doi(value) == "10.1109/ghtc55712.2022.9910606"


@pytest.mark.parametrize("value", ["https://evil.example/10.1234/test", "not a DOI", "10.1234/a b",
                                    "https://doi.org@evil.example/10.1234/test"])
def test_doi_rejects_invalid_inputs(value):
    with pytest.raises(ValueError):
        normalize_doi(value)


def test_crossref_metadata():
    data = {"DOI": "10.1234/TEST", "title": ["A paper"], "author": [{"given": "Soham", "family": "Phadke"}],
            "published": {"date-parts": [[2022]]}, "container-title": ["Conference"]}
    paper = parse_crossref(data, "10.1234/test")
    assert paper["authors"] == ["Soham Phadke"]
    assert paper["publishedAt"] == "2022-01-01T00:00:00Z"
    assert paper["doi"] == "10.1234/test"
    with pytest.raises(ValueError):
        parse_crossref(data, "10.1234/different")
    with pytest.raises(ValueError):
        parse_crossref({**data, "author": []}, "10.1234/test")


def test_doi_lookup_preview_then_publish_and_deduplicate(setup, monkeypatch):
    client, _, network, _ = setup
    paper = {**PAPER, "id": "doi:10.1234/test", "doi": "10.1234/test", "url": "https://doi.org/10.1234/test"}
    paper.pop("arxivId")
    async def lookup(doi):
        assert doi == "10.1234/test"
        return paper
    monkeypatch.setattr("backend.app.lookup_doi", lookup)
    assert client.post("/api/doi/lookup", json={"doi": "10.1234/test"}, headers=headers(client)).status_code == 401
    login(client, network)
    response = client.post("/api/doi/lookup", json={"doi": "https://doi.org/10.1234/TEST"}, headers=headers(client))
    assert response.status_code == 200
    assert response.json()["papers"][0]["id"] == paper["id"]
    assert network["records"] == {}  # Lookup does not publish.
    payload = {"papers": [{"id": paper["id"], "labIds": []}]}
    assert client.post("/api/publications", json=payload, headers=headers(client)).json()["results"][0]["status"] == "added"
    assert network["records"][DID][0]["value"]["doi"] == "10.1234/test"
    assert client.post("/api/publications", json=payload, headers=headers(client)).json()["results"][0]["status"] == "already-added"


@pytest.mark.parametrize("error,status", [(LookupError("Not found"), 404), (ValueError("Missing metadata"), 422), (OSError(), 502)])
def test_doi_lookup_errors(setup, monkeypatch, error, status):
    client, _, network, _ = setup
    async def lookup(doi):
        raise error
    monkeypatch.setattr("backend.app.lookup_doi", lookup)
    login(client, network)
    assert client.post("/api/doi/lookup", json={"doi": "10.1234/test"}, headers=headers(client)).status_code == status


def test_competing_claims_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    database = Database(tmp_path)
    def claim(did):
        try:
            database.claim_by_name(did, "jay.bsky.social", "jay")
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, [DID, OTHER])) == [False, True]
    assert len(database.claims()) == 1


def test_store_survives_restart_and_expires_states(tmp_path):
    database = Database(tmp_path)
    database.put("test", {"token": "do-not-store-plainly"})
    assert Database(tmp_path).get("test") == {"token": "do-not-store-plainly"}
    assert b"do-not-store-plainly" not in database.path.read_bytes()
    database.put("expired", {}, ttl=-1)
    assert database.get("expired") is None
    assert database.get("test", consume=True)
    assert database.get("test") is None


def test_arxiv_parser_normalizes_versions_and_rejects_xml_entities():
    xml = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/2606.14083v2</id><title>A\n paper</title>
    <author><name>Bryce Hackel</name></author><published>2026-06-12T00:00:00Z</published>
    <summary>Text</summary></entry></feed>'''
    paper = parse_arxiv(xml)[0]
    assert paper["arxivId"] == "2606.14083"
    assert paper["title"] == "A paper"
    assert paper["labIds"] == []
    with pytest.raises(Exception):
        parse_arxiv('<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><feed>&xxe;</feed>')


def test_refresh_rejects_changed_identity_before_persisting(monkeypatch, tmp_path):
    from atproto_oauth import OAuthClient
    from backend.oauth import make_client

    async def response(self, *args, **kwargs):
        return "nonce", httpx.Response(200, json={"sub": OTHER})

    monkeypatch.setattr(OAuthClient, "_make_token_request", response)
    client = make_client(Database(tmp_path), "browser", ORIGIN)
    token = refresh_did.set(DID)
    try:
        with pytest.raises(Exception, match="identity changed"):
            asyncio.run(client._make_token_request(params={"grant_type": "refresh_token"}))
    finally:
        refresh_did.reset(token)


def test_combined_search_returns_crossref_when_arxiv_fails(setup, monkeypatch):
    client, app, network, searcher = setup
    async def unavailable(query):
        raise RuntimeError("offline")
    async def crossref(query, field):
        assert query == "Soham Phadke"
        return [{**PAPER, "authors": ["Soham Phadke"]}]
    monkeypatch.setattr(searcher, "search_all", unavailable, raising=False)
    monkeypatch.setattr("backend.app.search_crossref", crossref)
    login(client, network)
    response = client.post("/api/papers/search", json={"query": "Soham Phadke"}, headers=headers(client))
    assert response.status_code == 200
    assert len(response.json()["papers"]) == 1
    assert "arXiv" in response.json()["warning"]
    assert not network["records"]
    async def failed(query, field):
        raise RuntimeError("offline")
    monkeypatch.setattr("backend.app.search_crossref", failed)
    monkeypatch.setattr("backend.app.search_openalex", failed)
    monkeypatch.setattr("backend.app.search_datacite", unavailable)
    assert client.post("/api/papers/search", json={"query": "Soham Phadke"}, headers=headers(client)).status_code == 502


def test_combined_doi_and_versioned_account_script(setup, monkeypatch):
    import hashlib
    from backend.app import ROOT
    client, _, network, _ = setup
    async def lookup(doi):
        assert doi == "10.1234/test"
        return {**PAPER, "doi": doi}
    monkeypatch.setattr("backend.app.lookup_doi", lookup)
    login(client, network)
    response = client.post("/api/papers/search", json={"query": "https://doi.org/10.1234/test"}, headers=headers(client))
    assert response.status_code == 200 and response.json()["kind"] == "doi"
    page = client.get("/account/")
    version = hashlib.sha256((ROOT / "web/account.js").read_bytes()).hexdigest()[:16]
    assert '/assets/account.js?v=' + version in page.text
    assert page.headers["cache-control"] == "no-store"
    assert client.get('/assets/account.js?v=' + version).headers["cache-control"] == "no-store"


def test_search_ranking_filters_unrelated_and_deduplicates():
    from backend.papers import rank_papers
    exact = {**PAPER, "authors": ["Soham Phadke"], "doi": "10.1234/test"}
    duplicate = {**exact, "arxivId": "", "url": "https://doi.org/10.1234/test"}
    assert rank_papers("Soham Phadke", [PAPER, exact, duplicate]) == [exact]
    assert rank_papers("N2D", [exact]) == [exact]


def test_profile_and_lab_go_live_without_rebuild_and_survive_restart(setup):
    client, app, network, _ = setup
    assert client.put("/api/profile", json={"name": "New Researcher"}, headers=headers(client)).status_code == 401
    login(client, network, "new.bsky.social")
    lab_payload = {"name": "Live Lab", "institution": "University", "professorName": "Professor Person"}
    response = client.post("/api/labs", json=lab_payload, headers=headers(client))
    assert response.status_code == 200
    lab = response.json()["lab"]
    assert client.post("/api/labs", json=lab_payload, headers=headers(client)).status_code == 409
    response = client.put("/api/profile", json={"name": "New <Researcher>", "institution": "University", "labIds": [lab["id"]]}, headers=headers(client))
    assert response.status_code == 200
    researcher = response.json()["researcher"]
    assert researcher["did"] == OTHER
    profile_id = researcher["id"]
    assert client.get("/api/session").json()["researcher"]["id"] == profile_id
    assert profile_id in client.get("/researchers/").text
    assert lab["id"] in client.get("/labs/").text
    page = client.get("/researchers/" + profile_id + "/")
    assert page.status_code == 200 and "New &lt;Researcher&gt;" in page.text
    assert "{{" not in page.text
    lab_page = client.get("/labs/" + lab["id"] + "/")
    assert "New &lt;Researcher&gt;" in lab_page.text and "{{" not in lab_page.text
    assert client.get("/researchers/missing/").status_code == 404
    assert client.get("/labs/missing/").status_code == 404
    restarted = create_app(directory=app.state.database.path.parent, config=copy.deepcopy(CONFIG), catalog=[])
    with TestClient(restarted, base_url=ORIGIN) as fresh:
        directory = fresh.get("/data/site.json").json()
        assert next(r for r in directory["researchers"] if r["id"] == profile_id)["name"] == "New <Researcher>"
        assert next(l for l in directory["labs"] if l["id"] == lab["id"])["members"] == [profile_id]
    assert client.put("/api/profile", json={"name": "Changed Name", "labIds": []}, headers=headers(client)).status_code == 200
    directory = client.get("/data/site.json").json()
    assert len([r for r in directory["researchers"] if r.get("did") == OTHER]) == 1
    assert next(l for l in directory["labs"] if l["id"] == lab["id"])["members"] == []
    assert client.put("/api/profile", json={"name": "Spoof", "did": DID}, headers=headers(client)).status_code == 422


def test_lab_tag_requires_professor_even_for_direct_api_request(setup):
    client, _, network, _ = setup
    login(client, network)
    labs = []
    for name in ["Bryce Hackel", "Michael Yip"]:
        lab = client.post("/api/labs", json={"name": name + " Lab", "institution": "University", "professorName": name}, headers=headers(client)).json()["lab"]
        labs.append(lab)
    client.get("/api/suggestions")
    payload = {"papers": [{"id": PAPER["id"], "labIds": [labs[1]["id"]]}]}
    denied = client.post("/api/publications", json=payload, headers=headers(client))
    assert denied.status_code == 400 and "professor" in denied.json()["detail"]
    assert not network["records"]
    payload["papers"][0]["labIds"] = [labs[0]["id"]]
    assert client.post("/api/publications", json=payload, headers=headers(client)).status_code == 200
    assert network["records"][DID][0]["value"]["labIds"] == [labs[0]["id"]]


def test_directory_storage_prevents_other_owner_overwrite(tmp_path):
    database = Database(tmp_path)
    database.save_directory_entry("labs", "lab", DID, {"name": "Original"})
    with pytest.raises(ValueError):
        database.save_directory_entry("labs", "lab", OTHER, {"name": "Changed"})
    assert database.directory_entries("labs") == [{"name": "Original"}]


def test_new_sources_normalize_authors_and_identifiers():
    from backend.papers import parse_openalex, parse_datacite, rank_papers
    paper = parse_openalex({"id": "https://openalex.org/W123", "title": "Test paper", "doi": "https://doi.org/10.1234/TEST",
        "publication_date": "2024-04-10", "authorships": [{"author": {"display_name": "Example Author"}}],
        "primary_location": {"landing_page_url": "https://arxiv.org/abs/2409.07786v2"}})
    assert paper["arxivId"] == "2409.07786" and paper["doi"] == "10.1234/test"
    item = {"attributes": {"doi": "10.1234/test", "titles": [{"title": "Test paper"}], "publicationYear": 2024,
        "creators": [{"name": "Author, Example", "nameType": "Personal"}], "types": {"resourceTypeGeneral": "Text", "resourceType": "Poster"}}}
    poster = parse_datacite(item)
    assert poster["authors"] == ["Example Author"] and poster["source"] == "datacite"
    assert rank_papers("Example Author", [paper, poster]) == [paper]
    item["attributes"]["types"]["resourceTypeGeneral"] = "Dataset"
    with pytest.raises(ValueError):
        parse_datacite(item)


def test_new_source_results_can_be_added_to_publications(setup, monkeypatch):
    client, _, network, searcher = setup
    async def empty(*args):
        return []
    async def datacite(query):
        return [{"title": "Example Poster", "authors": ["Example Author"], "doi": "10.1234/poster",
                 "url": "https://doi.org/10.1234/poster", "publishedAt": "2024-01-01T00:00:00Z", "source": "datacite"}]
    monkeypatch.setattr(searcher, "search_all", empty, raising=False)
    monkeypatch.setattr("backend.app.search_crossref", empty)
    monkeypatch.setattr("backend.app.search_datacite", datacite)
    login(client, network)
    response = client.post("/api/papers/search", json={"query": "Example Author"}, headers=headers(client))
    assert response.status_code == 200 and response.json()["warning"] == ""
    result = response.json()["papers"][0]
    assert result["id"] == "doi:10.1234/poster"
    response = client.post("/api/publications", json={"papers": [{"id": result["id"]}]}, headers=headers(client))
    assert response.status_code == 200
    assert network["records"][DID][0]["value"]["title"] == "Example Poster"


def test_doi_falls_back_to_datacite(monkeypatch):
    from backend import papers
    calls = []
    def handle(request):
        calls.append(request.url.host)
        if request.url.host == "api.crossref.org":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": {"attributes": {"doi": "10.1234/poster",
            "titles": [{"title": "Research poster"}], "publicationYear": 2024,
            "creators": [{"name": "Example Author"}], "types": {"resourceTypeGeneral": "Text"}}}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(papers.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(papers.lookup_doi("10.1234/poster"))
    assert result["source"] == "datacite" and calls == ["api.crossref.org", "api.datacite.org"]



def test_search_author_choice_creates_profile_without_manual_save(setup, monkeypatch):
    client, app, network, searcher = setup
    async def results(query):
        return [{**PAPER, "authors": ["Alex Whelan", "Soham Phadke"]}]
    monkeypatch.setattr(searcher, "search", results)
    login(client, network, "soham.bsky.social")
    assert client.get("/api/session").json()["researcher"] is None
    assert all(r.get("did") != OTHER for r in client.get("/data/site.json").json()["researchers"])
    response = client.post("/api/arxiv/search", json={"author": "Soham"}, headers=headers(client))
    paper = response.json()["papers"][0]
    rejected = client.post("/api/profile/author", json={"paperId": paper["id"], "name": "Invented Name"}, headers=headers(client))
    assert rejected.status_code == 400
    response = client.post("/api/profile/author", json={"paperId": paper["id"], "name": "Soham Phadke"}, headers=headers(client))
    assert response.status_code == 200
    researcher = response.json()["researcher"]
    assert researcher["name"] == "Soham Phadke" and researcher["did"] == OTHER
    assert "Soham Phadke" in client.get("/researchers/").text
    assert client.get("/api/session").json()["researcher"]["id"] == researcher["id"]
    assert client.get("/researchers/" + researcher["id"] + "/").status_code == 200


def test_automatic_placeholder_is_hidden_until_author_selected(setup):
    client, app, network, _ = setup
    login(client, network, "soham.bsky.social")
    app.state.database.save_directory_entry("researchers", "old-auto", OTHER,
        {"id": "old-auto", "name": "soham.bsky.social", "did": OTHER, "autoCreated": True})
    assert client.get("/api/session").json()["researcher"] is None
    assert "soham.bsky.social" not in client.get("/researchers/").text


def test_remove_own_profile_preserves_other_accounts_and_pds_records(setup):
    client, app, network, _ = setup
    assert client.delete("/api/profile", headers=headers(client)).status_code == 401
    login(client, network, "alt.bsky.social")
    profile = client.put("/api/profile", json={"name": "Soham Phadke"}, headers=headers(client)).json()["researcher"]
    network["records"][OTHER] = [{"uri": f"at://{OTHER}/{COLLECTION}/existing", "value": {**PAPER, "$type": COLLECTION}}]
    before = copy.deepcopy(network["records"])
    assert client.delete("/api/profile", headers={"Origin": ORIGIN}).status_code == 403
    assert client.delete("/api/profile", headers=headers(client)).status_code == 200
    assert client.get("/api/session").json()["researcher"] is None
    directory = client.get("/data/site.json").json()
    assert any(r.get("did") == DID for r in directory["researchers"])
    assert all(r.get("did") != OTHER for r in directory["researchers"])
    assert client.get("/researchers/" + profile["id"] + "/").status_code == 404
    assert network["records"] == before
    assert not any(request[2].endswith("deleteRecord") for request in network["requests"])
    assert client.get("/api/publications").status_code == 200
    paper = client.post("/api/arxiv/search", json={"author": "Bryce"}, headers=headers(client)).json()["papers"][0]
    chosen = client.post("/api/profile/author", json={"name": "Bryce Hackel", "paperId": paper["id"]}, headers=headers(client))
    assert chosen.status_code == 200
    assert chosen.json()["researcher"]["did"] == OTHER


def test_removed_seeded_profile_stays_removed_after_restart(setup):
    client, app, network, _ = setup
    login(client, network)
    assert client.delete("/api/profile", headers=headers(client)).status_code == 200
    assert client.get("/api/session").json()["researcher"] is None
    restarted = create_app(directory=app.state.database.path.parent, config=copy.deepcopy(CONFIG), catalog=[])
    with TestClient(restarted, base_url=ORIGIN) as fresh:
        directory = fresh.get("/data/site.json").json()
        assert all(r["id"] != "bryce-hackel" for r in directory["researchers"])
        assert directory["labs"][0]["members"] == []
        assert fresh.get("/researchers/bryce-hackel/").status_code == 404


def test_remove_profile_releases_legacy_claim(setup):
    client, app, network, _ = setup
    login(client, network, "jay.bsky.social")
    app.state.database.claim_by_name(OTHER, "jay.bsky.social", "jay")
    assert client.delete("/api/profile", headers=headers(client)).status_code == 200
    assert app.state.database.claims() == []
    assert client.get("/api/session").json()["researcher"] is None


def test_remove_all_is_scoped_to_signed_in_account_and_retries_failures(setup, monkeypatch):
    client, _, network, _ = setup
    assert client.delete("/api/publications", headers=headers(client)).status_code == 401
    login(client, network)
    def record(did, key):
        return {"uri": f"at://{did}/{COLLECTION}/{key}", "value": {**PAPER, "$type": COLLECTION}}
    network["records"] = {DID: [record(DID, "one"), record(DID, "two")], OTHER: [record(OTHER, "other")]}
    original = FakeOAuth.make_authenticated_request
    fail = True
    async def request(self, session, method, url, **kwargs):
        if fail and url.endswith("deleteRecord") and kwargs["json"]["rkey"] == "two":
            return httpx.Response(503, request=httpx.Request(method, url))
        return await original(self, session, method, url, **kwargs)
    monkeypatch.setattr(FakeOAuth, "make_authenticated_request", request)
    assert client.delete("/api/publications", headers={"Origin": ORIGIN}).status_code == 403
    response = client.delete("/api/publications", headers=headers(client))
    assert response.json() == {"removed": 1, "failed": 1}
    assert len(network["records"][DID]) == 1 and len(network["records"][OTHER]) == 1
    assert client.get("/api/session").json()["researcher"]["id"] == "bryce-hackel"
    fail = False
    assert client.delete("/api/publications", headers=headers(client)).json() == {"removed": 1, "failed": 0}
    assert network["records"][DID] == [] and len(network["records"][OTHER]) == 1
    assert client.delete("/api/publications", headers=headers(client)).json() == {"removed": 0, "failed": 0}


def test_remove_all_does_not_delete_when_listing_fails(setup, monkeypatch):
    client, _, network, _ = setup
    login(client, network)
    async def unavailable(*args):
        raise RuntimeError("offline")
    monkeypatch.setattr("backend.app.list_papers", unavailable)
    assert client.delete("/api/publications", headers=headers(client)).status_code == 502
    assert not network["requests"]
