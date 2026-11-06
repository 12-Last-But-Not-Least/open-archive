# open archive

Researcher profiles and lab pages that read public publication records from ATProto.

First draft: Bryce Hackel's profile, N2D Haptic Glove and Skill Tests publications, and a demo ARC Lab page. **Skill Tests is not affiliated with ARC Lab** and appears only on the researcher profile and main publication list. The lab page is labeled as a demo, not an official lab website or complete roster. Talks and posters are not implemented yet.

## Run the FastAPI prototype

Python 3.12+:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/dev.py
```

Open **http://127.0.0.1:8000/account/**. Use `127.0.0.1`, not `localhost`, so the OAuth callback and session cookie share the same host. API docs are at `/api/docs`.

Enter your ATProto handle and authorize on your account provider's sign-in page. The app requests create/delete access only to our publication collection. No app password is entered into Open Archive. The local OAuth client uses [ATProto's loopback development flow](https://atproto.com/specs/oauth#localhost-client-development) and the pinned [Python OAuth library](https://github.com/zzstoatzz/atproto-oauth).

The account page shows your PDS publications and imported suggestions. Select papers and associated labs, then choose **Add selected papers**. Existing arXiv IDs or DOIs are skipped. **Find papers on arXiv** runs a separate search only when submitted; search results do not publish anything. Remove deletes that publication record from your own PDS.

SQLite stores profile claim requests and approved account links. Session credentials, temporary OAuth state, and suggestions are encrypted. Runtime files and the encryption key are under ignored `.local/`; retain that directory to keep sessions and approvals across restarts. Publications themselves remain in the PDS.

This is a local, single-worker prototype, bound to `127.0.0.1`. A hosted backend is not deployed. GitHub Pages remains the public read-only site. Deploying login later requires HTTPS, public OAuth client metadata, a persistent runtime data directory, and a review of the OAuth library's server-side networking protections. The prototype deliberately rejects non-loopback origins.

## Run only the static website

Python 3.10+ is the only requirement for the website. No packages to install.

```bash
python3 scripts/build.py
python3 -m http.server 8000 --directory dist
```

Open http://localhost:8000. Use an HTTP server instead of opening HTML files directly, so the browser can fetch the JSON files.

## Edit it

- `web/index.html`: front page with just the site name and navigation.
- `web/publications.html`: publication list.
- `web/directory.html`: researcher and lab directories.
- `web/researcher.html` and `web/lab.html`: page templates.
- `web/style.css`: all styling.
- `web/app.js`: public ATProto reads and publication rendering.
- `web/account.html` and `web/account.js`: plain sign-in and publication management page.
- `backend/app.py`: FastAPI routes and browser sessions.
- `backend/oauth.py` and `backend/storage.py`: OAuth integration and encrypted SQLite storage.
- `backend/papers.py`: on-demand arXiv search and PDS publication operations.
- `data/site.json`: researchers, stable account DIDs, labs, and approved lab memberships.
- `data/papers.json`: imported paper metadata and paper-specific lab affiliations.
- `lexicons/publication.json`: our draft publication record schema.

Rebuild after changing templates or JSON. The main pages are `/publications/`, `/researchers/`, and `/labs/`. Individual pages are generated under `/researchers/<id>/` and `/labs/<id>/`. Relative links work on both localhost and the GitHub Pages project URL.

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

The PDS record contains enough metadata to display a new paper even if it isn't in the site's imported catalog. Refreshing the site reads it immediately without redeployment. **For linked researchers, the PDS is the authoritative publication list.** Imported entries appear only on unclaimed profiles or as suggestions in the account page. An empty or unavailable PDS does not restore imported publications. Records are self-published claims, not independent authorship verification.

## Link another account

```bash
python3 scripts/link_account.py your-handle.bsky.social --researcher bryce-hackel
```

This maintainer script only reads public account details and saves the handle and DID in `data/site.json`. It needs no password and does not verify the human's identity. Use it only after approval. Adding a different person starts with a researcher entry in that file, with empty `handle` and `did` fields. Lab membership changes are made in `data/site.json` after manual email approval.

In the backend prototype, an unlinked account can request an existing unclaimed profile. Requests remain pending until a maintainer manually verifies the researcher by email and runs:

```bash
.venv/bin/python -m backend.claims list
.venv/bin/python -m backend.claims approve did:plc:EXAMPLE researcher-id
```

No approval emails are sent automatically. Signing in alone does not claim a researcher profile. An approved profile keeps its existing URL; its publications come from the linked PDS. The local backend serves the approved mappings immediately. Publishing that mapping to the static GitHub Pages site still requires updating `data/site.json` and pushing. Bryce's existing DID and both published records remain in place.

## Deploy

The GitHub Actions workflow tests the backend and frontend, builds, and deploys `dist/` on pushes to `main`. In repository Settings → Pages, select **GitHub Actions** as the source. Backend code, sessions, and credentials are not included in the Pages deployment. The account navigation link is enabled only by the local development build. There is no scheduled scraping.

## Check it

Node.js 20+ runs the public-record tests; no npm dependencies are needed.

```bash
node --test tests/*.test.js
.venv/bin/python -m pytest -q tests/test_backend.py
python3 scripts/build.py
```

Tests use a fake authorization server and PDS to cover session isolation, callback binding, CSRF, manual claim approval, selected imports, duplicate prevention, and deletion. They do not write to real accounts. If unrelated system pytest plugins are installed (for example ROS), prefix the Python test command with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## Data sources

- [N2D Haptic Glove on arXiv](https://arxiv.org/abs/2606.14083), metadata from the arXiv Atom API.
- [Skill Tests at Scale](https://doi.org/10.1145/3770762.3772518), publisher-deposited metadata from Crossref; [author's publication list](https://lau.ucsd.edu/).
- [ATProto repository reads and writes](https://atproto.com/guides/reads-and-writes).

Papers are linked at their sources; PDFs are not copied into this repository. The collection namespace uses the organization's `12-last-but-not-least.github.io` domain. This is a draft schema stored in the repository; network Lexicon publication and wider interoperability are future work.
