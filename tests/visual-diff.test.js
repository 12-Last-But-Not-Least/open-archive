import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {PNG} from 'pngjs';
import {compare} from '../scripts/visual-diff.js';
import {validateReport, commentBody, publish, uploadImage} from '../scripts/post-visual-preview.js';

test('image uploads retry transient GitHub failures with the same content', async () => {
  const args = {content: 'test-image', encoding: 'base64'};
  const waits = [];
  let calls = 0;
  const result = await uploadImage(async received => {
    assert.equal(received, args);
    if (++calls < 3) throw Object.assign(new Error('GitHub unavailable'), {status: 500});
    return {data: {sha: 'uploaded'}};
  }, args, async ms => waits.push(ms));
  assert.equal(result.data.sha, 'uploaded');
  assert.deepEqual(waits, [1000, 2000]);
});

test('image uploads stop after bounded retries and omit request data from errors', async () => {
  for (const status of [503, 403]) {
    let calls = 0;
    await assert.rejects(uploadImage(async () => {
      calls++;
      throw Object.assign(new Error('large image payload'), {status});
    }, {}, async () => {}), error => {
      assert.match(error.message, new RegExp(`HTTP ${status}`));
      assert(!error.message.includes('large image payload'));
      return true;
    });
    assert.equal(calls, status === 503 ? 3 : 1);
  }
});

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'archive-visual-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const before = path.join(root, 'before'), after = path.join(root, 'after'), output = path.join(root, 'output');
  const write = (dir, name, {width = 8, height = 8, black = false} = {}) => {
    const image = new PNG({width, height}); image.data.fill(255);
    if (black) for (let i = 0; i < image.data.length; i += 4) image.data[i] = image.data[i + 1] = image.data[i + 2] = 0;
    fs.mkdirSync(path.join(dir, 'desktop'), {recursive: true});
    fs.writeFileSync(path.join(dir, 'desktop', `${name}.png`), PNG.sync.write(image));
  };
  return {root, before, after, output, write};
}

test('unchanged images are excluded, changed pixels are highlighted', t => {
  const f = fixture(t);
  for (const dir of [f.before, f.after]) f.write(dir, 'same');
  f.write(f.before, 'changed'); f.write(f.after, 'changed', {black: true});
  const r = compare(f.before, f.after, f.output);
  assert.equal(r.unchanged, 1); assert.equal(r.changes.length, 1);
  assert.equal(r.changes[0].pixels, 64);
  const diff = PNG.sync.read(fs.readFileSync(path.join(f.output, 'desktop--changed-diff.png')));
  assert.deepEqual([...diff.data.subarray(0, 4)], [255, 0, 80, 255]);
  assert(!fs.existsSync(path.join(f.output, 'desktop--same-diff.png')));
});

test('resizes and added/removed states are reported, even for white canvases', t => {
  const f = fixture(t);
  f.write(f.before, 'resize'); f.write(f.after, 'resize', {height: 12});
  f.write(f.before, 'removed'); f.write(f.after, 'added');
  const r = compare(f.before, f.after, f.output);
  assert.deepEqual(r.changes.map(c => c.status).sort(), ['added', 'removed', 'resized']);
  assert.equal(PNG.sync.read(fs.readFileSync(path.join(f.output, 'desktop--resize-diff.png'))).height, 12);
});

test('missing captures fail rather than claiming no changes', t => {
  const f = fixture(t); f.write(f.after, 'home');
  assert.throws(() => compare(f.before, f.after, f.output), /Missing/);
});

test('reject malformed PNGs and oversized dimensions before decoding', t => {
  const f = fixture(t); f.write(f.before, 'home'); f.write(f.after, 'home');
  const file = path.join(f.after, 'desktop/home.png');
  const bytes = fs.readFileSync(file); bytes.writeUInt32BE(0x7fffffff, 16); fs.writeFileSync(file, bytes);
  assert.throws(() => compare(f.before, f.after, f.output), /dimensions/);
});

