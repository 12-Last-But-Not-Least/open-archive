"""Local FastAPI app: login, profile claims, paper suggestions, and PDS writes."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .oauth import make_client
from .papers import COLLECTION, ArxivSearch, list_papers, make_record, paper_key, paper_keys, record_key, xrpc
from .storage import Database, OAuthStore

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "open_archive_session"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    handle: str = Field(min_length=3, max_length=253, pattern=r"^@?[a-zA-Z0-9.:-]+$")


class Claim(Input):
    researcherId: str = Field(min_length=1, max_length=100)


class Search(Input):
    author: str = Field(min_length=2, max_length=120, pattern=r"^[\w .,'’\-]+$")


class Selection(Input):
    id: str = Field(max_length=500)
    labIds: list[str] = Field(default_factory=list, max_length=20)


class Import(Input):
    papers: list[Selection] = Field(min_length=1, max_length=20)


def create_app(*, directory=None, origin=None, config=None, catalog=None, oauth_factory=make_client, searcher=None):
    origin = (origin or os.environ.get("OPEN_ARCHIVE_ORIGIN", "http://127.0.0.1:8000")).rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("This prototype uses loopback OAuth. Set OPEN_ARCHIVE_ORIGIN to http://127.0.0.1:<port>.")
    database = Database(directory or os.environ.get("OPEN_ARCHIVE_STATE_DIR", ROOT / ".local"))
    config = config or json.loads((ROOT / "data/site.json").read_text())
    catalog = catalog if catalog is not None else json.loads((ROOT / "data/papers.json").read_text())
    searcher = searcher or ArxivSearch()
    # One worker; serialize each browser's token refreshes and writes.
    locks = [asyncio.Lock() for _ in range(64)]
    app = FastAPI(title="Open Archive", docs_url="/api/docs", redoc_url=None)
    app.state.database = database
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])

    def browser(request):
        cookie = request.cookies.get(COOKIE, "")
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
        response.set_cookie(COOKIE, cookie, httponly=True, samesite="lax", max_age=7 * 86400, path="/")
        return key, data

    def public_config():
        result = json.loads(json.dumps(config))
        links = {claim["researcher_id"]: claim for claim in database.claims() if claim["status"] == "approved"}
        for researcher in result["researchers"]:
            if researcher["id"] in links:
                link = links[researcher["id"]]
                researcher.update(did=link["did"], handle=link["handle"])
        return result

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
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("origin") != origin:
                return JSONResponse({"detail": "Request must come from this site."}, status_code=403)
            try:
                _, data = browser(request)
            except HTTPException:
                return JSONResponse({"detail": "Open the account page first."}, status_code=401)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), data["csrf"]):
                return JSONResponse({"detail": "Session check failed. Reload the page."}, status_code=403)
            body = await request.body()
            if len(body) > 16000:
                return JSONResponse({"detail": "Request too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/api/", "/auth/", "/data/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/session")
    async def session_info(request: Request):
        response = JSONResponse({})
        try:
            _, data = browser(request)
        except HTTPException:
            _, data = new_browser(response)
        did = data.get("did")
        researcher = researcher_for(did) if did else None
        claim = next((c for c in database.claims() if c["did"] == did), None)
        response.body = json.dumps({"csrf": data["csrf"], "user": {"did": did, "handle": data.get("handle")} if did else None,
                                    "researcher": researcher, "claim": claim,
                                    "researchers": [{"id": r["id"], "name": r["name"]} for r in public_config()["researchers"] if not r.get("did")],
                                    "labs": config["labs"]}).encode()
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
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/api/claims")
    async def claim_profile(payload: Claim, request: Request):
        _, _, _, session = await authenticated(request)
        if researcher_for(session.did):
            raise HTTPException(409, "Your account is already linked to a profile.")
        researcher = next((r for r in public_config()["researchers"] if r["id"] == payload.researcherId), None)
        if researcher is None or researcher.get("did"):
            raise HTTPException(409, "This profile is unavailable to claim.")
        database.request_claim(session.did, session.handle, researcher["id"])
        return {"status": "pending", "message": "Requested. Contact the site maintainer for email verification."}

    @app.get("/api/suggestions")
    async def suggestions(request: Request):
        key, _, _, session = await authenticated(request)
        researcher = researcher_for(session.did)
        claim = next((c for c in database.claims() if c["did"] == session.did), None)
        researcher_id = researcher["id"] if researcher else claim["researcher_id"] if claim else None
        papers = [p for p in catalog if researcher_id in p.get("researcherIds", [])]
        remember_suggestions(key, papers)
        return {"papers": [{**p, "id": paper_key(p)} for p in papers]}

    @app.post("/api/arxiv/search")
    async def search(payload: Search, request: Request):
        key, _, _, _ = await authenticated(request)
        try:
            papers = await searcher.search(payload.author.strip())
        except Exception:
            raise HTTPException(502, "arXiv search is unavailable. Try again later.") from None
        remember_suggestions(key, papers)
        return {"papers": [{**p, "id": paper_key(p)} for p in papers]}

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
        saved = database.get("suggestions:" + key) or {}
        known_labs = {lab["id"] for lab in config["labs"]}
        for selected in payload.papers:
            if selected.id not in saved:
                raise HTTPException(400, "A suggestion expired. Reload suggestions or search again.")
            if set(selected.labIds) - known_labs:
                raise HTTPException(400, "Unknown lab.")
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
                        "record": make_record(paper, list(dict.fromkeys(selected.labIds))),
                    })
                    existing.update(paper_keys(paper))
                    results.append({"id": selected.id, "status": "added", "uri": result["uri"]})
                except Exception:
                    # Keep the successful results; retrying re-checks the PDS.
                    results.append({"id": selected.id, "status": "failed"})
        return {"results": results}

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
        return public_config()

    @app.get("/account/")
    async def account_page():
        return FileResponse(ROOT / "web/account.html")

    @app.get("/assets/account.js")
    async def account_script():
        return FileResponse(ROOT / "web/account.js", media_type="text/javascript")

    # The same plain website is served alongside the API during development.
    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True, check_dir=False), name="website")
    return app
