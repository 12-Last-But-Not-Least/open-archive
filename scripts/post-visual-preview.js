import fs from 'node:fs';
import path from 'node:path';

const marker = '<!-- open-archive-visual-preview -->';
const branch = 'pr-preview-images';

export function validateReport(report) {
  if (!Number.isInteger(report.total) || report.total < 1 || report.total > 600 ||
      !Number.isInteger(report.unchanged) || report.unchanged < 0 || !Array.isArray(report.changes) ||
      report.changes.length + report.unchanged !== report.total) throw new Error('Invalid visual report');
  const seen = new Set();
  for (const c of report.changes) {
    if (!/^(desktop|mobile)--[a-z0-9][a-z0-9-]{0,119}$/.test(c.id) ||
        c.id !== `${c.device}--${c.name}` || seen.has(c.id) ||
        !['added', 'removed', 'resized', 'changed'].includes(c.status) ||
        !Number.isInteger(c.pixels) || c.pixels < 0 || c.pixels > 20_000_000 ||
        !Number.isFinite(c.percent) || c.percent < 0 || c.percent > 100) throw new Error('Invalid screenshot entry');
    seen.add(c.id);
  }
  return report;
}

export function commentBody(report, {runURL, sha, imageRoot}) {
  const lines = [marker, '## Visual preview', `Commit \`${sha.slice(0, 7)}\` · [Full visual-diff report](${runURL})`, ''];
  if (!report.changes.length) return [...lines, `No visual changes detected in the ${report.total} covered screenshots.`].join('\n');
  lines.push(`**${report.changes.length} changed screenshots**, ${report.unchanged} unchanged. Pink highlights changed pixels.`,
    'Showing up to 6 images with the largest pixel changes; all comparisons are in the visual-diff-report artifact.', '');
  lines.push('<details><summary>View screenshot comparisons</summary>', '');
  for (const c of report.changes.slice(0, 6)) {
    lines.push(`### ${c.name} — ${c.device} (${c.status}, ${c.percent}%)`, '',
      '| Before | After | Highlighted changes |', '| --- | --- | --- |',
      `| ![Before](${imageRoot}/${c.id}-before.png) | ![After](${imageRoot}/${c.id}-after.png) | ![Changed pixels](${imageRoot}/${c.id}-diff.png) |`, '');
  }
  lines.push('</details>');
  return lines.join('\n');
}

export async function publish({github, context, core, directory = 'visual-diff'}) {
  const {owner, repo} = context.repo;
  const run = context.payload.workflow_run;
  if (run.event !== 'pull_request' || run.conclusion !== 'success') throw new Error('Not a successful PR run');
  // Establish PR identity via GitHub, never a PR-provided artifact or title.
  const candidates = await github.paginate(github.rest.repos.listPullRequestsAssociatedWithCommit,
    {owner, repo, commit_sha: run.head_sha, per_page: 100});
  const prs = candidates.filter(pr => pr.state === 'open' && pr.base.repo.full_name === `${owner}/${repo}` && pr.head.sha === run.head_sha);
  if (prs.length !== 1) { core.notice('No unique current PR for this run; skipping stale or ambiguous preview.'); return; }
  const pr = prs[0];
  const report = validateReport(JSON.parse(fs.readFileSync(path.join(directory, 'report.json'), 'utf8')));
  const prefix = `pr-${pr.number}`;
  let commitSHA;
  if (report.changes.length) {
    const tree = [];
    for (const c of report.changes.slice(0, 6)) {
      for (const side of ['before', 'after', 'diff']) {
        const filename = `${c.id}-${side}.png`;
        const bytes = fs.readFileSync(path.join(directory, filename));
        if (bytes.length > 15_000_000 || bytes.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') throw new Error('Invalid publish image');
        const blob = await github.rest.git.createBlob({owner, repo, encoding: 'base64', content: bytes.toString('base64')});
        tree.push({path: `${prefix}/${filename}`, mode: '100644', type: 'blob', sha: blob.data.sha});
      }
    }
    // Different PRs can finish together. Rebuild on the latest tree after a ref race.
    for (let attempt = 0; attempt < 4; attempt++) {
      let previous;
      try { previous = (await github.rest.git.getRef({owner, repo, ref: `heads/${branch}`})).data.object.sha; }
      catch (error) { if (error.status !== 404) throw error; }
      const baseTree = previous ? (await github.rest.git.getCommit({owner, repo, commit_sha: previous})).data.tree.sha : undefined;
      const createdTree = await github.rest.git.createTree({owner, repo, tree, ...(baseTree ? {base_tree: baseTree} : {})});
      const commit = await github.rest.git.createCommit({owner, repo, message: `Visual preview for PR #${pr.number} (${run.head_sha.slice(0, 7)})`, tree: createdTree.data.sha, parents: previous ? [previous] : []});
      commitSHA = commit.data.sha;
      try {
        if (previous) await github.rest.git.updateRef({owner, repo, ref: `heads/${branch}`, sha: commitSHA, force: false});
        else await github.rest.git.createRef({owner, repo, ref: `refs/heads/${branch}`, sha: commitSHA});
        break;
      } catch (error) {
        if (attempt === 3 || ![409, 422].includes(error.status)) throw error;
      }
    }
  }
  // Check again after uploads so an old run cannot overwrite a newer commit's preview.
  const latest = (await github.rest.pulls.get({owner, repo, pull_number: pr.number})).data;
  if (latest.state !== 'open' || latest.head.sha !== run.head_sha) { core.notice('PR updated during publication; skipping comment.'); return; }
  const runURL = `https://github.com/${owner}/${repo}/actions/runs/${context.runId}`;
  const imageRoot = `https://raw.githubusercontent.com/${owner}/${repo}/${commitSHA}/${prefix}`;
  const body = commentBody(report, {runURL, sha: run.head_sha, imageRoot}) +
    `\n\n[Playwright HTML reports](https://github.com/${owner}/${repo}/actions/runs/${run.id})`;
  const comments = await github.paginate(github.rest.issues.listComments, {owner, repo, issue_number: pr.number, per_page: 100});
  const existing = comments.find(c => c.user?.login === 'github-actions[bot]' && c.body?.startsWith(marker));
  if (existing) await github.rest.issues.updateComment({owner, repo, comment_id: existing.id, body});
  else await github.rest.issues.createComment({owner, repo, issue_number: pr.number, body});
  core.notice(`Updated visual preview on PR #${pr.number}`);
}
