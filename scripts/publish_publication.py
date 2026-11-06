"""Publish a paper to your own PDS; credentials are entered locally and never saved."""

import argparse
import getpass
import hashlib
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

from common import ROOT, arxiv_id, read_json, request_json, resolve_identity
from import_arxiv import fetch_paper


def make_record(paper, collection, labs):
    record = {
        "$type": collection,
        "title": paper["title"],
        "authors": paper["authors"],
        "publishedAt": paper["publishedAt"],
        "url": paper["url"],
        "abstract": paper.get("abstract", ""),
        "labIds": labs,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if paper.get("doi"):
        record["doi"] = paper["doi"]
    if paper.get("arxivId"):
        record["arxivId"] = paper["arxivId"]
    if paper.get("venue"):
        record["venue"] = paper["venue"]
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper", help="arXiv URL/identifier, or a paper id from data/papers.json")
    parser.add_argument("--handle", help="Your Bluesky handle; prompted if omitted")
    parser.add_argument("--lab", action="append", default=[], help="Lab id associated with the paper")
    parser.add_argument("--dry-run", action="store_true", help="Print the record without signing in or publishing")
    args = parser.parse_args()
    config = read_json(ROOT / "data/site.json")
    if set(args.lab) - {lab["id"] for lab in config["labs"]}:
        parser.error("Unknown lab id; add it to data/site.json first.")
    papers = read_json(ROOT / "data/papers.json")
    paper = next((item for item in papers if item["id"] == args.paper or item["url"] == args.paper), None)
    if paper is None:
        identifier = arxiv_id(args.paper)
        paper = next((item for item in papers if item.get("arxivId") == identifier), None)
        if paper is None:
            paper = fetch_paper(identifier)
    record = make_record(paper, config["collection"], args.lab)
    if args.dry_run:
        import json
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return
    handle = (args.handle or input("Bluesky handle: ")).strip().removeprefix("@")
    did, pds = resolve_identity(handle)
    print("Paper: " + paper["title"])
    print("Publishing to your account: " + did)
    if input("Publish this public authorship claim? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return
    password = getpass.getpass("Bluesky app password (hidden): ")
    session = request_json(pds + "/xrpc/com.atproto.server.createSession", {"identifier": handle, "password": password})
    del password
    if session.get("did") != did:
        raise ValueError("Authenticated account does not match the selected account.")
    token = session["accessJwt"]
    # A stable key prevents rerunning the script from creating duplicates.
    identifier = paper.get("arxivId") or paper["id"]
    rkey = ("arxiv-" if paper.get("arxivId") else "paper-") + hashlib.sha256(identifier.encode()).hexdigest()[:24]
    query = urlencode({"repo": did, "collection": config["collection"], "rkey": rkey})
    try:
        try:
            existing = request_json(pds + "/xrpc/com.atproto.repo.getRecord?" + query)
        except RuntimeError as error:
            if not str(error).startswith("HTTP 400:") and not str(error).startswith("HTTP 404:"):
                raise
        else:
            print("Already published: " + existing["uri"])
            return
        result = request_json(pds + "/xrpc/com.atproto.repo.createRecord", {
            "repo": did,
            "collection": config["collection"],
            "rkey": rkey,
            "record": record,
        }, token=token)
        print("Published: " + result["uri"])
        print("Refresh the researcher or lab page to read the new record.")
    finally:
        # Discard the short-lived login session, including its refresh token.
        try:
            request_json(pds + "/xrpc/com.atproto.server.deleteSession", {}, token=session["refreshJwt"])
        except (OSError, RuntimeError):
            pass


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, EOFError) as error:
        sys.exit(str(error))
