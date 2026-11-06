"""On-demand metadata discovery and writes into the signed-in user's PDS."""

import asyncio
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from defusedxml import ElementTree

COLLECTION = "io.github.12-last-but-not-least.publication"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def paper_key(paper):
    if paper.get("arxivId"):
        return "arxiv:" + re.sub(r"v\d+$", "", paper["arxivId"])
    if paper.get("doi"):
        return "doi:" + paper["doi"].lower().removeprefix("https://doi.org/")
    return paper["url"]


def paper_keys(paper):
    keys = set()
    if paper.get("arxivId"):
        keys.add("arxiv:" + re.sub(r"v\d+$", "", paper["arxivId"]))
    if paper.get("doi"):
        keys.add("doi:" + paper["doi"].lower().removeprefix("https://doi.org/"))
    return keys or {paper["url"]}


def record_key(paper):
    # Preserve the keys used by the original publishing script.
    identifier = paper.get("arxivId") or paper_key(paper)
    prefix = "arxiv-" if paper.get("arxivId") else "paper-"
    return prefix + hashlib.sha256(identifier.encode()).hexdigest()[:24]


def make_record(paper, lab_ids):
    record = {key: paper[key] for key in ("title", "authors", "publishedAt", "url")}
    for key in ("arxivId", "doi", "abstract", "venue", "projectURL"):
        if paper.get(key):
            record[key] = paper[key]
    record.update({"$type": COLLECTION, "labIds": lab_ids,
                   "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
    return record


async def xrpc(client, session, method, name, **kwargs):
    if session.expires_at and session.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=30):
        session = await client.refresh_session(session)
    response = await client.make_authenticated_request(
        session, method, session.pds_url.rstrip("/") + "/xrpc/com.atproto.repo." + name, **kwargs)
    if response.status_code == 401:
        session = await client.refresh_session(session)
        response = await client.make_authenticated_request(
            session, method, session.pds_url.rstrip("/") + "/xrpc/com.atproto.repo." + name, **kwargs)
    response.raise_for_status()
    return response.json()


async def list_papers(client, session):
    papers, seen, cursor = [], set(), None
    for _ in range(100):
        params = {"repo": session.did, "collection": COLLECTION, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = await xrpc(client, session, "GET", "listRecords", params=params)
        for item in data["records"]:
            value = item.get("value", {})
            prefix = f"at://{session.did}/{COLLECTION}/"
            if item.get("uri", "").startswith(prefix) and value.get("$type") == COLLECTION:
                papers.append({**value, "uri": item["uri"], "cid": item.get("cid"),
                               "rkey": item["uri"][len(prefix):]})
        cursor = data.get("cursor")
        if not cursor:
            return papers
        if cursor in seen:
            raise ValueError("PDS repeated a page")
        seen.add(cursor)
    raise ValueError("Too many publication pages")


def parse_arxiv(xml):
    papers = []
    for entry in ElementTree.fromstring(xml).findall("a:entry", ATOM):
        identifier = re.sub(r"v\d+$", "", entry.findtext("a:id", "", ATOM).split("/abs/")[-1])
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})", identifier):
            raise ValueError("arXiv returned an invalid result")
        papers.append({
            "id": "arxiv:" + identifier, "arxivId": identifier,
            "title": " ".join(entry.findtext("a:title", "", ATOM).split()),
            "authors": [author.findtext("a:name", "", ATOM) for author in entry.findall("a:author", ATOM)],
            "publishedAt": entry.findtext("a:published", "", ATOM),
            "abstract": " ".join(entry.findtext("a:summary", "", ATOM).split()),
            "doi": entry.findtext("arxiv:doi", "", ATOM),
            "url": "https://arxiv.org/abs/" + identifier, "labIds": [], "source": "arxiv",
        })
    return papers


class ArxivSearch:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_request = 0

    async def search(self, author):
        # One request at a time, with arXiv's required minimum interval.
        async with self.lock:
            await asyncio.sleep(max(0, 3 - (time.monotonic() - self.last_request)))
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    response = await client.get("https://export.arxiv.org/api/query", params={
                        "search_query": 'au:"' + author + '"', "max_results": 20,
                        "sortBy": "submittedDate", "sortOrder": "descending",
                    }, headers={"User-Agent": "open-archive/0.2 (+https://github.com/12-Last-But-Not-Least/open-archive)"})
                    response.raise_for_status()
                    return parse_arxiv(response.content)
            finally:
                self.last_request = time.monotonic()
