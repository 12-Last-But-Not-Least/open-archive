# Browser previews

Every PR (including new commits) runs the same scenarios against its base commit
and proposed merge. Each runs in Chromium at desktop and Pixel 7 viewport sizes.
Main pushes and manual runs capture the current version only.

Open **Checks → Browser previews → Details**, then download `browser-preview-before`
and `browser-preview-after` from the workflow run. PNGs are under `test-results`;
open the extracted report with `npx playwright show-report path/to/playwright-report`.
Reports contain named screenshot attachments and failure traces, retained for 14 days.
After a successful PR run, **Post visual preview** compares matching screenshots
with Pixelmatch. Unchanged images are omitted. Changed pixels are pink over a faded
image; differently sized pages are aligned at the top-left and padded with white.
Added/removed screenshots are labeled and use a blank panel for the missing side.
Small antialiasing differences are ignored (Pixelmatch threshold 0.1, includeAA false).
There is no LLM, API key, or inference cost.

The `visual-diff-report` artifact contains `index.html`, before/after/diff PNGs,
and `report.json`. Open `index.html` directly after extracting it.
One `github-actions[bot]` PR comment displays the six comparisons with the largest
changed-pixel counts, with links to both reports. A run with no changes updates the
same comment to say no visual changes were detected in covered screenshots.
Visual differences are informational; they do not fail CI. Browser failures still do.

## Activation and publishing

Merge both workflow files and scripts into the default branch to enable publication.
The publishing workflow uses `workflow_run`, so it must already exist on the default
branch. Then open/update a PR (or rerun Browser previews). Existing PRs do not get a
comment until a new successful run. Failed runs retain Playwright failure artifacts;
an earlier comment remains explicitly labeled with its commit.

PNG images are stored on the automatically created `pr-preview-images` branch of
this public repository, and embedded using immutable commit URLs. Images remain in
Git history after the 14-day workflow artifacts expire. No Pages setup or external
hosting account is needed. The workflow needs contents-write and pull-requests-write
permission; repository/organization rules must allow its image-branch writes.

The comparison job has read-only permissions and treats downloaded images as data.
Only the final publishing job has write permissions. Both use scripts from the
default branch, never execute PR code, validate names/report fields, and check that
the source run still belongs to the PR's current head before updating its comment.

To compare two local captures:

```sh
node scripts/visual-diff.js path/to/before/visual path/to/after/visual visual-diff
```

## Local use

Create and activate a Python virtual environment first (`python -m venv .venv`;
`.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on macOS/Linux).

```sh
python -m pip install -r requirements.txt
npm ci
npx playwright install chromium
npm run test:browser
npm run report:browser
```

Use `npm run test:browser:ui` for interactive debugging. Port 8000 must be free.
The suite starts the real local FastAPI app via `scripts/dev.py`, which rebuilds
`dist/`. Test state lives in the ignored `.local/playwright` directory.
`PLAYWRIGHT_APP_DIR` optionally selects another checkout to render with this suite.

## Coverage and fixtures

- Home and expanded account instructions.
- Researcher and lab directories: populated, empty, unavailable.
- Publications, researcher profile, lab profile, and expanded abstracts.
- Empty and unavailable publications.
- Signed-out, new-user, member, owner, and admin accounts.
- Lab creation form and generated invitation links.
- Paper search, author selection, selected papers.
- Invitation sign-in, author selection, join, expired, and joined states.
  The initial invitation screenshot includes the popup when present (PR #16);
  the following capture shows the card after dismissing it.

`fixtures.js` intercepts API and data requests with fixed sample responses.
No real login, publication writes, invitations, or external services are used.
Unexpected API/external calls and uncaught JavaScript errors fail the test.
These are UI checks; backend integration tests remain separate.

Add a scenario in `pages.spec.js`, wait for its visible ready state with `expect`,
and call `capture(page, testInfo, 'descriptive-name')`. It runs at both sizes and
on both PR versions automatically. New routes absent from the base branch need
an explicit base-version expectation. Do not silently skip failed navigation.