test('publisher rejects unsafe IDs and renders a bounded comment', () => {
  const changes = Array.from({length: 8}, (_, i) => ({id: `desktop--state-${i}`, device: 'desktop', name: `state-${i}`, status: 'changed', pixels: 10, percent: 1}));
  const report = {total: 9, unchanged: 1, changes};
  validateReport(report);
  const body = commentBody(report, {runURL: 'https://example.test/run', sha: '123456789', imageRoot: 'https://example.test/images'});
  assert.equal((body.match(/<details>/g) || []).length, 1);
  assert.equal((body.match(/<\/details>/g) || []).length, 1);
  assert(!body.includes('<details open>'));
  assert.equal((body.match(/^### /gm) || []).length, 6);
  assert(!body.includes('state-6'));
  assert.match(commentBody({total: 9, unchanged: 9, changes: []}, {sha: '123456789', runURL: 'run'}), /No visual changes/);
  assert.throws(() => validateReport({...report, changes: [{...changes[0], id: '../../secret'}, ...changes.slice(1)]}), /Invalid screenshot/);
});

test('stale workflow runs cannot publish images or comments', async () => {
  let called = false;
  const github = {rest: {repos: {listPullRequestsAssociatedWithCommit() {}}}, paginate: async () => [{state: 'open', base: {repo: {full_name: 'owner/repo'}}, head: {sha: 'newer'}}]};
  await publish({github, context: {repo: {owner: 'owner', repo: 'repo'}, payload: {workflow_run: {event: 'pull_request', conclusion: 'success', head_sha: 'old'}}}, core: {notice() {called = true;}}});
  assert(called);
});

test('publisher uploads only changed PNGs and edits its existing bot comment', async t => {
  const f = fixture(t);
  f.write(f.before, 'home'); f.write(f.after, 'home', {black: true});
  compare(f.before, f.after, f.output);
  const pr = {number: 16, state: 'open', base: {repo: {full_name: 'owner/repo'}}, head: {sha: 'abcdef123456'}};
  const calls = [];
  let refAttempts = 0;
  const record = (name, result) => async args => {calls.push({name, args}); return {data: result};};
  const github = {
    rest: {
      repos: {listPullRequestsAssociatedWithCommit: 'prs'},
      pulls: {get: record('getPR', pr)},
      git: {
        createBlob: record('blob', {sha: 'blob-sha'}),
        getRef: async () => {if (++refAttempts === 1) throw {status: 404}; return {data: {object: {sha: 'competing-commit'}}};},
        getCommit: record('getCommit', {tree: {sha: 'competing-tree'}}),
        createTree: record('tree', {sha: 'tree-sha'}),
        createCommit: record('commit', {sha: 'new-commit'}),
        createRef: async () => {throw {status: 422};},
        updateRef: record('ref', {}),
      },
      issues: {listComments: 'comments', updateComment: record('update', {}), createComment: record('create', {})},
    },
    paginate: async method => method === 'prs' ? [pr] : [
      {id: 1, user: {login: 'someone'}, body: '<!-- open-archive-visual-preview -->'},
      {id: 2, user: {login: 'github-actions[bot]'}, body: '<!-- open-archive-visual-preview --> old'},
    ],
  };
  await publish({github, context: {repo: {owner: 'owner', repo: 'repo'}, runId: 22,
    payload: {workflow_run: {id: 11, event: 'pull_request', conclusion: 'success', head_sha: pr.head.sha}}}, core: {notice() {}}, directory: f.output});
  assert.equal(calls.filter(c => c.name === 'blob').length, 3);
  assert.equal(calls.find(c => c.name === 'update').args.comment_id, 2);
  assert.equal(calls.filter(c => c.name === 'create').length, 0);
  assert.equal(calls.find(c => c.name === 'ref').args.force, false);
  assert.equal(calls.filter(c => c.name === 'tree')[1].args.base_tree, 'competing-tree');
  assert.match(calls.find(c => c.name === 'update').args.body, /new-commit\/pr-16\/desktop--home-diff.png/);
});

test('unchanged report creates a no-change comment without uploading images', async t => {
  const f = fixture(t); f.write(f.before, 'home'); f.write(f.after, 'home');
  compare(f.before, f.after, f.output);
  const pr = {number: 16, state: 'open', base: {repo: {full_name: 'owner/repo'}}, head: {sha: 'abc123'}};
  let body;
  const github = {
    rest: {repos: {listPullRequestsAssociatedWithCommit: 'prs'}, pulls: {get: async () => ({data: pr})},
      issues: {listComments: 'comments', createComment: async args => {body = args.body;}}},
    paginate: async method => method === 'prs' ? [pr] : [],
  };
  await publish({github, context: {repo: {owner: 'owner', repo: 'repo'}, runId: 22,
    payload: {workflow_run: {id: 11, event: 'pull_request', conclusion: 'success', head_sha: pr.head.sha}}}, core: {notice() {}}, directory: f.output});
  assert.match(body, /No visual changes detected in the 1 covered screenshots/);
  assert(!body.includes('undefined'));
});
