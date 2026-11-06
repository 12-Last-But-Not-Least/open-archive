"""Import one paper's public metadata. No account, API key, or dependencies."""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from common import ROOT, USER_AGENT, arxiv_id, read_json, write_json

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def parse_atom(xml):
    entry = ET.fromstring(xml).find("a:entry", ATOM)
    if entry is None or entry.findtext("a:title", "", ATOM).strip() == "Error":
        raise ValueError("arXiv did not return a paper.")
    identifier = arxiv_id(entry.findtext("a:id", "", ATOM))
    return {
        "id": "arxiv:" + identifier,
        "arxivId": identifier,
        "title": re.sub(r"\s+", " ", entry.findtext("a:title", "", ATOM)).strip(),
        "authors": [author.findtext("a:name", "", ATOM) for author in entry.findall("a:author", ATOM)],
        "publishedAt": entry.findtext("a:published", "", ATOM),
        "abstract": re.sub(r"\s+", " ", entry.findtext("a:summary", "", ATOM)).strip(),
        "url": "https://arxiv.org/abs/" + identifier,
        "doi": entry.findtext("arxiv:doi", "", ATOM),
        "researcherIds": [],
        "labIds": [],
        "source": "arxiv",
    }


def fetch_paper(identifier):
    url = "https://export.arxiv.org/api/query?" + urlencode({"id_list": arxiv_id(identifier)})
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
        return parse_atom(response.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper", help="arXiv URL or identifier")
    parser.add_argument("--researcher", action="append", default=[], help="Researcher id from data/site.json")
    parser.add_argument("--lab", action="append", default=[], help="Lab id attached to this work")
    args = parser.parse_args()
    config = read_json(ROOT / "data/site.json")
    if set(args.researcher) - {r["id"] for r in config["researchers"]}:
        parser.error("Unknown researcher id; add it to data/site.json first.")
    if set(args.lab) - {lab["id"] for lab in config["labs"]}:
        parser.error("Unknown lab id; add it to data/site.json first.")
    paper = fetch_paper(args.paper)
    paper["researcherIds"], paper["labIds"] = args.researcher, args.lab
    path = ROOT / "data/papers.json"
    papers = read_json(path) if path.exists() else []
    papers = [item for item in papers if item["id"] != paper["id"]] + [paper]
    write_json(path, papers)
    print("Imported: " + paper["title"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, ET.ParseError) as error:
        sys.exit(str(error))
