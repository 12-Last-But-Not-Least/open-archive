"""Generate a static website from readable HTML templates and JSON data."""

import argparse
import os
import re
import shutil
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from common import ROOT

ACCOUNT_LINK = ""
API_ROOT = ""


def text(value):
    return escape(str(value), quote=True)


def valid_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTPS URL: " + value)
    return value


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
    # The build contains only shells. The persistent backend owns all directory data.
    config = {"researchers": [], "labs": []}
    papers = []
    output = ROOT / "dist"
    # Only this script's generated output is removed on a rebuild.
    if output.exists():
        shutil.rmtree(output)
    (output / "assets").mkdir(parents=True)
    for asset in ("style.css", "app.js"):
        shutil.copyfile(ROOT / "web" / asset, output / "assets" / asset)
    shutil.copytree(ROOT / "data", output / "data")
    if (ROOT / "LLM Transcripts").exists():
        shutil.copytree(ROOT / "LLM Transcripts", output / "LLM Transcripts")
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
    print(f"Built {4 + len(config['researchers']) + len(config['labs'])} pages in {output}")


if __name__ == "__main__":
    main()
