"""Loopback-only MVP test server. Never imported by the production application.

Real routes, cookies, CSRF, SQLite and publication writes; only upstream OAuth,
search and PDS are simulated. Each invocation uses a disposable database.
"""
import importlib
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
from fastapi import FastAPI
from tests.support import FakeOAuth, PAPER

backend = importlib.import_module("backend.app")
ORIGIN = "http://127.0.0.1:8001"
network = {"requests": [], "records": {}, "revoked": []}
paper = {**PAPER, "title": "Shared lab research", "authors": ["Lab Member"]}


class OAuth(FakeOAuth):
    async def start_authorization(self, handle):
        _, state = await super().start_authorization(handle)
        return ORIGIN + "/auth/callback?" + urlencode({
            "state": state, "code": "test-code", "iss": "https://bsky.social",
        }), state


class Search:
    async def search_all(self, query):
        return [paper]


async def no_results(*args, **kwargs):
    return []


async def public_profile(url, **kwargs):
    return {"value": {}}


async def institution_suggestions(query):
    if query == "UC San D":
        return ["University of California San Diego"]
    if query == "Example Univ":
        return ["Example University"]
    if query == "Updated University":
        raise httpx.ConnectError("Test service outage")
    return []


if __name__ == "__main__":
    subprocess.run([sys.executable, "scripts/build.py", "--with-account"], cwd=ROOT, check=True)
    backend.search_crossref = backend.search_openalex = backend.search_datacite = no_results
    backend.get_json = public_profile
    backend.search_institutions = institution_suggestions
    with TemporaryDirectory(prefix="open-archive-mvp-") as directory:
        app = FastAPI()

        @app.get("/__test__/records")
        async def records(repo: str):
            return {"records": network["records"].get(repo, [])}

        app.mount("/", backend.create_app(directory=directory, origin=ORIGIN,
            oauth_factory=lambda *args: OAuth(*args, network), searcher=Search()))
        uvicorn.run(app, host="127.0.0.1", port=8001, access_log=False)
