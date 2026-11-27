"""FastAPI app: login, live profiles, paper suggestions, and PDS writes."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .oauth import client_metadata, make_client
from .network import get_json
from .papers import COLLECTION, ArxivSearch, list_papers, make_record, paper_key, paper_keys, record_key, xrpc
from .storage import Database, OAuthStore, normalized_name
from .papers import normalize_doi, lookup_doi, search_crossref, rank_papers, search_openalex, search_datacite
from .papers import normalize_orcid, search_orcid

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "open_archive_session"


def shared_names(entries):
    """Normalized names used by more than one entry in the same list."""
    seen, shared = set(), set()
    for entry in entries:
        name = normalized_name(entry.get("name"))
        (shared if name in seen else seen).add(name)
    return shared


def entry_detail(entry, shared):
    """Institution, plus the account handle when another listed entry has the same name."""
    parts = [entry.get("institution", "")]
    if normalized_name(entry.get("name")) in shared and entry.get("handle"):
        parts.append("@" + entry["handle"])
    return " · ".join(part for part in parts if part)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    handle: str = Field(min_length=3, max_length=253, pattern=r"^@?[a-zA-Z0-9.:-]+$")


class Search(Input):
    author: str = Field(min_length=2, max_length=120, pattern=r"^[\w .,'’\-]+$")


class PaperSearch(Input):
    query: str = Field(min_length=2, max_length=1000)


class DoiLookup(Input):
    doi: str = Field(min_length=1, max_length=1000)


class ResearcherProfile(Input):
    name: str = Field(min_length=2, max_length=120)
    institution: str = Field(default="", max_length=200)


class OrcidChoice(Input):
    orcid: str = Field(max_length=100)  # Empty removes the ORCID iD.


class AuthorChoice(Input):
    paperId: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=120)
    index: int = Field(ge=0, lt=1000)


class LabProfile(Input):
    name: str = Field(min_length=2, max_length=200)
    institution: str = Field(min_length=2, max_length=200)
    about: str = Field(default="", max_length=2000)
    contactEmail: str = Field(default="", max_length=254, pattern=r"^(?:[^\s@]+@[^\s@]+\.[^\s@]+)?$")


class LabJoin(Input):
    token: str = Field(min_length=20, max_length=100)


class LabMemberChange(Input):
    did: str = Field(min_length=1, max_length=253)
    action: str = Field(pattern=r"^(remove|admin|member)$")


class Selection(Input):
    id: str = Field(max_length=500)


class Import(Input):
    papers: list[Selection] = Field(min_length=1, max_length=20)


def create_app(*, directory=None, origin=None, oauth_factory=make_client, searcher=None):
    origin = (origin or os.environ.get("OPEN_ARCHIVE_ORIGIN") or (
        "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        else "http://127.0.0.1:8000")).rstrip("/")
    parsed = urlparse(origin)
    secure = parsed.scheme == "https"
    cookie_name = "__Host-" + COOKIE if secure else COOKIE
    if os.environ.get("RAILWAY_ENVIRONMENT_ID") and not secure:
        raise ValueError("Create the Railway public domain before deploying, or set OPEN_ARCHIVE_ORIGIN.")
    if (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.hostname
            or not (secure or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"))):
        raise ValueError("OPEN_ARCHIVE_ORIGIN must be an HTTPS origin, or http://127.0.0.1:<port> locally.")
    state_dir = directory or os.environ.get("OPEN_ARCHIVE_STATE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if secure and not state_dir:
        raise ValueError("Hosted deployments require a persistent state directory.")
    if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if not mount or not Path(state_dir).resolve().is_relative_to(Path(mount).resolve()):
            raise ValueError("Attach a persistent Railway volume containing OPEN_ARCHIVE_STATE_DIR.")
    database = Database(state_dir or ROOT / ".local")
    if secure:
        client_metadata(database, origin)  # Create the signing key once at startup.
    searcher = searcher or ArxivSearch()
    # One worker; serialize each browser's token refreshes and writes.
    locks = [asyncio.Lock() for _ in range(64)]
    app = FastAPI(title="Open Archive", docs_url="/api/docs", redoc_url=None)
    app.state.database = database
    allowed_hosts = [parsed.hostname]
    if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        allowed_hosts.append("healthcheck.railway.app")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    rate_windows = {}

    def rate_limit(request, limit):
        now = time.monotonic()
        expired = [key for key, value in rate_windows.items() if value[0] <= now]
        for key in expired:
            rate_windows.pop(key)
        key = (request.client.host if request.client else "unknown", request.url.path)
        if key not in rate_windows:
            if len(rate_windows) >= 4096:
                return False
            rate_windows[key] = [now + 60, 0]
        rate_windows[key][1] += 1
        return rate_windows[key][1] <= limit

    def browser(request):
        cookie = request.cookies.get(cookie_name, "")
        key = hashlib.sha256(cookie.encode()).hexdigest()
        data = database.get("browser:" + key) if cookie else None
        if data is None:
            raise HTTPException(401, "Open the account page and sign in again.")
        return key, data

    def new_browser(response, data=None):
        cookie = secrets.token_urlsafe(32)
        key = hashlib.sha256(cookie.encode()).hexdigest()
        data = {**(data or {}), "csrf": secrets.token_urlsafe(32)}
        database.put("browser:" + key, data)
        response.set_cookie(cookie_name, cookie, secure=secure, httponly=True, samesite="lax", max_age=7 * 86400, path="/")
        return key, data

    def public_config():
        researchers = [r for r in database.directory_entries("researchers") if not r.get("autoCreated")]
        labs = database.directory_entries("labs")
        memberships = database.lab_members()
        for lab in labs:
            dids = {m["did"] for m in memberships if m["lab_id"] == lab["id"]}
            lab["members"] = sorted(r["id"] for r in researchers if r.get("did") in dids)
            lab["managed"] = True
        return {"name": "Open Archive", "collection": COLLECTION, "researchers": researchers, "labs": labs}

    def researcher_for(did):
        return next((r for r in public_config()["researchers"] if r.get("did") == did), None)

    async def authenticated(request):
        key, data = browser(request)
        if not data.get("did"):
            raise HTTPException(401, "Sign in first.")
        client = oauth_factory(database, key, origin)
        session = await client.session_store.get_session(data["did"])
        if session is None:
            raise HTTPException(401, "Your session expired. Sign in again.")
        return key, data, client, session

    def lock_for(key):
        return locks[int(key[:8], 16) % len(locks)]

    def remember_suggestions(key, papers):
        saved = database.get("suggestions:" + key) or {}
        for paper in papers:
            saved[paper_key(paper)] = paper
        # Suggestions expire; they are never the published publication list.
        database.put("suggestions:" + key, dict(list(saved.items())[-200:]), ttl=3600)

    @app.middleware("http")
    async def security(request, call_next):
        limits = {"/api/session": 60, "/auth/login": 10, "/api/arxiv/search": 10, "/api/doi/lookup": 10, "/api/papers/search": 10, "/api/labs": 10, "/api/profile": 20, "/api/profile/author": 20, "/api/profile/orcid": 20}
        if request.url.path in limits and not rate_limit(request, limits[request.url.path]):
            return JSONResponse({"detail": "Too many requests. Try again in a minute."}, status_code=429,
                                headers={"Retry-After": "60"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("origin") != origin:
                return JSONResponse({"detail": "Request must come from this site."}, status_code=403)
            try:
                _, data = browser(request)
            except HTTPException:
                return JSONResponse({"detail": "Open the account page first."}, status_code=401)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), data["csrf"]):
                return JSONResponse({"detail": "Session check failed. Reload the page."}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16000:
                    return JSONResponse({"detail": "Request too large."}, status_code=413)
            request._body = bytes(body)  # Replay the bounded body to FastAPI's parser.
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=86400"
        if request.url.path.startswith(("/api/", "/auth/", "/data/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health():
        revision = ROOT / "backend/revision.txt"
        return {"status": "ok", "revision": revision.read_text().strip() if revision.exists() else "development"}

    @app.get("/oauth-client-metadata.json")
    async def oauth_metadata():
        if not secure:
            raise HTTPException(404, "Local development uses loopback client metadata.")
        return client_metadata(database, origin)

    @app.get("/api/session")
    async def session_info(request: Request):
        response = JSONResponse({})
        try:
            key, data = browser(request)
        except HTTPException:
            key, data = new_browser(response)
        did = data.get("did")
        researcher = researcher_for(did) if did else None
        response.body = json.dumps({"csrf": data["csrf"], "user": {"did": did, "handle": data.get("handle")} if did else None,
                                    "researcher": researcher,
                                    "labs": public_config()["labs"]}).encode()
        response.headers["content-length"] = str(len(response.body))
        return response

    @app.post("/auth/login")
    async def login(payload: Login, request: Request):
        key, data = browser(request)
        if data.get("did"):
            raise HTTPException(409, "Sign out before switching accounts.")
        if time.time() - data.get("loginAt", 0) < 5:
            raise HTTPException(429, "Wait a few seconds before trying again.")
        data["loginAt"] = time.time()
        database.put("browser:" + key, data)
        client = oauth_factory(database, key, origin)
        try:
            url, state = await client.start_authorization(payload.handle.strip().removeprefix("@"))
        except Exception:
            raise HTTPException(502, "Could not start ATProto login. Check the handle and try again.") from None
        database.put("login:" + state, {"browser": key}, ttl=600)
        return {"url": url}

    @app.get("/auth/callback")
    async def callback(request: Request, state: str = "", code: str = "", iss: str = "", error: str = ""):
        key, _ = browser(request)
        binding = database.get("login:" + state)
        if not binding or not secrets.compare_digest(binding["browser"], key):
            raise HTTPException(400, "Login expired or started in a different browser. Sign in again.")
        async with lock_for(key):
            if database.get("login:" + state, consume=True) is None:
                raise HTTPException(400, "This login has already been used.")
            client = oauth_factory(database, key, origin)
            if error:
                await client.state_store.delete_state(state)
                return RedirectResponse("/account/?error=denied", status_code=303)
            try:
                session = await client.handle_callback(code=code, state=state, iss=iss)
            except Exception:
                return RedirectResponse("/account/?error=login", status_code=303)
            response = RedirectResponse("/account/", status_code=303)
            new_key, _ = new_browser(response, {"did": session.did, "handle": session.handle})
            await OAuthStore(database, new_key).save_session(session)
            await client.session_store.delete_session(session.did)
            database.delete("browser:" + key)
            return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        key, data = browser(request)
        async with lock_for(key):
            client = oauth_factory(database, key, origin)
            if data.get("did"):
                session = await client.session_store.get_session(data["did"])
                if session:
                    try:
                        await client.revoke_session(session)
                    except Exception:
                        pass
                await client.session_store.delete_session(data["did"])
            database.delete("browser:" + key)
            database.delete("suggestions:" + key)
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="lax")
        return response

    @app.post("/api/profile/author")
    async def choose_author(payload: AuthorChoice, request: Request):
        key, _, _, session = await authenticated(request)
        paper = (database.get("suggestions:" + key) or {}).get(payload.paperId)
        if not paper:
            raise HTTPException(400, "Search again to choose your author name.")
        # The claim is a specific author slot on a specific paper, not a basic name match
        authors = paper.get("authors", [])
        if payload.index >= len(authors) or normalized_name(authors[payload.index]) != normalized_name(payload.name):
            raise HTTPException(400, "Choose an author from the search results.")
        name = authors[payload.index]
        current = researcher_for(session.did)
        identifier = current["id"] if current else "researcher-" + hashlib.sha256(session.did.encode()).hexdigest()[:16]
        profile = {"institution": "", **(current or {}), "id": identifier,
                   "name": name.strip(), "did": session.did, "handle": session.handle, "autoCreated": False}
        database.save_directory_entry("researchers", identifier, session.did, profile)
        return {"researcher": profile}

    @app.delete("/api/profile")
    async def remove_profile(request: Request):
        key, _, _, session = await authenticated(request)
        database.remove_researcher(session.did)
        database.delete("suggestions:" + key)
        return {"ok": True}

    @app.put("/api/profile")
    async def save_profile(payload: ResearcherProfile, request: Request):
        _, _, _, session = await authenticated(request)
        if not payload.name.strip():
            raise HTTPException(400, "Enter your name.")
        current = researcher_for(session.did)
        identifier = current["id"] if current else "researcher-" + hashlib.sha256(session.did.encode()).hexdigest()[:16]
        profile = {**(current or {}), **payload.model_dump(), "id": identifier,
                   "name": payload.name.strip(), "did": session.did, "handle": session.handle, "autoCreated": False}
        database.save_directory_entry("researchers", identifier, session.did, profile)
        return {"researcher": profile}

    @app.put("/api/profile/orcid")
    async def save_orcid(payload: OrcidChoice, request: Request):
        _, _, _, session = await authenticated(request)
        current = researcher_for(session.did)
        if not current:
            raise HTTPException(400, "Choose your author name before adding an ORCID iD.")
        profile = {key: value for key, value in current.items() if key != "orcid"}
        if payload.orcid.strip():
            try:
                orcid = normalize_orcid(payload.orcid)
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
            # An ORCID iD identifies one person, so two profiles can't share it.
            if any(r.get("orcid") == orcid and r.get("did") != session.did for r in public_config()["researchers"]):
                raise HTTPException(409, "Another researcher profile already uses this ORCID iD.")
            profile["orcid"] = orcid
        profile["autoCreated"] = False
        database.save_directory_entry("researchers", current["id"], session.did, profile)
        return {"researcher": profile}

    @app.post("/api/labs")
    async def create_lab(payload: LabProfile, request: Request):
        _, _, _, session = await authenticated(request)
        if not researcher_for(session.did):
            raise HTTPException(400, "Create your researcher profile before creating a lab.")
        if not all(value.strip() for value in (payload.name, payload.institution)):
            raise HTTPException(400, "Enter the lab name and institution.")
        labs = public_config()["labs"]
        if any(normalized_name(lab["name"]) == normalized_name(payload.name) and
               normalized_name(lab.get("institution", "")) == normalized_name(payload.institution) for lab in labs):
            raise HTTPException(409, "This lab is already listed.")
        identifier = "lab-" + hashlib.sha256((normalized_name(payload.name) + "|" + normalized_name(payload.institution)).encode()).hexdigest()[:16]
        # A renamed lab retains its ID; its former name can be used again.
        if any(lab["id"] == identifier for lab in labs):
            identifier = "lab-" + secrets.token_hex(8)
        lab = {"id": identifier, "name": payload.name.strip(), "shortName": payload.name.strip(),
               "institution": payload.institution.strip(),
               "members": [], "about": payload.about.strip(), "contactEmail": payload.contactEmail.strip()}
        try:
            database.create_lab(identifier, session.did, lab)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"lab": lab}

    @app.put("/api/labs/{identifier}")
    async def edit_lab(identifier: str, payload: LabProfile, request: Request):
        _, _, _, session = await authenticated(request)
        if not all(value.strip() for value in (payload.name, payload.institution)):
            raise HTTPException(400, "Enter the lab name and institution.")
        details = {key: value.strip() for key, value in payload.model_dump().items()}
        try:
            lab = database.edit_lab(identifier, session.did, details)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except PermissionError as error:
            raise HTTPException(403, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"lab": lab}

    @app.get("/api/my-labs")
    async def my_labs(request: Request):
        _, _, _, session = await authenticated(request)
        directory = public_config()
        people = {r.get("did"): r for r in directory["researchers"] if r.get("did")}
        memberships = database.lab_members()
        roles = {m["lab_id"]: m["role"] for m in memberships if m["did"] == session.did}
        labs = []
        for lab in directory["labs"]:
            if lab["id"] not in roles:
                continue
            members = [{"did": m["did"], "role": m["role"], "name": people.get(m["did"], {}).get("name", m["did"]),
                        "handle": people.get(m["did"], {}).get("handle", "")}
                       for m in memberships if m["lab_id"] == lab["id"]]
            shared = shared_names(members)
            for member in members:
                member["detail"] = "@" + member["handle"] if member["handle"] and normalized_name(member["name"]) in shared else ""
            labs.append({**lab, "role": roles[lab["id"]], "people": members})
        return {"labs": labs}

    @app.get("/api/lab-invites/{token}")
    async def preview_invite(token: str):
        with database.connect() as db:
            row = db.execute("SELECT lab_id FROM lab_invites WHERE token=?", (token,)).fetchone()
        lab = next((lab for lab in public_config()["labs"] if row and lab["id"] == row["lab_id"]), None)
        if not lab:
            raise HTTPException(404, "This invitation is invalid. Ask the lab admin for a new link.")
        return {"lab": {"id": lab["id"], "name": lab["name"]}}

    @app.post("/api/labs/{identifier}/invite")
    async def invite_member(identifier: str, request: Request):
        _, _, _, session = await authenticated(request)
        token = secrets.token_urlsafe(32)
        try:
            database.manage_lab(identifier, session.did, "invite", token=token)
        except ValueError as error:
            raise HTTPException(403, str(error)) from None
        return {"url": origin + "/account/?invite=" + token}

    @app.post("/api/labs/{identifier}/join")
    async def join_lab(identifier: str, payload: LabJoin, request: Request):
        _, _, _, session = await authenticated(request)
        if not researcher_for(session.did):
            raise HTTPException(400, "Create your researcher profile before joining the lab.")
        try:
            database.manage_lab(identifier, session.did, "join", token=payload.token)
        except ValueError as error:
            raise HTTPException(403, str(error)) from None
        return {"ok": True}

    @app.post("/api/labs/{identifier}/members")
    async def change_member(identifier: str, payload: LabMemberChange, request: Request):
        _, _, _, session = await authenticated(request)
        try:
            database.manage_lab(identifier, session.did, payload.action, target=payload.did)
        except ValueError as error:
            raise HTTPException(403, str(error)) from None
        return {"ok": True}

    @app.get("/api/suggestions")
    async def suggestions(request: Request):
        key, _, _, _ = await authenticated(request)
        return {"papers": list((database.get("suggestions:" + key) or {}).values())}

    @app.post("/api/arxiv/search")
    async def search(payload: Search, request: Request):
        key, _, _, _ = await authenticated(request)
        try:
            papers = await searcher.search(payload.author.strip())
        except Exception:
            raise HTTPException(502, "arXiv search is unavailable. Try again later.") from None
        remember_suggestions(key, papers)
        return {"papers": [{**p, "id": paper_key(p)} for p in papers]}

    @app.post("/api/papers/search")
    async def paper_search(payload: PaperSearch, request: Request):
        key, _, _, _ = await authenticated(request)
        query = payload.query.strip()
        try:
            orcid = normalize_orcid(query)
        except ValueError:
            orcid = None
        if orcid:
            # An ORCID iD finds exactly this person's papers, even when others share the name.
            try:
                papers = await search_orcid(orcid)
            except Exception:
                raise HTTPException(502, "ORCID search is unavailable. Try again later.") from None
            remember_suggestions(key, papers)
            return {"papers": [{**p, "id": paper_key(p)} for p in papers], "authors": [], "kind": "orcid", "warning": ""}
        if query.lower().startswith(("10.", "doi:", "https://", "http://")):
            result = await doi_lookup(DoiLookup(doi=query), request)
            return {**result, "kind": "doi"}
        if not re.search(r"\w", query) or len(query) > 200:
            raise HTTPException(400, "Enter an author name, paper title, or DOI.")
        results = await asyncio.gather(searcher.search_all(query), search_crossref(query, "author"),
                                       search_crossref(query, "title"), search_openalex(query, "author"),
                                       search_openalex(query, "title"), search_datacite(query), return_exceptions=True)
        failures = [label for label, result in zip(["arXiv", "Crossref", "Crossref", "OpenAlex", "OpenAlex", "DataCite"], results)
                    if isinstance(result, Exception)]
        if len(failures) == len(results):
            raise HTTPException(502, "Paper search is unavailable. Try again later.")
        papers = rank_papers(query, [paper for result in results if not isinstance(result, Exception) for paper in result], limit=200)
        remember_suggestions(key, papers)
        warning = "Some results are missing because " + " and ".join(dict.fromkeys(failures)) + " could not be reached. Try again." if failures else ""
        return {"papers": [{**p, "id": paper_key(p)} for p in papers[:20]], "kind": "text", "warning": warning}

    @app.post("/api/doi/lookup")
    async def doi_lookup(payload: DoiLookup, request: Request):
        key, _, _, _ = await authenticated(request)
        try:
            doi = normalize_doi(payload.doi)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        try:
            paper = await lookup_doi(doi)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except Exception:
            raise HTTPException(502, "DOI lookup is unavailable. Try again later.") from None
        remember_suggestions(key, [paper])
        return {"papers": [paper]}

    @app.get("/api/publications")
    async def publications(request: Request):
        key, _, client, session = await authenticated(request)
        async with lock_for(key):
            # Re-load under the lock to avoid refreshing an already-rotated token.
            session = await client.session_store.get_session(session.did)
            try:
                return {"papers": await list_papers(client, session)}
            except Exception:
                raise HTTPException(502, "Could not read your PDS. Try again, or sign in again.") from None

    @app.post("/api/publications")
    async def publish(payload: Import, request: Request):
        key, _, client, session = await authenticated(request)
        if not researcher_for(session.did):
            raise HTTPException(400, "Create your researcher profile or choose your author name first.")
        saved = database.get("suggestions:" + key) or {}
        if any(selected.id not in saved for selected in payload.papers):
            raise HTTPException(400, "Search again before adding these papers.")
        results = []
        async with lock_for(key):
            session = await client.session_store.get_session(session.did)
            try:
                existing = set().union(*(paper_keys(p) for p in await list_papers(client, session)))
            except Exception:
                raise HTTPException(502, "Could not check existing papers. Nothing was added.") from None
            for selected in payload.papers:
                paper = saved[selected.id]
                if paper_keys(paper) & existing:
                    results.append({"id": selected.id, "status": "already-added"})
                    continue
                try:
                    result = await xrpc(client, session, "POST", "createRecord", json={
                        "repo": session.did, "collection": COLLECTION, "rkey": record_key(paper),
                        "record": make_record(paper, []),
                    })
                    existing.update(paper_keys(paper))
                    results.append({"id": selected.id, "status": "added", "uri": result["uri"]})
                except Exception:
                    # Keep the successful results; retrying re-checks the PDS.
                    results.append({"id": selected.id, "status": "failed"})
        return {"results": results}

    @app.delete("/api/publications")
    async def remove_all_publications(request: Request):
        key, _, client, session = await authenticated(request)
        removed, failed = 0, 0
        async with lock_for(key):
            session = await client.session_store.get_session(session.did)
            try:
                papers = await list_papers(client, session)
            except Exception:
                raise HTTPException(502, "Could not load your papers. Nothing was removed.") from None
            for paper in papers:
                rkey = paper["rkey"]
                if not re.fullmatch(r"[a-zA-Z0-9._~:-]{1,512}", rkey) or rkey in {".", ".."}:
                    failed += 1
                    continue
                try:
                    await xrpc(client, session, "POST", "deleteRecord", json={
                        "repo": session.did, "collection": COLLECTION, "rkey": rkey,
                    })
                    removed += 1
                except Exception:
                    failed += 1
        return {"removed": removed, "failed": failed}

    @app.delete("/api/publications/{rkey}")
    async def remove(rkey: str, request: Request):
        if not re.fullmatch(r"[a-zA-Z0-9._~:-]{1,512}", rkey) or rkey in {".", ".."}:
            raise HTTPException(400, "Invalid record key.")
        key, _, client, session = await authenticated(request)
        async with lock_for(key):
            session = await client.session_store.get_session(session.did)
            try:
                await xrpc(client, session, "POST", "deleteRecord", json={
                    "repo": session.did, "collection": COLLECTION, "rkey": rkey,
                })
            except Exception:
                raise HTTPException(502, "Could not remove the paper from your PDS.") from None
        return {"ok": True}

    @app.get("/data/site.json")
    async def site_config():
        # Only this public directory endpoint is readable by GitHub Pages.
        return JSONResponse(public_config(), headers={"Access-Control-Allow-Origin": "*"})

    def render_public(template, values):
        html = (ROOT / "web" / template).read_text(encoding="utf-8")
        values = {"ROOT": "/", "API_ROOT": "", "ACCOUNT_LINK": ' | <a href="/account/">Account</a>', **values}
        for key, value in values.items():
            html = html.replace("{{" + key + "}}", str(value))
        version = hashlib.sha256((ROOT / "web/app.js").read_bytes()).hexdigest()[:16]
        return HTMLResponse(html.replace("/assets/app.js", "/assets/app.js?v=" + version), headers={"Cache-Control": "no-store"})

    def directory_html(entries, kind):
        shared = shared_names(entries)
        return "".join('<div class="directory-item"><h3><a href="/' + kind + '/' + escape(entry["id"], quote=True) +
                       '/">' + escape(entry["name"]) + '</a></h3><p>' + escape(entry_detail(entry, shared)) + '</p></div>' for entry in entries)

    @app.get("/researchers/")
    @app.get("/labs/")
    async def live_directory(request: Request):
        kind = request.url.path.strip("/")
        return render_public("directory.html", {"KIND": kind, "TITLE": kind.capitalize(),
            "ENTRIES": directory_html(public_config()[kind], kind),
            "RESEARCHERS_CURRENT": 'aria-current="page"' if kind == "researchers" else "",
            "LABS_CURRENT": 'aria-current="page"' if kind == "labs" else ""})

    @app.get("/researchers/{identifier}/")
    async def live_researcher(identifier: str):
        directory = public_config()
        researcher = next((r for r in directory["researchers"] if r["id"] == identifier), None)
        if not researcher:
            raise HTTPException(404, "Researcher not found.")
        labs = [lab for lab in directory["labs"] if identifier in lab["members"]]
        account = '<a href="https://bsky.app/profile/' + escape(researcher["did"], quote=True) + '">@' + escape(researcher["handle"]) + '</a>' if researcher.get("did") else "No ATProto account linked yet."
        orcid = ('ORCID iD: <a href="https://orcid.org/' + escape(researcher["orcid"], quote=True) + '">' +
                 escape(researcher["orcid"]) + '</a> (self-provided, not verified)') if researcher.get("orcid") else ""
        return render_public("researcher.html", {"ID": escape(identifier), "NAME": escape(researcher["name"]),
            "INSTITUTION": escape(researcher.get("institution", "")), "ACCOUNT": account, "ORCID": orcid,
            "AFFILIATIONS": directory_html(labs, "labs"), "PUBLICATIONS": ""})

    @app.get("/labs/{identifier}/")
    async def live_lab(identifier: str):
        directory = public_config()
        lab = next((lab for lab in directory["labs"] if lab["id"] == identifier), None)
        if not lab:
            raise HTTPException(404, "Lab not found.")
        members = [r for r in directory["researchers"] if r["id"] in lab["members"]]
        return render_public("lab.html", {"ID": escape(identifier), "NAME": escape(lab["name"]),
            "SHORT_NAME": escape(lab.get("shortName", lab["name"])), "INSTITUTION": escape(lab.get("institution", "")),
            "MEMBER_COUNT": len(members), "MEMBERS": directory_html(members, "researchers"), "PUBLICATIONS": ""})

    @app.get("/account/")
    async def account_page():
        script_hash = hashlib.sha256((ROOT / "web/account.js").read_bytes()).hexdigest()[:16]
        html = (ROOT / "web/account.html").read_text(encoding="utf-8").replace("/assets/account.js", "/assets/account.js?v=" + script_hash)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/assets/account.js")
    async def account_script():
        return FileResponse(ROOT / "web/account.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    # The same plain website is served alongside the API during development.
    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True, check_dir=False), name="website")
    return app
