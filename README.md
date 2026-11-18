# Open Archive

[Open the site](https://open-archive-production.up.railway.app/).

Sign in with your AT Protocol handle (for example `username.bsky.social`). In Account, create a researcher profile with your name and institution, or choose your author name from publication search. You do not need a paper to have a profile.

Create a lab, share its invitation link, and let members join with their own accounts. The creator owns the lab; owners can assign admins, and admins can invite/remove members. Public lab pages collect publications from current members, with coauthor duplicates combined. Add papers through search in Account; publication records are written to your own AT Protocol account. Pages read records on load or Refresh, not through a background polling job.

## Data and deployment

SQLite on the persistent backend volume is the only source for researchers, labs, and membership. AT Protocol accounts are the source for publications. The repository ships no researcher, lab, or publication examples. GitHub Pages hosts directory/publication shells configured with `OPEN_ARCHIVE_BACKEND_URL`; profile links go to the live backend. Run `python scripts/dev.py` locally after installing `requirements.txt`.

This revision removes the bundled Bryce/ARC Lab examples, JSON import/publish/account-link scripts, and legacy name-claim endpoint. Existing database directory entries, lab roles, invitation links, OAuth credentials, and AT Protocol publications are retained. Old claim/tombstone tables, if present, are ignored. Profiles that existed only in the bundled JSON or legacy claims must be created again through Account. This does not delete their AT Protocol papers; those appear after the same account creates a profile. ARC Lab must be created through the UI if it is wanted as a real lab.

Profile deletion hides the person from the directory but preserves their lab roles and publications, so an owner can recreate a profile without orphaning a lab. Lab membership can only change through invitation/member endpoints, not profile fields. Deprecated `labIds` profile/import fields and `professorName` creation fields are rejected; old publication tags are not used to build lab feeds.

## Checks

`python scripts/build.py` then `python -m pytest -q tests/test_backend.py tests/test_hosting.py`

`npm ci` then `npm test` and `npm run test:browser`.

Browser screenshots use fixtures contained in tests, never production seed data. See [browser test instructions](tests/browser/README.md).


## MVP and demo flow

The MVP is a public lab page assembled from its members' publication profiles:

1. Sign in with Bluesky and save a researcher profile (no paper required).
2. Create a lab with its institution and description. The creator becomes its owner and first member.
3. Generate an invitation link in Your labs. Open it in another browser profile, sign in as a different researcher, create their profile and click Join lab.
4. Search for a paper in the member account and add it. Open the public researcher and lab pages while signed out; both should show it.
5. Reload the pages to verify saved data. When a member leaves, their papers leave the lab feed but remain on their own profile.

Public pages update on opening or Refresh. Collaborator matching, custom site themes, AI features and comprehensive cross-source paper deduplication are outside this MVP.

### Two-account browser integration test

After installing Python requirements and `npm ci`, run `npx playwright install chromium` and `npm run test:mvp`. The required backend check runs this on desktop and mobile before building the container.

This test drives the real application UI and backend using three independent browser sessions (owner, invited member, anonymous visitor). It exercises login callbacks, profile creation, lab creation, invitation persistence across sign-in, joining, publication writes, public feeds, reloads and leaving the lab. Only the upstream OAuth provider, search services and PDS are simulated; application API requests are not intercepted. Existing backend tests additionally verify persistence across application restart.

The test server binds only to `127.0.0.1:8001`, uses a temporary database removed at shutdown, and is not included in the production container. It cannot verify live Bluesky authorization or third-party service availability; the real two-account smoke test above is still needed before the demo. Failure reports and traces are uploaded as `mvp-browser-report` in GitHub Actions.
