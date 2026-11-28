import asyncio
import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import COOKIE, create_app, normalized_name
from backend.oauth import ArchiveOAuthClient, refresh_did
from backend.papers import COLLECTION, parse_arxiv, normalize_doi, normalize_orcid, parse_crossref
from backend.storage import Database
from tests.support import DID, OTHER, ORIGIN, PAPER, FakeOAuth, FakeSearch

CONFIG = {"collection": COLLECTION, "researchers": [
    {"id": "bryce-hackel", "name": "Bryce Hackel", "did": DID, "handle": "bhackel.bsky.social"},
    {"id": "jay", "name": "Jay", "did": "", "handle": ""},
], "labs": [{"id": "ucsd-arc", "name": "ARC", "members": ["bryce-hackel"]}]}


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
    app = create_app(directory=tmp_path, searcher=searcher, oauth_factory=lambda *args: FakeOAuth(*args, network))
    app.state.database.save_directory_entry("researchers", "bryce-hackel", DID, copy.deepcopy(CONFIG["researchers"][0]))
    app.state.database.create_lab("ucsd-arc", DID, copy.deepcopy(CONFIG["labs"][0]))
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app, network, searcher


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/session").json()["csrf"]}


def login(client, network, handle="bhackel.bsky.social"):
    response = client.post("/auth/login", json={"handle": handle}, headers=headers(client))
    assert response.status_code == 200
    response = client.get("/auth/callback", params={"code": "test-code", "state": network["state"], "iss": "https://bsky.social"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/account/"
    # Explicit test-only suggestion, never a production catalog.
    import hashlib
    key = hashlib.sha256(client.cookies.get(COOKIE).encode()).hexdigest()
    client.app.state.database.put("suggestions:" + key, {PAPER["id"]: copy.deepcopy(PAPER)})


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
    payload = {"papers": [{"id": PAPER["id"]}]}
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
    payload = {"papers": [{"id": paper["id"]}]}
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
    assert client.put("/api/profile", json={"name": "New Researcher"}, headers=headers(client)).status_code == 200
    lab_payload = {"name": "Live Lab", "institution": "University"}
    response = client.post("/api/labs", json=lab_payload, headers=headers(client))
    assert response.status_code == 200
    lab = response.json()["lab"]
    assert client.post("/api/labs", json=lab_payload, headers=headers(client)).status_code == 409
    response = client.put("/api/profile", json={"name": "New <Researcher>", "institution": "University"}, headers=headers(client))
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
    restarted = create_app(directory=app.state.database.path.parent)
    with TestClient(restarted, base_url=ORIGIN) as fresh:
        directory = fresh.get("/data/site.json").json()
        assert next(r for r in directory["researchers"] if r["id"] == profile_id)["name"] == "New <Researcher>"
        assert next(l for l in directory["labs"] if l["id"] == lab["id"])["members"] == [profile_id]
    assert client.put("/api/profile", json={"name": "Changed Name"}, headers=headers(client)).status_code == 200
    directory = client.get("/data/site.json").json()
    assert len([r for r in directory["researchers"] if r.get("did") == OTHER]) == 1
    assert next(l for l in directory["labs"] if l["id"] == lab["id"])["members"] == [profile_id]  # Profile fields cannot change managed membership.
    assert client.put("/api/profile", json={"name": "Spoof", "did": DID}, headers=headers(client)).status_code == 422


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
    rejected = client.post("/api/profile/author", json={"paperId": paper["id"], "name": "Invented Name", "index": 1}, headers=headers(client))
    assert rejected.status_code == 400
    response = client.post("/api/profile/author", json={"paperId": paper["id"], "name": "Soham Phadke", "index": 1}, headers=headers(client))
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
    chosen = client.post("/api/profile/author", json={"name": "Bryce Hackel", "paperId": paper["id"], "index": 0}, headers=headers(client))
    assert chosen.status_code == 200
    assert chosen.json()["researcher"]["did"] == OTHER


def test_deleted_profile_stays_removed_after_restart(setup):
    client, app, network, _ = setup
    login(client, network)
    assert client.delete("/api/profile", headers=headers(client)).status_code == 200
    assert client.get("/api/session").json()["researcher"] is None
    restarted = create_app(directory=app.state.database.path.parent)
    with TestClient(restarted, base_url=ORIGIN) as fresh:
        directory = fresh.get("/data/site.json").json()
        assert all(r["id"] != "bryce-hackel" for r in directory["researchers"])
        assert directory["labs"][0]["members"] == []
        assert fresh.get("/researchers/bryce-hackel/").status_code == 404


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


def test_lab_invitation_membership_and_permissions(setup):
    client, app, network, _ = setup
    login(client, network)
    lab = client.post("/api/labs", json={"name": "Member Lab", "institution": "University", "about": "Our research", "contactEmail": "lab@example.org"}, headers=headers(client)).json()["lab"]
    base = "/api/labs/" + lab["id"]
    assert client.get("/api/my-labs").json()["labs"][0]["role"] == "owner"
    token = client.post(base + "/invite", headers=headers(client)).json()["url"].split("invite=")[1]
    assert client.get("/api/lab-invites/" + token).json()["lab"]["name"] == "Member Lab"
    owner_headers = headers(client)
    with TestClient(app, base_url=ORIGIN) as other:
        login(other, network, "other.bsky.social")
        assert other.post(base + "/join", json={"token": token}, headers=headers(other)).status_code == 400
        # A profile's old labIds field cannot bypass invitation membership.
        profile = other.put("/api/profile", json={"name": "Other Author"}, headers=headers(other)).json()["researcher"]
        def members():
            return next(l for l in client.get("/data/site.json").json()["labs"] if l["id"] == lab["id"])["members"]
        assert profile["id"] not in members()
        assert other.post(base + "/invite", headers=headers(other)).status_code == 403
        assert other.post(base + "/join", json={"token": "invalid-token-that-is-long"}, headers=headers(other)).status_code == 403
        assert other.post(base + "/join", json={"token": token}, headers=headers(other)).status_code == 200
        assert profile["id"] in members()
        change = {"did": OTHER, "action": "admin"}
        assert other.post(base + "/members", json=change, headers=headers(other)).status_code == 403
        assert client.post(base + "/members", json=change, headers=owner_headers).status_code == 200
        assert other.get("/api/my-labs").json()["labs"][0]["role"] == "admin"
        assert other.post(base + "/members", json={"did": DID, "action": "remove"}, headers=headers(other)).status_code == 403
        rotated = other.post(base + "/invite", headers=headers(other)).json()["url"].split("invite=")[1]
        assert client.get("/api/lab-invites/" + token).status_code == 404
        assert other.post(base + "/members", json={"did": OTHER, "action": "remove"}, headers=headers(other)).status_code == 200
        assert profile["id"] not in members()
        assert other.post(base + "/join", json={"token": rotated}, headers=headers(other)).status_code == 200
        assert client.post(base + "/members", json={"did": OTHER, "action": "remove"}, headers=owner_headers).status_code == 200
        assert profile["id"] not in members()
        assert other.post(base + "/invite", headers=headers(other)).status_code == 403
    assert client.post(base + "/members", json={"did": DID, "action": "remove"}, headers=owner_headers).status_code == 403
    assert "lab_invites" not in str(client.get("/data/site.json").json())
    restarted = Database(app.state.database.path.parent)
    assert restarted.lab_members(lab["id"]) == [{"lab_id": lab["id"], "did": DID, "role": "owner"}]


@pytest.mark.parametrize("value,expected", [
    ("0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("orcid.org/0000000218250097/", "0000-0002-1825-0097"),
    (" 0000-0002-1694-233x ", "0000-0002-1694-233X"),
])
def test_orcid_inputs(value, expected):
    assert normalize_orcid(value) == expected


@pytest.mark.parametrize("value", ["", "1234", "0000-0002-1825-0098", "0000-0002-1825-009",
                                   "https://example.com/0000-0002-1825-0097", "0000-0002-1825-00970", None])
def test_orcid_rejects_invalid_inputs(value):
    with pytest.raises(ValueError):
        normalize_orcid(value)


def test_orcid_is_saved_shown_removed_and_unique(setup):
    client, app, network, _ = setup
    assert client.put("/api/profile/orcid", json={"orcid": "0000-0002-1825-0097"}, headers=headers(client)).status_code == 401
    login(client, network)
    response = client.put("/api/profile/orcid", json={"orcid": "https://orcid.org/0000-0002-1825-0097"}, headers=headers(client))
    assert response.status_code == 200 and response.json()["researcher"]["orcid"] == "0000-0002-1825-0097"
    assert client.get("/api/session").json()["researcher"]["orcid"] == "0000-0002-1825-0097"
    page = client.get("/researchers/bryce-hackel/").text
    assert 'href="https://orcid.org/0000-0002-1825-0097"' in page and "self-provided, not verified" in page
    assert any(r.get("orcid") == "0000-0002-1825-0097" for r in client.get("/data/site.json").json()["researchers"])

    typo = client.put("/api/profile/orcid", json={"orcid": "0000-0002-1825-0098"}, headers=headers(client))
    assert typo.status_code == 400 and "typo" in typo.json()["detail"]
    assert client.get("/api/session").json()["researcher"]["orcid"] == "0000-0002-1825-0097"

    # Editing the rest of the profile keeps the ORCID iD.
    assert client.put("/api/profile", json={"name": "Bryce Hackel"}, headers=headers(client)).status_code == 200
    assert client.get("/api/session").json()["researcher"]["orcid"] == "0000-0002-1825-0097"

    removed = client.put("/api/profile/orcid", json={"orcid": ""}, headers=headers(client))
    assert removed.status_code == 200 and "orcid" not in removed.json()["researcher"]
    assert "orcid.org" not in client.get("/researchers/bryce-hackel/").text
    assert client.put("/api/profile/orcid", json={"orcid": "0000-0002-1825-0097"}, headers=headers(client)).status_code == 200

    # Another account needs its own profile first, and can't take an ORCID iD already in use.
    assert client.post("/auth/logout", headers=headers(client)).status_code == 200
    login(client, network, "alt.bsky.social")
    missing = client.put("/api/profile/orcid", json={"orcid": "0000-0002-1694-233X"}, headers=headers(client))
    assert missing.status_code == 400
    assert client.put("/api/profile", json={"name": "Other Person"}, headers=headers(client)).status_code == 200
    taken = client.put("/api/profile/orcid", json={"orcid": "0000-0002-1825-0097"}, headers=headers(client))
    assert taken.status_code == 409
    assert client.put("/api/profile/orcid", json={"orcid": "0000-0002-1694-233X"}, headers=headers(client)).status_code == 200


def test_orcid_search_finds_exact_papers_that_can_be_added(setup, monkeypatch):
    client, app, network, searcher = setup
    calls = []
    async def by_orcid(orcid):
        calls.append(orcid)
        return [{**PAPER, "title": "ORCID paper", "id": "arxiv:2606.55555", "arxivId": "2606.55555", "labIds": []}]
    async def unexpected(*args):
        raise AssertionError("name search should not run for an ORCID iD")
    monkeypatch.setattr("backend.app.search_orcid", by_orcid)
    monkeypatch.setattr("backend.app.search_crossref", unexpected)
    login(client, network)
    response = client.post("/api/papers/search", json={"query": "https://orcid.org/0000-0002-1825-0097"}, headers=headers(client))
    assert response.status_code == 200 and response.json()["kind"] == "orcid"
    assert calls == ["0000-0002-1825-0097"]
    paper = response.json()["papers"][0]
    added = client.post("/api/publications", json={"papers": [{"id": paper["id"]}]}, headers=headers(client))
    assert added.status_code == 200 and added.json()["results"][0]["status"] == "added"
    assert network["records"][DID][0]["value"]["title"] == "ORCID paper"


def test_orcid_search_uses_openalex_orcid_filter(monkeypatch):
    from backend import papers
    seen = []
    def handle(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"results": [{
            "title": "Helping Hand", "publication_date": "2026-11-11", "doi": "https://doi.org/10.1234/hand",
            "authorships": [{"author": {"display_name": "Michael C. Yip"}}],
            "primary_location": {"landing_page_url": "https://doi.org/10.1234/hand", "source": {"display_name": "ICRA"}}}]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(papers.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(papers.search_orcid("https://orcid.org/0000-0001-9689-0172"))
    assert seen[0]["filter"] == "author.orcid:0000-0001-9689-0172"
    assert result[0]["title"] == "Helping Hand" and result[0]["doi"] == "10.1234/hand"

def test_empty_install_profile_lab_invitation_and_restart(tmp_path):
    network = {"requests": [], "records": {}, "revoked": []}
    app = create_app(directory=tmp_path, oauth_factory=lambda *args: FakeOAuth(*args, network))
    with TestClient(app, base_url=ORIGIN) as owner, TestClient(app, base_url=ORIGIN) as member:
        directory = owner.get("/data/site.json").json()
        assert directory["researchers"] == [] and directory["labs"] == []
        assert owner.get("/labs/ucsd-arc/").status_code == 404
        assert "username.bsky.social" in owner.get("/account/").text
        login(owner, network)
        assert owner.get("/api/session").json()["researcher"] is None
        assert owner.post("/api/labs", json={"name": "No owner profile", "institution": "University"}, headers=headers(owner)).status_code == 400
        assert owner.get("/data/site.json").json()["labs"] == []
        profile = owner.put("/api/profile", json={"name": "Lab Owner", "institution": "University"}, headers=headers(owner)).json()["researcher"]
        lab = owner.post("/api/labs", json={"name": "New Lab", "institution": "University"}, headers=headers(owner)).json()["lab"]
        invitation = owner.post("/api/labs/" + lab["id"] + "/invite", headers=headers(owner)).json()
        from urllib.parse import urlparse, parse_qs
        token = parse_qs(urlparse(invitation["url"]).query)["invite"][0]
        login(member, network, "member.bsky.social")
        assert member.put("/api/profile", json={"name": "Member", "labIds": [lab["id"]]}, headers=headers(member)).status_code == 422
        member_profile = member.put("/api/profile", json={"name": "Member"}, headers=headers(member)).json()["researcher"]
        assert member.post("/api/labs/" + lab["id"] + "/join", json={"token": token}, headers=headers(member)).status_code == 200
        assert member.post("/api/claims", json={"researcherId": profile["id"]}, headers=headers(member)).status_code in (404, 405)
        assert sorted(owner.get("/data/site.json").json()["labs"][0]["members"]) == sorted([profile["id"], member_profile["id"]])
        assert owner.delete("/api/profile", headers=headers(owner)).status_code == 200
        # A hidden/deleted public profile must not destroy ownership or PDS records.
        assert owner.get("/api/my-labs").json()["labs"][0]["role"] == "owner"
        recreated = owner.put("/api/profile", json={"name": "Lab Owner"}, headers=headers(owner)).json()["researcher"]
        assert recreated["id"] == profile["id"]
    restarted = create_app(directory=tmp_path)
    with TestClient(restarted, base_url=ORIGIN) as fresh:
        directory = fresh.get("/data/site.json").json()
        assert len(directory["researchers"]) == 2 and len(directory["labs"]) == 1
        assert fresh.get("/labs/" + lab["id"] + "/").status_code == 200


def test_edit_lab_details_permissions_and_persistence(setup):
    owner, app, network, _ = setup
    login(owner, network)
    original = {"name": "Editable Lab", "institution": "University", "about": "Original", "contactEmail": "old@example.org"}
    lab = owner.post("/api/labs", json=original, headers=headers(owner)).json()["lab"]
    path = "/api/labs/" + lab["id"]
    public_path = "/labs/" + lab["id"] + "/"
    token = owner.post(path + "/invite", headers=headers(owner)).json()["url"].split("invite=")[1]
    changed = {"name": "Renamed Lab", "institution": "New University", "about": "Updated research", "contactEmail": "new@example.org"}

    with TestClient(app, base_url=ORIGIN) as anonymous, TestClient(app, base_url=ORIGIN) as other:
        assert anonymous.put(path, json=changed, headers=headers(anonymous)).status_code == 401
        login(other, network, "other.bsky.social")
        assert other.put(path, json=changed, headers=headers(other)).status_code == 403
        profile = other.put("/api/profile", json={"name": "Other Author"}, headers=headers(other)).json()["researcher"]
        assert other.post(path + "/join", json={"token": token}, headers=headers(other)).status_code == 200
        assert other.put(path, json=changed, headers=headers(other)).status_code == 403

        response = owner.put(path, json=changed, headers=headers(owner))
        assert response.status_code == 200
        assert response.json()["lab"]["id"] == lab["id"]
        assert owner.post(path + "/members", json={"did": OTHER, "action": "admin"}, headers=headers(owner)).status_code == 200
        final = {**changed, "about": "Admin updated research", "contactEmail": ""}
        assert other.put(path, json=final, headers=headers(other)).status_code == 200
        assert owner.get("/api/lab-invites/" + token).json()["lab"]["name"] == final["name"]
        assert owner.put("/api/labs/missing", json=final, headers=headers(owner)).status_code == 404
        assert owner.put(path, json={**final, "name": "  "}, headers=headers(owner)).status_code == 400
        assert owner.put(path, json={**final, "contactEmail": "invalid"}, headers=headers(owner)).status_code == 422
        assert owner.put(path, json={**final, "members": []}, headers=headers(owner)).status_code == 422
        assert owner.put(path, json=final, headers={"Origin": ORIGIN}).status_code == 403

        # The old name is available again, but another lab's name and
        # institution combination cannot be taken by an edit.
        replacement = owner.post("/api/labs", json=original, headers=headers(owner))
        assert replacement.status_code == 200
        assert replacement.json()["lab"]["id"] != lab["id"]
        assert owner.put(path, json=original, headers=headers(owner)).status_code == 409
        assert other.get("/api/my-labs").json()["labs"][0]["role"] == "admin"
        page = anonymous.get(public_path)
        assert page.status_code == 200
        assert final["name"] in page.text and final["institution"] in page.text

    restarted = create_app(directory=app.state.database.path.parent)
    with TestClient(restarted, base_url=ORIGIN) as fresh:
        saved = next(entry for entry in fresh.get("/data/site.json").json()["labs"] if entry["id"] == lab["id"])
        assert {key: saved[key] for key in final} == final
        assert saved["shortName"] == final["name"]
        assert set(saved["members"]) == {"bryce-hackel", profile["id"]}
        assert fresh.get(public_path).status_code == 200
        assert fresh.get("/api/lab-invites/" + token).json()["lab"]["id"] == lab["id"]


def test_same_name_researchers_are_distinguished_by_handle_in_directory(tmp_path):
    from backend.app import entry_detail, shared_names

    entries = [{"name": "Wei Zhang", "institution": "UCSD", "handle": "a.bsky.social"},
               {"name": "wei  zhang", "institution": "", "handle": "b.bsky.social"},
               {"name": "Sam Lee", "institution": "MIT", "handle": "c.bsky.social"}]
    shared = shared_names(entries)
    assert [entry_detail(entry, shared) for entry in entries] == ["UCSD · @a.bsky.social", "@b.bsky.social", "MIT"]


def same_name_papers():
    common = {key: value for key, value in PAPER.items() if key not in ("arxivId", "researcherIds")}
    return [
        {**common, "id": "doi:10.1/ucsd", "doi": "10.1/ucsd", "title": "Kidney imaging", "url": "https://doi.org/10.1/ucsd",
         "authors": ["Alex Whelan", "Wei Zhang"], "publishedAt": "2024-05-01T00:00:00Z",
         "authorDetails": [{"name": "Alex Whelan", "affiliation": ""}, {"name": "Wei Zhang", "affiliation": "UC San Diego"}]},
        {**common, "id": "doi:10.1/mit", "doi": "10.1/mit", "title": "Graph databases", "url": "https://doi.org/10.1/mit",
         "authors": ["Wei Zhang", "Priya Rao"], "publishedAt": "2021-02-01T00:00:00Z",
         "authorDetails": [{"name": "Wei Zhang", "affiliation": "MIT"}, {"name": "Priya Rao", "affiliation": ""}]},
    ]


def test_same_name_authors_keep_their_own_context_and_need_an_explicit_claim(setup, monkeypatch):
    client, _, network, searcher = setup
    async def results(query):
        return same_name_papers()
    async def empty(*args):
        return []
    monkeypatch.setattr(searcher, "search_all", results, raising=False)
    monkeypatch.setattr("backend.app.search_crossref", empty)
    login(client, network, "wei.bsky.social")
    data = client.post("/api/papers/search", json={"query": "Wei Zhang"}, headers=headers(client)).json()
    assert [(p["id"], p["authors"], [d["affiliation"] for d in p["authorDetails"]]) for p in data["papers"]] == [
        ("doi:10.1/ucsd", ["Alex Whelan", "Wei Zhang"], ["", "UC San Diego"]),
        ("doi:10.1/mit", ["Wei Zhang", "Priya Rao"], ["MIT", ""])]
    # A matching name on a different slot, or a wrong slot, is not a claim.
    for body in ({"paperId": "doi:10.1/ucsd", "name": "Wei Zhang", "index": 0},
                 {"paperId": "doi:10.1/ucsd", "name": "Wei Zhang", "index": 5},
                 {"paperId": "doi:10.1/mit", "name": "Alex Whelan", "index": 0}):
        assert client.post("/api/profile/author", json=body, headers=headers(client)).status_code == 400
    assert client.get("/api/session").json()["researcher"] is None
    chosen = client.post("/api/profile/author", json={"paperId": "doi:10.1/mit", "name": "Wei Zhang", "index": 0}, headers=headers(client))
    assert chosen.status_code == 200 and chosen.json()["researcher"]["did"] == OTHER


def test_two_accounts_with_the_same_name_stay_separate_researchers(setup, monkeypatch):
    client, app, network, searcher = setup
    async def results(query):
        return same_name_papers()
    async def empty(*args):
        return []
    monkeypatch.setattr(searcher, "search_all", results, raising=False)
    monkeypatch.setattr("backend.app.search_crossref", empty)
    login(client, network, "wei.bsky.social")
    client.post("/api/papers/search", json={"query": "Wei Zhang"}, headers=headers(client))
    first = client.post("/api/profile/author", json={"paperId": "doi:10.1/ucsd", "name": "Wei Zhang", "index": 1}, headers=headers(client)).json()["researcher"]
    app.state.database.save_directory_entry("researchers", "other-wei", DID + "-2", {
        "id": "other-wei", "name": "wei  zhang", "did": DID + "-2", "handle": "wei2.bsky.social", "institution": ""})
    directory = client.get("/data/site.json").json()["researchers"]
    assert {r["id"] for r in directory if normalized_name(r["name"]) == "wei zhang"} == {first["id"], "other-wei"}
    assert "@wei.bsky.social" in client.get("/researchers/").text and "@wei2.bsky.social" in client.get("/researchers/").text


def test_parsers_keep_author_affiliations():
    from backend.papers import parse_datacite, parse_openalex
    crossref = parse_crossref({"DOI": "10.1234/test", "title": ["T"], "published": {"date-parts": [[2024, 1, 2]]},
        "author": [{"given": "Wei", "family": "Zhang", "affiliation": [{"name": "UCSD"}, {"name": "UCSD"}, {"name": "Salk"}]},
                   {"given": "Al", "family": "B", "affiliation": []}]}, "10.1234/test")
    assert crossref["authorDetails"] == [{"name": "Wei Zhang", "affiliation": "UCSD; Salk"}, {"name": "Al B", "affiliation": ""}]
    openalex = parse_openalex({"id": "https://openalex.org/W1", "title": "T", "publication_date": "2024-01-02",
        "doi": "https://doi.org/10.1234/x", "authorships": [
            {"author": {"display_name": "Wei Zhang"}, "institutions": [{"display_name": "MIT"}]},
            {"author": {"display_name": "Al B"}, "institutions": [], "raw_affiliation_strings": ["Somewhere Lab"]}]})
    assert [d["affiliation"] for d in openalex["authorDetails"]] == ["MIT", "Somewhere Lab"]
    datacite = parse_datacite({"attributes": {"doi": "10.1234/p", "titles": [{"title": "T"}], "publicationYear": 2024,
        "types": {"resourceTypeGeneral": "Text"}, "creators": [
            {"name": "Zhang, Wei", "affiliation": ["Stanford"]}, {"name": "Al B", "affiliation": [{"name": "CMU"}]}]}})
    assert [(d["name"], d["affiliation"]) for d in datacite["authorDetails"]] == [("Wei Zhang", "Stanford"), ("Al B", "CMU")]
    xml = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
      <id>http://arxiv.org/abs/2401.00001v1</id><title>T</title><summary>S</summary><published>2024-01-02T00:00:00Z</published>
      <author><name>Wei Zhang</name><arxiv:affiliation>Caltech</arxiv:affiliation></author><author><name>Al B</name></author></entry></feed>"""
    assert [d["affiliation"] for d in parse_arxiv(xml)[0]["authorDetails"]] == ["Caltech", ""]


def test_duplicate_papers_prefer_the_copy_with_affiliations():
    from backend.papers import rank_papers
    bare = {"title": "T", "authors": ["Wei Zhang"], "doi": "10.1/x", "url": "https://doi.org/10.1/x"}
    rich = {**bare, "authorDetails": [{"name": "Wei Zhang", "affiliation": "MIT"}]}
    assert rank_papers("Wei Zhang", [bare, rich])[0] is rich
