# open archive

Researcher profiles and lab pages that read public publication records from ATProto.

First draft: Bryce Hackel's profile, N2D Haptic Glove and Skill Tests publications, and a demo ARC Lab page. **Skill Tests is not affiliated with ARC Lab** and appears only on the researcher profile and main publication list. The lab page is labeled as a demo, not an official lab website or complete roster. Talks and posters are not implemented yet.

## Run it

Python 3.10+ is the only requirement for the website. No packages to install.

```bash
python3 scripts/build.py
python3 -m http.server 8000 --directory dist
```

Open http://localhost:8000. Use an HTTP server instead of opening HTML files directly, so the browser can fetch the JSON files.

## Edit it

- `web/index.html`: homepage.
- `web/researcher.html` and `web/lab.html`: page templates.
- `web/style.css`: all styling.
- `web/app.js`: public ATProto reads and publication rendering.
- `data/site.json`: researchers, stable account DIDs, labs, and approved lab memberships.
- `data/papers.json`: imported paper metadata and paper-specific lab affiliations.
- `lexicons/publication.json`: our draft publication record schema.

Rebuild after changing templates or JSON. Pages are generated under `/researchers/<id>/` and `/labs/<id>/`. Relative links work on both localhost and the GitHub Pages project URL.

## Add a paper

Import arXiv metadata once:

```bash
python3 scripts/import_arxiv.py https://arxiv.org/abs/2606.14083 --researcher bryce-hackel --lab ucsd-arc
```

This changes the local JSON catalog. Omit `--lab` for work not affiliated with that lab. Make separate import requests at least three seconds apart; use arXiv's bulk interfaces for large catalogs.

Publish an authorship record to your own PDS:

```bash
python3 scripts/publish_publication.py 2606.14083 --handle bhackel.bsky.social --lab ucsd-arc
```

For a DOI paper already in `data/papers.json`, pass its id:

```bash
python3 scripts/publish_publication.py doi:10.1145/3770762.3772518 --handle bhackel.bsky.social
```

The script prompts for an app password with input hidden, authenticates against the account's current PDS, and creates a public publication record. It does not save credentials or print tokens, and deletes the temporary session on completion. Use `--dry-run` to inspect the record without signing in. A stable record key makes repeat runs safe: an existing record is reported and left unchanged.

The PDS record contains enough metadata to display a new paper even if it isn't in the site's imported catalog. Refreshing the site reads it immediately without redeployment. Imported entries remain available if a PDS is unavailable; the page status reports this. Matching arXiv ids or DOIs are deduplicated. Records are self-published claims, not independent authorship verification.

## Link another account

```bash
python3 scripts/link_account.py your-handle.bsky.social --researcher bryce-hackel
```

This only reads public account details and saves the handle and DID in `data/site.json`. It needs no password. Adding a different person starts with a researcher entry in that file. Lab membership changes are made in `data/site.json` after manual email approval. The public site has no login or approval interface.

## Deploy

The GitHub Actions workflow builds and deploys `dist/` on pushes to `main`. In repository Settings → Pages, select **GitHub Actions** as the source. Scripts and credentials are not included in the deployment. There is no scheduled scraping or backend server.

## Check it

Node.js 20+ runs the public-record tests; no npm dependencies are needed.

```bash
node --test tests/*.test.js
python3 scripts/build.py
```

## Data sources

- [N2D Haptic Glove on arXiv](https://arxiv.org/abs/2606.14083), metadata from the arXiv Atom API.
- [Skill Tests at Scale](https://doi.org/10.1145/3770762.3772518), publisher-deposited metadata from Crossref; [author's publication list](https://lau.ucsd.edu/).
- [ATProto repository reads and writes](https://atproto.com/guides/reads-and-writes).

Papers are linked at their sources; PDFs are not copied into this repository. The collection namespace uses the organization's `12-last-but-not-least.github.io` domain. This is a draft schema stored in the repository; network Lexicon publication and wider interoperability are future work.
