"""FastAPI app: login, profile claims, paper suggestions, and PDS writes."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
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
from .storage import Database, OAuthStore
from .papers import normalize_doi, lookup_doi, search_crossref, rank_papers

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "open_archive_session"


def normalized_name(value):
    return " ".join(unicodedata.normalize("NFC", value).casefold().split()) if isinstance(value, str) else ""


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    handle: str = Field(min_length=3, max_length=253, pattern=r"^@?[a-zA-Z0-9.:-]+$")


class Claim(Input):
    researcherId: str = Field(min_length=1, max_length=100)


class Search(Input):
    author: str = Field(min_length=2, max_length=120, pattern=r"^[\w .,'’\-]+$")


class PaperSearch(Input):
    query: str = Field(min_length=2, max_length=1000)


class DoiLookup(Input):
    doi: str = Field(min_length=1, max_length=1000)


class ResearcherProfile(Input):
    name: str = Field(min_length=2, max_length=120)
    institution: str = Field(default="", max_length=200)
    labIds: list[str] = Field(default_factory=list, max_length=20)


class LabProfile(Input):
    name: str = Field(min_length=2, max_length=200)
    institution: str = Field(min_length=2, max_length=200)
    professorName: str = Field(min_length=2, max_length=120)


class Selection(Input):
    id: str = Field(max_length=500)
    labIds: list[str] = Field(default_factory=list, max_length=20)


class Import(Input):
    papers: list[Selection] = Field(min_length=1, max_length=20)


def create_app(*, directory=None, origin=None, config=None, catalog=None, oauth_factory=make_client, searcher=None):
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
    config = config or json.loads((ROOT / "data/site.json").read_text())
    catalog = catalog if catalog is not None else json.loads((ROOT / "data/papers.json").read_text())
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
        result = json.loads(json.dumps(config))
        links = {claim["researcher_id"]: claim for claim in database.claims() if claim["status"] == "approved"}
        for researcher in result["researchers"]:
            if researcher["id"] in links:
                link = links[researcher["id"]]
                researcher.update(did=link["did"], handle=link["handle"])
        for kind in ("researchers", "labs"):
            entries = {entry["id"]: entry for entry in result[kind]}
            entries.update({entry["id"]: entry for entry in database.directory_entries(kind)})
            result[kind] = list(entries.values())
        for lab in result["labs"]:
            members = set(lab.get("members", []))
            for researcher in result["researchers"]:
                if "labIds" in researcher:
                    members.discard(researcher["id"])
                    if lab["id"] in researcher["labIds"]:
                        members.add(researcher["id"])
            lab["members"] = sorted(members)
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
        limits = {"/api/session": 60, "/auth/login": 10, "/api/arxiv/search": 10, "/api/doi/lookup": 10, "/api/papers/search": 10, "/api/labs": 10, "/api/profile": 20}
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
            _, data = browser(request)
        except HTTPException:
            _, data = new_browser(response)
        did = data.get("did")
        researcher = researcher_for(did) if did else None
        claim = next((c for c in database.claims() if c["did"] == did), None)
        response.body = json.dumps({"csrf": data["csrf"], "user": {"did": did, "handle": data.get("handle")} if did else None,
                                    "researcher": researcher, "claim": claim,
                                    "researchers": [{"id": r["id"], "name": r["name"]} for r in public_config()["researchers"] if not r.get("did")],
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

    @app.put("/api/profile")
    async def save_profile(payload: ResearcherProfile, request: Request):
        _, _, _, session = await authenticated(request)
        if not payload.name.strip():
            raise HTTPException(400, "Enter your name.")
        known = {lab["id"] for lab in public_config()["labs"]}
        if set(payload.labIds) - known:
            raise HTTPException(400, "Unknown lab.")
        current = researcher_for(session.did)
        identifier = current["id"] if current else "researcher-" + hashlib.sha256(session.did.encode()).hexdigest()[:16]
        profile = {**(current or {}), **payload.model_dump(), "id": identifier,
                   "name": payload.name.strip(), "did": session.did, "handle": session.handle}
        database.save_directory_entry("researchers", identifier, session.did, profile)
        return {"researcher": profile}

    @app.post("/api/labs")
    async def create_lab(payload: LabProfile, request: Request):
        _, _, _, session = await authenticated(request)
        if not all(value.strip() for value in (payload.name, payload.institution, payload.professorName)):
            raise HTTPException(400, "Enter the lab name, institution, and professor's full name.")
        labs = public_config()["labs"]
        if any(normalized_name(lab["name"]) == normalized_name(payload.name) and
               normalized_name(lab.get("institution", "")) == normalized_name(payload.institution) for lab in labs):
            raise HTTPException(409, "This lab is already listed.")
        identifier = "lab-" + hashlib.sha256((normalized_name(payload.name) + "|" + normalized_name(payload.institution)).encode()).hexdigest()[:16]
        lab = {"id": identifier, "name": payload.name.strip(), "shortName": payload.name.strip(),
               "institution": payload.institution.strip(), "professorNames": [payload.professorName.strip()],
               "members": [], "about": ""}
        try:
            database.save_directory_entry("labs", identifier, session.did, lab)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"lab": lab}

    @app.post("/api/claims")
    async def claim_profile(payload: Claim, request: Request):
        _, _, _, session = await authenticated(request)
        if researcher_for(session.did):
            raise HTTPException(409, "Your account is already linked to a profile.")
        researcher = next((r for r in public_config()["researchers"] if r["id"] == payload.researcherId), None)
        if researcher is None or researcher.get("did"):
            raise HTTPException(409, "This profile is unavailable to claim.")
        # Read the signed-in DID's public profile, never a browser-supplied name.
        try:
            profile = await get_json(session.pds_url.rstrip("/") + "/xrpc/com.atproto.repo.getRecord",
                                     params={"repo": session.did, "collection": "app.bsky.actor.profile", "rkey": "self"})
        except Exception:
            raise HTTPException(502, "Could not read your Bluesky profile. Make sure it has a display name and try again.") from None
        if profile.get("uri") != f"at://{session.did}/app.bsky.actor.profile/self":
            raise HTTPException(502, "The account provider returned a different profile. Try again later.")
        name = normalized_name(profile.get("value", {}).get("displayName"))
        if not name or name != normalized_name(researcher["name"]):
            raise HTTPException(403, "Your Bluesky display name must match this researcher's full name.")
        matches = [r for r in public_config()["researchers"] if normalized_name(r["name"]) == name]
        if len(matches) != 1:
            raise HTTPException(409, "More than one researcher has this name. This profile cannot be claimed by name alone.")
        try:
            database.claim_by_name(session.did, session.handle, researcher["id"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"status": "approved", "message": "Profile claimed. You can now manage your publications."}

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

    @app.post("/api/papers/search")
    async def paper_search(payload: PaperSearch, request: Request):
        key, _, _, _ = await authenticated(request)
        query = payload.query.strip()
        if query.lower().startswith(("10.", "doi:", "https://", "http://")):
            result = await doi_lookup(DoiLookup(doi=query), request)
            return {**result, "kind": "doi"}
        if not re.search(r"\w", query) or len(query) > 200:
            raise HTTPException(400, "Enter an author name, paper title, or DOI.")
        results = await asyncio.gather(searcher.search_all(query), search_crossref(query, "author"),
                                       search_crossref(query, "title"), return_exceptions=True)
        failures = [label for label, result in zip(["arXiv", "Crossref", "Crossref"], results)
                    if isinstance(result, Exception)]
        if len(failures) == len(results):
            raise HTTPException(502, "Paper search is unavailable. Try again later.")
        papers = rank_papers(query, [paper for result in results if not isinstance(result, Exception) for paper in result])
        remember_suggestions(key, papers)
        warning = "Some results are missing because " + " and ".join(dict.fromkeys(failures)) + " could not be reached. Try again." if failures else ""
        return {"papers": [{**p, "id": paper_key(p)} for p in papers], "kind": "text", "warning": warning}

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
        saved = database.get("suggestions:" + key) or {}
        labs = public_config()["labs"]
        known_labs = {lab["id"] for lab in labs}
        for selected in payload.papers:
            if selected.id not in saved:
                raise HTTPException(400, "A suggestion expired. Reload suggestions or search again.")
            if set(selected.labIds) - known_labs:
                raise HTTPException(400, "Unknown lab.")
            authors = {normalized_name(name) for name in saved[selected.id].get("authors", [])}
            eligible = {lab["id"] for lab in labs if authors & {normalized_name(name) for name in lab.get("professorNames", [])}}
            if set(selected.labIds) - eligible:
                raise HTTPException(400, "A lab can only be associated when its professor is a listed author.")
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
        return "".join('<div class="directory-item"><h3><a href="/' + kind + '/' + escape(entry["id"], quote=True) +
                       '/">' + escape(entry["name"]) + '</a></h3><p>' + escape(entry.get("institution", "")) + '</p></div>' for entry in entries)

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
        return render_public("researcher.html", {"ID": escape(identifier), "NAME": escape(researcher["name"]),
            "INSTITUTION": escape(researcher.get("institution", "")), "ACCOUNT": account,
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
            "DEMO_NOTE": '<p class="note">Demo page. Not the lab’s official website.</p>' if lab.get("demo") else "", "MEMBER_COUNT": len(members), "MEMBERS": directory_html(members, "researchers"), "PUBLICATIONS": ""})

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
