"""Generate a static website from readable HTML templates and JSON data."""

import argparse
import os
import re
import shutil
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from common import ROOT, read_json

ACCOUNT_LINK = ""
API_ROOT = ""


def text(value):
    return escape(str(value), quote=True)


def valid_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTPS URL: " + value)
    return value


def validate(config, papers):
    for group in (config["researchers"], config["labs"]):
        ids = [item["id"] for item in group]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", id) for id in ids):
            raise ValueError("Page ids must be unique lowercase slugs.")
    researchers = {item["id"] for item in config["researchers"]}
    labs = {item["id"] for item in config["labs"]}
    for lab in config["labs"]:
        if set(lab["members"]) - researchers:
            raise ValueError("A lab references an unknown researcher.")
    for paper in papers:
        if not paper["title"] or not paper["authors"]:
            raise ValueError("A paper needs a title and authors.")
        valid_url(paper["url"])
        if paper.get("projectURL"):
            valid_url(paper["projectURL"])
        datetime.fromisoformat(paper["publishedAt"].replace("Z", "+00:00"))
        if set(paper["researcherIds"]) - researchers or set(paper["labIds"]) - labs:
            raise ValueError("A paper references an unknown researcher or lab.")


def directory(items, kind, root):
    return "\n".join(
        '<div class="directory-item"><h3><a href="' + root + kind + "/" + text(item["id"]) + '/">'
        + text(item["name"]) + '</a></h3><p>' + text(item["institution"]) + "</p></div>"
        for item in items
    )


def publications(papers):
    result = []
    for paper in sorted(papers, key=lambda paper: paper["publishedAt"], reverse=True):
        date = datetime.fromisoformat(paper["publishedAt"].replace("Z", "+00:00")).strftime("%b %d, %Y")
        project = (' <a href="' + text(paper["projectURL"]) + '">Project</a>') if paper.get("projectURL") else ""
        citation = "arXiv:" + paper["arxivId"] if paper.get("arxivId") else paper.get("venue", "doi:" + paper.get("doi", ""))
        abstract = '<details><summary>Abstract</summary><p>' + text(paper["abstract"]) + '</p></details>' if paper.get("abstract") else ""
        result.append(
            '<article class="publication"><h3><a href="' + text(paper["url"]) + '">' + text(paper["title"]) + '</a></h3>'
            + '<p class="authors">' + text(", ".join(paper["authors"])) + '</p>'
            + '<p class="meta">' + date + ' · ' + text(citation) + '</p>'
            + '<p class="links"><a href="' + text(paper["url"]) + '">Paper</a>' + project + '</p>'
            + abstract + '</article>'
        )
    return "\n".join(result) or '<p class="empty">No publications added yet.</p>'


def render(template, destination, values):
    content = (ROOT / "web" / template).read_text(encoding="utf-8")
    content = content.replace("{{ACCOUNT_LINK}}", ACCOUNT_LINK)
    content = content.replace("{{API_ROOT}}", text(API_ROOT))
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", str(value))
    if re.search(r"\{\{[A-Z_]+\}\}", content):
        raise ValueError("Unfilled HTML template field in " + template)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def main():
    global ACCOUNT_LINK, API_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-account", action="store_true", help="Include the local FastAPI account page in navigation")
    parser.add_argument("--backend-url", default=os.environ.get("OPEN_ARCHIVE_BACKEND_URL", ""), help="Hosted backend HTTPS origin for GitHub Pages")
    args = parser.parse_args()
    if args.with_account:
        ACCOUNT_LINK = ' | <a href="/account/">Account</a>'
    elif args.backend_url:
        API_ROOT = valid_url(args.backend_url.rstrip("/"))
        parsed = urlparse(API_ROOT)
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Backend URL must be an HTTPS origin without a path.")
        ACCOUNT_LINK = ' | <a href="' + text(API_ROOT) + '/account/">Account</a>'
    config, papers = read_json(ROOT / "data/site.json"), read_json(ROOT / "data/papers.json")
    validate(config, papers)
    # Claimed profiles are rendered from the PDS in the browser. Imported
    # suggestions must not resurrect deleted records, even before JS loads.
    unclaimed = {r["id"] for r in config["researchers"] if not r.get("did")}
    papers = [{**p, "researcherIds": [id for id in p["researcherIds"] if id in unclaimed]} for p in papers]
    papers = [p for p in papers if p["researcherIds"]]
    if args.with_account or API_ROOT:
        # Local claim approvals live in SQLite and can change without a rebuild.
        # Render from the runtime configuration, never from stale HTML imports.
        papers = []
    output = ROOT / "dist"
    # Only this script's generated output is removed on a rebuild.
    if output.exists():
        shutil.rmtree(output)
    (output / "assets").mkdir(parents=True)
    for asset in ("style.css", "app.js"):
        shutil.copyfile(ROOT / "web" / asset, output / "assets" / asset)
    shutil.copytree(ROOT / "data", output / "data")
    (output / ".nojekyll").touch()
    render("index.html", output / "index.html", {"ROOT": "./"})
    render("publications.html", output / "publications" / "index.html", {
        "ROOT": "../", "PUBLICATIONS": publications(papers),
    })
    for kind in ("researchers", "labs"):
        render("directory.html", output / kind / "index.html", {
            "ROOT": "../", "TITLE": kind.capitalize(), "KIND": kind,
            "ENTRIES": directory(config[kind], kind, "../"),
            "RESEARCHERS_CURRENT": 'aria-current="page"' if kind == "researchers" else "",
            "LABS_CURRENT": 'aria-current="page"' if kind == "labs" else "",
        })
    for researcher in config["researchers"]:
        affiliations = [lab for lab in config["labs"] if researcher["id"] in lab["members"]]
        account = ('<a href="https://bsky.app/profile/' + text(researcher["did"]) + '">@' + text(researcher["handle"]) + '</a>') if researcher.get("did") else "No ATProto account linked yet."
        render("researcher.html", output / "researchers" / researcher["id"] / "index.html", {
            "ROOT": "../../", "ID": text(researcher["id"]), "NAME": text(researcher["name"]),
            "INSTITUTION": text(researcher["institution"]), "ABOUT": text(researcher["about"]),
            "WEBSITE": text(valid_url(researcher["website"])), "ACCOUNT": account,
            "AFFILIATIONS": " · ".join('<a href="../../labs/' + text(lab["id"]) + '/">' + text(lab["shortName"]) + '</a>' for lab in affiliations),
            "PUBLICATIONS": publications([paper for paper in papers if researcher["id"] in paper["researcherIds"]]),
        })
    for lab in config["labs"]:
        members = [researcher for researcher in config["researchers"] if researcher["id"] in lab["members"]]
        render("lab.html", output / "labs" / lab["id"] / "index.html", {
            "ROOT": "../../", "ID": text(lab["id"]), "NAME": text(lab["name"]), "SHORT_NAME": text(lab["shortName"]),
            "INSTITUTION": text(lab["institution"]), "ABOUT": text(lab["about"]), "MEMBER_COUNT": len(members),
            "MEMBERS": directory(members, "researchers", "../../"),
            "LAB_LABEL": "Lab page · demo" if lab.get("demo") else "Lab page",
            "DEMO_NOTE": '<p class="note">Demo page. Not the lab’s official website.</p>' if lab.get("demo") else "",
            "PUBLICATIONS": publications([paper for paper in papers if lab["id"] in paper["labIds"]]),
        })
    print(f"Built {4 + len(config['researchers']) + len(config['labs'])} pages in {output}")


if __name__ == "__main__":
    main()
