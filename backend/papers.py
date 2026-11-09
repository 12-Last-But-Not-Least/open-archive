"""On-demand metadata discovery and writes into the signed-in user's PDS."""

import asyncio
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

import httpx
from defusedxml import ElementTree

COLLECTION = "io.github.12-last-but-not-least.publication"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def normalize_doi(value):
    value = value.strip()
    if value.lower().startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.hostname not in {"doi.org", "dx.doi.org"} or parsed.username or parsed.password or parsed.port:
            raise ValueError("Enter a DOI or a doi.org link.")
        value = unquote(parsed.path.lstrip("/"))
    else:
        value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", value) or len(value) > 500:
        raise ValueError("Enter a valid DOI, such as 10.1109/GHTC55712.2022.9910606.")
    return value.lower()


def parse_crossref(data, doi):
    if normalize_doi(data.get("DOI", "")) != doi:
        raise ValueError("The metadata returned a different DOI.")
    titles = data.get("title") or []
    authors = [a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")]))
               for a in data.get("author", [])]
    authors = [name for name in authors if name.strip()]
    dates = (data.get("published") or data.get("issued") or {}).get("date-parts", [])
    if not titles or not titles[0].strip() or not authors or not dates or not dates[0]:
        raise ValueError("This DOI is missing a title, authors, or publication date in Crossref.")
    parts = dates[0]
    published = datetime(parts[0], parts[1] if len(parts) > 1 else 1,
                         parts[2] if len(parts) > 2 else 1, tzinfo=timezone.utc)
    return {"id": "doi:" + doi, "doi": doi, "title": " ".join(titles[0].split()),
            "authors": authors, "publishedAt": published.isoformat().replace("+00:00", "Z"),
            "url": "https://doi.org/" + quote(doi, safe="/():;"),
            "venue": (data.get("container-title") or [""])[0], "labIds": [], "source": "crossref"}


async def lookup_doi(doi):
    # Only contact Crossref; never fetch a user-supplied URL.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get("https://api.crossref.org/works/" + quote(doi, safe=""),
                                    headers={"User-Agent": "open-archive/0.2 (+https://github.com/12-Last-But-Not-Least/open-archive)"})
        if response.status_code == 404:
            response = await client.get("https://api.datacite.org/dois/" + quote(doi, safe=""))
            if response.status_code == 404:
                raise LookupError("DOI not found. Check the DOI and try again.")
            response.raise_for_status()
            paper = parse_datacite(response.json()["data"])
            if paper["doi"] != doi:
                raise ValueError("The metadata returned a different DOI.")
            return paper
        response.raise_for_status()
        return parse_crossref(response.json()["message"], doi)


async def search_crossref(query, field):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get("https://api.crossref.org/works", params={
            "query." + field: query, "rows": 20,
        }, headers={"User-Agent": "open-archive/0.2 (+https://github.com/12-Last-But-Not-Least/open-archive)"})
        response.raise_for_status()
        papers = []
        for item in response.json()["message"]["items"]:
            try:
                papers.append(parse_crossref(item, normalize_doi(item.get("DOI", ""))))
            except (ValueError, TypeError, KeyError):
                continue
        return papers


def metadata_paper(title, authors, date, url, source, doi="", venue=""):
    if not isinstance(title, str) or not title.strip() or not authors:
        raise ValueError("Missing title or authors")
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid paper link")
    published = datetime.fromisoformat(date.replace("Z", "+00:00"))
    paper = {"title": " ".join(title.split()), "authors": authors,
             "publishedAt": published.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
             "url": url, "source": source, "labIds": [], "venue": venue}
    if doi:
        paper["doi"] = normalize_doi(doi)
    if parsed.hostname in {"arxiv.org", "www.arxiv.org"} and parsed.path.startswith("/abs/"):
        identifier = re.sub(r"v\d+$", "", parsed.path[5:])
        if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})", identifier):
            paper["arxivId"] = identifier
    paper["id"] = paper_key(paper)
    return paper


def parse_openalex(item):
    authors = [a.get("author", {}).get("display_name", "").strip() for a in item.get("authorships", [])]
    authors = [name for name in authors if name]
    location = item.get("primary_location") or {}
    url = location.get("landing_page_url") or item.get("doi") or item.get("id")
    if not url or not url.startswith("https://"):
        url = item.get("doi") or item.get("id")
    return metadata_paper(item.get("title"), authors, item.get("publication_date", ""), url,
                          "openalex", item.get("doi") or "", (location.get("source") or {}).get("display_name", ""))


