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

The development command binds to `127.0.0.1`. The same app can run on Railway using the deployment below. Hosted login uses HTTPS, secure cookies, and a persistent OAuth signing key. ATProto discovery requests check DNS results and connect only to validated public IPs, preserving TLS hostname verification.

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
- `backend/network.py`: public-IP-only ATProto discovery and requests.
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

An unlinked account can claim an existing unclaimed profile immediately when its Bluesky display name matches the researcher's full name. Select your profile on the account page and click **Claim profile**. The server reads the public profile record from the signed-in account's PDS, ignoring capitalization and extra whitespace when comparing names. Missing or different names, duplicate researcher names, and already-linked profiles are rejected. Existing pending requests can be resubmitted; no maintainer approval is required.

Name matching is an MVP convenience, not identity verification: display names can be changed or copied. Signing in alone does not claim a profile. A claimed profile keeps its URL and reads publications from its linked PDS. The backend serves these mappings immediately; GitHub Pages also reads them when configured with the backend URL. Without a backend URL, static mappings require updating `data/site.json` and pushing. Bryce's existing DID and records remain in place. The `backend.claims` CLI remains available for legacy pending requests.

## Deploy the backend automatically

The hosted account page is **https://open-archive-production.up.railway.app/account/**. The public website remains on [GitHub Pages](https://12-last-but-not-least.github.io/open-archive/).

`.github/workflows/backend.yml` tests and builds the Docker container on pushes and pull requests. Once Railway is connected, pushes to `main` deploy it automatically. The workflow uploads a clean Git snapshot and waits until `/api/health` reports the new commit. A successful build alone is not treated as a successful deployment. Without the Railway configuration, checks run and deployment is explicitly skipped.

One-time setup:

1. Create a Railway project and an empty service named `open-archive`. Let Actions own deployments; do not also enable Railway's GitHub autodeploys for this service.
2. Attach a persistent volume at **`/data`**. Start with the smallest capacity offered by the selected plan. Keep one service replica; SQLite and token-refresh locks assume one worker.
3. Select the Dockerfile builder with path `Dockerfile`. Set the healthcheck path to `/api/health`, timeout to 120 seconds, and restart policy to on-failure with three retries. Enable serverless sleeping for the prototype. These settings live in Railway's service configuration.
4. Generate a public Railway domain targeting port **8000**. Set service variables `PORT=8000`, `OPEN_ARCHIVE_ORIGIN` to its `https://…` origin, and `FORWARDED_ALLOW_IPS=*` for Railway's edge proxy. The Dockerfile sets `OPEN_ARCHIVE_STATE_DIR=/data/open-archive`. The app refuses a Railway deployment without its volume.
5. Create a Railway **project token scoped to that environment** in Project Settings → Tokens. Save it as the GitHub repository Actions secret `RAILWAY_TOKEN`; never commit it. Set repository variables `RAILWAY_SERVICE_ID` to the service ID and `OPEN_ARCHIVE_BACKEND_URL` to the HTTPS origin.
6. Run **Publish backend** in GitHub Actions. After it succeeds, run **Publish website** once to put the account link on GitHub Pages. Later pushes run both workflows automatically.

The backend serves the full plain website plus `/account/`, `/api/docs`, and public OAuth metadata. SQLite, the session encryption key, and the OAuth signing key stay on the volume across deployments. Back up that volume before replacing it. Claim approval commands run inside the hosted service using `python -m backend.claims ...`; they do not operate on the laptop's `.local/` database.

The [Railway free plan](https://railway.com/pricing) provides a trial and limited monthly credits, not unlimited hosting. Use the free plan for this prototype and review usage before choosing a paid plan. [Railway deployment docs](https://docs.railway.com/cli/deploying) and [volume docs](https://docs.railway.com/volumes) describe the hosting setup.

## Deploy the public website

The Pages workflow tests the backend and frontend, builds, and deploys `dist/` on pushes to `main`. In repository Settings → Pages, select **GitHub Actions** as the source. Backend code, sessions, and credentials are not included in the Pages deployment. `OPEN_ARCHIVE_BACKEND_URL` adds the hosted account link and lets public pages read approved researcher mappings from the backend. There is no scheduled scraping.

## Check it

Node.js 20+ runs the public-record tests; no npm dependencies are needed.

```bash
node --test tests/*.test.js
.venv/bin/python -m pytest -q tests/test_backend.py tests/test_hosting.py
python3 scripts/build.py
```

Tests use a fake authorization server and PDS to cover session isolation, callback binding, CSRF, name-matched claims, selected imports, duplicate prevention, and deletion. They do not write to real accounts. If unrelated system pytest plugins are installed (for example ROS), prefix the Python test command with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## Data sources

- [N2D Haptic Glove on arXiv](https://arxiv.org/abs/2606.14083), metadata from the arXiv Atom API.
- [Skill Tests at Scale](https://doi.org/10.1145/3770762.3772518), publisher-deposited metadata from Crossref; [author's publication list](https://lau.ucsd.edu/).
- [ATProto repository reads and writes](https://atproto.com/guides/reads-and-writes).

Papers are linked at their sources; PDFs are not copied into this repository. The collection namespace uses the organization's `12-last-but-not-least.github.io` domain. This is a draft schema stored in the repository; network Lexicon publication and wider interoperability are future work.
