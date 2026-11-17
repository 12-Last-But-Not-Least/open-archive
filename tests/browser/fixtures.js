import {test as base, expect} from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';

export const researcher = {id: 'bryce-hackel', name: 'Bryce Hackel', institution: 'Example University'};
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
      if (url.origin !== 'http://127.0.0.1:8000') {
        unexpected.push(url.origin + p);
        return route.abort();
      }
      const json = data => route.fulfill({json: data});
      if (p === '/data/site.json') {
        if (state.error) return route.fulfill({status: 503, json: {detail: 'Unavailable'}});
        return json({collection: 'io.github.12-last-but-not-least.publication',
          researchers: state.empty ? [] : [researcher], labs: state.empty ? [] : [lab]});
      }
      if (p === '/data/papers.json') return json(state.empty ? [] : [paper]);
      if (p === '/api/session') return json({csrf: 'fixture', user: state.signedIn ? {did: 'did:example:reviewer', handle: 'reviewer.test'} : null,
        researcher: state.profile ? researcher : null});
      if (p === '/api/my-labs') return json({labs: state.role ? [{...lab, role: state.role,
        people: [{name: researcher.name, did: 'did:example:reviewer', role: state.role},
          {name: 'Alex Example', did: 'did:example:alex', role: 'member'}]}] : []});
      if (p === '/api/suggestions') return json({papers: []});
      if (p === '/api/publications') return json({papers: state.profile ? [paper] : []});
      if (p === '/api/papers/search') return json({papers: [paper], authors: [{name: researcher.name, paperId: paper.id}]});
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