async def search_openalex(query, field):
    headers = {"User-Agent": "open-archive/0.2 (+https://github.com/12-Last-But-Not-Least/open-archive)"}
    if os.environ.get("OPENALEX_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["OPENALEX_API_KEY"]
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as client:
        params = {"per-page": 20, "search": query}
        if field == "author":
            response = await client.get("https://api.openalex.org/authors", params={"search": query, "per-page": 10})
            response.raise_for_status()
            tokens = set(re.findall(r"\w+", query.casefold()))
            ids = [a["id"].rsplit("/", 1)[-1] for a in response.json()["results"]
                   if re.fullmatch(r"https://openalex.org/A\d+", a.get("id", "")) and
                   tokens <= set(re.findall(r"\w+", a.get("display_name", "").casefold()))]
            if not ids:
                return []
            params = {"per-page": 20, "filter": "authorships.author.id:" + "|".join(ids)}
        response = await client.get("https://api.openalex.org/works", params=params)
        response.raise_for_status()
        papers = []
        for item in response.json()["results"]:
            try:
                papers.append(parse_openalex(item))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return papers


def parse_datacite(item):
    data = item["attributes"]
    if (data.get("types") or {}).get("resourceTypeGeneral") not in {
        "Text", "JournalArticle", "ConferencePaper", "ConferenceProceeding", "Preprint", "Dissertation", "Report", "Book", "BookChapter"}:
        raise ValueError("Not a written research work")
    doi = normalize_doi(data["doi"])
    authors = []
    for creator in data.get("creators", []):
        name = " ".join(filter(None, [creator.get("givenName"), creator.get("familyName")])) or creator.get("name", "")
        if "," in name and not creator.get("givenName") and creator.get("nameType") != "Organizational":
            family, given = name.split(",", 1)
            name = given.strip() + " " + family.strip()
        if name.strip():
            authors.append(name.strip())
    year = int(data["publicationYear"])
    return metadata_paper(data["titles"][0]["title"], authors, f"{year:04d}-01-01",
                          "https://doi.org/" + quote(doi, safe="/():;"), "datacite", doi)


async def search_datacite(query):
    terms = " AND ".join('"' + token + '"' for token in re.findall(r"\w+", query))
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get("https://api.datacite.org/dois", params={
            "query": f"(creators.name:({terms}) OR titles.title:({terms})) AND types.resourceTypeGeneral:(Text OR JournalArticle OR ConferencePaper OR ConferenceProceeding OR Preprint OR Dissertation OR Report OR Book OR BookChapter)",
            "page[size]": 20,
        })
        response.raise_for_status()
        papers = []
        for item in response.json()["data"]:
            try:
                papers.append(parse_datacite(item))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
        return papers


def rank_papers(query, papers, limit=20):
    tokens = set(re.findall(r"\w+", query.casefold()))
    def fields(paper):
        return [paper["title"], *paper.get("authors", [])]
    def exact(paper):
        return any(" ".join(value.casefold().split()) == " ".join(query.casefold().split()) for value in fields(paper))
    ranked, seen = [], set()
    for paper in sorted(papers, key=lambda p: not exact(p)):
        if not tokens or not any(tokens <= set(re.findall(r"\w+", value.casefold())) for value in fields(paper)):
            continue
        identifiers = paper_keys(paper)
        if identifiers & seen:
            continue
        seen.update(identifiers)
        ranked.append(paper)
    return ranked[:limit]


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
        return await self._search('au:"' + author + '"', "submittedDate")

    async def search_all(self, query):
        words = re.findall(r"\w+", query)
        terms = " AND ".join('(au:"' + word + '" OR ti:"' + word + '")' for word in words)
        return await self._search(terms, "relevance")

    async def _search(self, query, sort):
        # One request at a time, with arXiv's required minimum interval.
        async with self.lock:
            await asyncio.sleep(max(0, 3 - (time.monotonic() - self.last_request)))
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                    response = await client.get("https://export.arxiv.org/api/query", params={
                        "search_query": query, "max_results": 20,
                        "sortBy": sort, "sortOrder": "descending",
                    }, headers={"User-Agent": "open-archive/0.2 (+https://github.com/12-Last-But-Not-Least/open-archive)"})
                    response.raise_for_status()
                    return parse_arxiv(response.content)
            finally:
                self.last_request = time.monotonic()
