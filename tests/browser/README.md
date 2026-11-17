# Browser previews

Every PR (including new commits) runs the same scenarios against its base commit
and proposed merge. Each runs in Chromium at desktop and Pixel 7 viewport sizes.
Main pushes and manual runs capture the current version only.

Open **Checks → Browser previews → Details**, then download `browser-preview-before`
and `browser-preview-after` from the workflow run. PNGs are under `test-results`;
open the extracted report with `npx playwright show-report path/to/playwright-report`.
Reports contain named screenshot attachments and failure traces, retained for 14 days.
This produces review images; it does not enforce pixel baselines or publish images
in PR comments. Passing means the covered UI states rendered and their assertions passed,
not that there were no visual changes.

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
