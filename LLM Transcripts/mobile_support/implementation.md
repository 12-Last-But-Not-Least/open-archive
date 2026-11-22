# Issue #15 — Improve account and lab pages on mobile

A researcher should be able to create a lab, invite a colleague, or join from a phone. Use the existing two-account walkthrough as the baseline. The issue budgets four engineer-hours including audit, fixes, testing, and review. [Issue #15](https://github.com/12-Last-But-Not-Least/open-archive/issues/15) · [MVP walkthrough and local setup](../../README.md)

## Local review evidence

Baseline Chromium audit on November 17, 2026, at 390px and 768px. The owner saved a 112-character unbroken researcher name and created a lab with a 148-character name and a 160-character institution. These values fit the existing form limits.

| Reproduction | Observed before editing | Expected |
| --- | --- | --- |
| Open the owner's profile and lab forms, then view the populated account. | The profile link and lab text widened the page at both widths. At 390px the expanded account measured 863px wide. | Read the text and reach controls without scrolling sideways. |
| Open the copied invitation in a fresh session. | The long lab name exceeded the dialog's inner width. At 768px its content used 1579px inside 446px. | Read the full name inside the dialog. |
| Close the dialog with Escape or keyboard-activate Continue. | Focus fell to the document body in signed-out, needs-profile, and ready-to-join sessions. | Focus the next action indicated by the invitation. |
| Open the populated public lab page and refresh its publications. | The long name and institution overflowed at both widths. At 768px the document measured 2098px wide. | Read all details within the viewport. |

At both widths the owner could select and copy the complete invitation URL. Replacing it produced a dismissible error on the old link. The member could complete sign-in, profile creation, joining, and adding a paper. The visitor could read the resulting publication. Local before screenshots are under `../../.local/evidence`.

**Retest.** Scoped text wrapping kept the account and public lab documents at exactly 390px and 768px throughout the audited states, including a public paper title containing a 153-character word. The invitation dialog's content fit its 349px and 446px inner widths. After Escape or Continue, focus reached the sign-in field, profile disclosure, or Join lab button according to the researcher's state. The profile disclosure opened with Enter, Tab reached the name field, and Shift+Tab returned to the visible disclosure focus indicator. The existing invitation field passed the copy-and-open check.

**Local checks.** All 35 unit tests, 46 desktop/mobile browser tests, and both MVP integration tests passed. The MVP test now checks long-name overflow and focus after closing the invitation. The local comparison with the branch's current commit covered 80 preview screenshots. Six changed, all in the three invitation states immediately after dialog dismissal on desktop and mobile. Review the [local visual comparison](../../.local/review-diff/index.html) and [before-and-after audit screenshots](.local/evidence/). These artifacts are local to this checkout. The browser checks used Chromium and disposable accounts with simulated upstream sign-in.

## 1. Audit before changing styles

Use separate browser sessions for the owner, invited researcher, and signed-out visitor. Run each journey at **390px and 768px viewport widths** with disposable test data. Use a fresh invitee session for each starting state so a previously dismissed dialog or saved token cannot mask a first-visit problem.

| Journey | What to exercise |
| --- | --- |
| Owner creates a lab | Sign in, save a researcher profile, create a lab, and generate an invitation. Copy the entire URL from the field and use that copy in the invitee session. Check that labels, fields, and member-management actions stay readable and reachable. |
| Researcher accepts | Start signed out, signed in without a profile, and signed in with a profile. In each case, verify the dialog names the lab and the page identifies the next required action. Follow the invitation through any sign-in and profile steps, choose **Join lab**, and reload to confirm membership. |
| Researcher opens a replaced link | Generate a second invitation and open the first in a fresh session. Read the error, choose **Dismiss invitation**, and verify the account page remains usable. |
| Visitor opens the public lab | Add a paper from the member account. Open the public lab page while signed out. Read the member list and publication, follow their links, and operate **Refresh**. |

Exercise the relevant screens with long, valid lab and researcher names, institutions, and paper titles. Include expanded account forms and populated member and publication lists. For each defect, record the viewport width, page, user state, steps, observed result, and expected result before editing. Capture a baseline screenshot for visible defects. Record a pass for each state that works.

## 2. Fix confirmed obstacles

- Address blockers to signing in, saving a profile, sharing a usable invitation, and joining first. Retain the existing typography, colors, borders, native controls, and invitation styling.
- Fix overflow at the affected component. Let text wrap, fields fit their containers, and adjacent actions move onto another line when needed. Keep labels with their controls and preserve reading and tab order. Page-wide overflow clipping can hide content and focus indicators.
- Allow the invitation field to scroll internally if the full URL can be selected, copied, and opened on a phone. Add a **Copy link** action only if the audit demonstrates a copying problem.
- Start with scoped rules in the [existing stylesheet](../../web/style.css). Adjust markup or focus handling where a reproduced flow or keyboard defect requires it. The planned changes are confined to the presentation layer.

## 3. Verify and close

- Repeat each affected scenario with the same data at both exact widths. Verify readable text, reachable controls, and freedom from unintended page-level horizontal scrolling. Use Tab and Shift+Tab to check focus visibility and order, operate the disclosures, and dismiss the invitation dialog with **Continue** and **Escape** on separate visits. After dismissal, verify that the next keyboard action makes sense for the current invitation step.
- Run `npm test`, `npm run test:browser`, and `npm run test:mvp`. Record the desktop and Pixel 7 suite results alongside the two exact-width audit results. Add a focused regression check when existing tests would miss a confirmed defect.
- Inspect the PR’s before-and-after browser previews for unintended changes to shared layouts. Pair each baseline finding with its fix and retest result. If every audited state passes initially, document the evidence and test results. [Browser preview guide](../../tests/browser/README.md)
- Include `Closes #15`, audit results for both widths, keyboard findings, and test outcomes in the implementation PR. Mark each issue criterion complete when its evidence is available. Export and include the current transcript before preparing each commit for push, per [repository instructions](../../AGENTS.md). After review and merge, confirm the issue closed.

## Pull request message

**Title** — Fix mobile text overflow and keyboard focus for lab invitations

```markdown
Long names, institutions, and paper titles now wrap on account and public lab pages. Long lab names also fit inside the invitation dialog. Closing the dialog with Escape or Continue focuses the sign-in field, profile disclosure, or Join lab button, depending on the researcher's next step.

Verified the two-account flow in Chromium at 390px and 768px. At both widths, researchers could copy an invitation, join the lab, and publish a paper without sideways scrolling. All 35 unit tests, 46 browser tests, and 2 MVP integration tests passed, including new checks for overflow and focus.

Closes #15
```
