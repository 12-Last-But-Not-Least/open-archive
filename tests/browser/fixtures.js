import {test as base, expect} from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

export const researcher = {id: 'bryce-hackel', name: 'Bryce Hackel', did: 'did:plc:aaaaaaaaaaaaaaaaaaaaaaaa', handle: 'researcher.example', institution: 'Example University'};
export const lab = {id: 'ucsd-arc', name: 'Advanced Robotics and Controls Lab', institution: researcher.institution,
  members: [researcher.id], about: 'Robotics, haptics, and human interaction.', contactEmail: 'lab@example.test'};
export const paper = {id: 'sample-paper', rkey: 'sample-paper', title: 'A reproducible study of robotic touch',
  authors: [researcher.name, 'Alex Example'], publishedAt: '2025-01-15T00:00:00Z',
  abstract: 'A fixed example abstract for reviewing publication layouts across screen sizes.',
  url: 'https://example.test/paper', doi: '10.1234/example', researcherIds: [researcher.id], labIds: [lab.id]};

export const test = base.extend({
  mock: async ({page}, use) => {
    const state = {signedIn: false, profile: false, role: null, empty: false, error: false, invalidInvite: false};
    const unexpected = [];
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      if (url.origin === 'https://plc.directory' && p === '/did:plc:aaaaaaaaaaaaaaaaaaaaaaaa')
        return route.fulfill({json: {id: researcher.did, service: [{type: 'AtprotoPersonalDataServer', serviceEndpoint: 'https://pds.example.test'}]}});
      if (url.origin === 'https://pds.example.test' && p === '/xrpc/com.atproto.repo.listRecords')
        return route.fulfill({json: {records: state.empty ? [] : [{uri: `at://${researcher.did}/io.github.12-last-but-not-least.publication/sample-paper`, value: {...paper, $type: 'io.github.12-last-but-not-least.publication'}}]}});
      if (url.origin !== 'http://127.0.0.1:8000') {
        unexpected.push(url.origin + p);
        return route.abort();
      }
      const json = data => route.fulfill({json: data});
      // Isolated screenshot fixtures use the app's real templates, not production seed records.
      if (p === '/researchers/bryce-hackel/' || p === '/labs/ucsd-arc/') {
        const kind = p.startsWith('/labs/') ? 'lab' : 'researcher';
        const entry = kind === 'lab' ? lab : researcher;
        const template = await fs.readFile(path.resolve(process.env.PLAYWRIGHT_APP_DIR || '.', 'web', `${kind}.html`), 'utf8');
        const values = {ROOT: '/', API_ROOT: '', ACCOUNT_LINK: ' | <a href="/account/">Account</a>',
          ID: entry.id, NAME: entry.name, SHORT_NAME: entry.name, INSTITUTION: entry.institution,
          MEMBER_COUNT: '1', MEMBERS: '', PUBLICATIONS: '', AFFILIATIONS: '', ACCOUNT: ''};
        return route.fulfill({contentType: 'text/html', body: template.replace(/\{\{([A-Z_]+)\}\}/g, (_, key) => values[key] || '')});
      }

      if (p === '/data/site.json') {
        if (state.error) return route.fulfill({status: 503, json: {detail: 'Unavailable'}});
        return json({collection: 'io.github.12-last-but-not-least.publication',
          researchers: state.empty ? [] : [researcher], labs: state.empty ? [] : [lab]});
      }
      if (p === '/data/papers.json') return json(state.empty ? [] : [paper]);
      if (p === '/api/session') return json({csrf: 'fixture', user: state.signedIn ? {did: 'did:example:reviewer', handle: 'reviewer.test'} : null,
        researcher: state.profile ? (state.researcher || researcher) : null});
      if (p === '/api/my-labs') return json({labs: state.role ? [{...lab, role: state.role,
        people: [{name: researcher.name, did: 'did:example:reviewer', role: state.role},
          {name: 'Alex Example', did: 'did:example:alex', role: 'member'}]}] : []});
      if (p === '/api/suggestions') return json({papers: []});
      if (p === '/api/publications') return json({papers: state.profile ? [paper] : []});
      if (p === '/api/papers/search') return json({papers: [paper], authors: [{name: researcher.name, paperId: paper.id}]});
      if (p === '/api/profile' && route.request().method() === 'PUT') { state.profile = true; state.researcher = {...researcher, ...route.request().postDataJSON()}; return json({researcher: state.researcher}); }
      if (p === '/api/profile/author') { state.profile = true; return json({researcher}); }
      if (p === '/api/lab-invites/preview-token') return state.invalidInvite
        ? route.fulfill({status: 404, json: {detail: 'Invitation not found or expired.'}}) : json({lab});
      if (p === '/api/labs/ucsd-arc/invite') return json({url: 'http://127.0.0.1:8000/account/?invite=preview-token'});
      if (p === '/api/labs/ucsd-arc/join') { state.role = 'member'; return json({ok: true}); }
      if (p.startsWith('/api/') || p.startsWith('/auth/')) {
        unexpected.push(p); return route.fulfill({status: 501, json: {detail: 'Missing browser fixture'}});
      }
      return route.continue();
    });
    await use(state);
    expect(unexpected, 'Every API and external request must have an explicit fixture').toEqual([]);
    expect(errors, 'No uncaught browser errors').toEqual([]);
  },
});
export {expect};

export async function capture(page, testInfo, name) {
  const file = testInfo.outputPath(name + '.png');
  await page.screenshot({path: file, fullPage: true, animations: 'disabled'});
  await testInfo.attach(name, {path: file, contentType: 'image/png'});
  // Stable names shared by the before/after jobs, independent of test line numbers.
  const visual = path.join(testInfo.project.outputDir, 'visual', testInfo.project.name);
  await fs.mkdir(visual, {recursive: true});
  await fs.copyFile(file, path.join(visual, name + '.png'));
}

export async function accountReady(page) {
  await expect(page.locator('#find-papers')).toBeEnabled();
}
