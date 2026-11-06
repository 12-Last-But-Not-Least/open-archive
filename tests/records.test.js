import test from "node:test";
import assert from "node:assert/strict";
import {safeURL, normalizeRecord, mergePublications, filterPublications, readPublications} from "../web/app.js";

const researcher = {id: "bryce-hackel", did: "did:plc:rqu4asr46qcalfx5b2mxqzvx"};
const collection = "io.github.12-last-but-not-least.publication";
const pds = "https://example.test";
const record = (overrides = {}) => ({
  uri: `at://${researcher.did}/${collection}/paper-one`,
  value: {$type: collection, title: "New paper", authors: ["Bryce Hackel"], url: "https://arxiv.org/abs/2606.14083", arxivId: "2606.14083", publishedAt: "2026-06-12T00:00:00Z", labIds: ["ucsd-arc"], ...overrides},
});

test("a new PDS record renders without being in the imported catalog", () => {
  const paper = normalizeRecord(record(), researcher, collection, pds);
  assert.equal(mergePublications([], [paper])[0].title, "New paper");
  assert.equal(paper.source, "atproto");
});

test("coauthors and versioned arXiv records merge into one publication", () => {
  const paper = normalizeRecord(record(), researcher, collection, pds);
  const imported = {...paper, source: "arxiv", arxivId: "2606.14083v1", researcherIds: ["coauthor"]};
  const merged = mergePublications([imported], [paper]);
  assert.equal(merged.length, 1);
  assert.deepEqual(merged[0].researcherIds, ["coauthor", "bryce-hackel"]);
  assert.equal(merged[0].source, "atproto");
});

test("Skill Tests stays on the researcher profile and off the ARC lab page", () => {
  const paper = normalizeRecord(record({title: "Skill Tests", arxivId: undefined, doi: "10.1145/3770762.3772518", labIds: []}), researcher, collection, pds);
  assert.equal(filterPublications([paper], "lab", "ucsd-arc").length, 0);
  assert.equal(filterPublications([paper], "researcher", "bryce-hackel").length, 1);
});

test("unsafe links and malformed records are ignored", () => {
  assert.equal(safeURL("javascript:alert(1)"), null);
  assert.equal(safeURL("https://user:secret@example.com"), null);
  assert.equal(normalizeRecord(record({url: "javascript:alert(1)"}), researcher, collection, pds), null);
  assert.equal(normalizeRecord(record({publishedAt: "invalid"}), researcher, collection, pds), null);
  assert.equal(normalizeRecord(record({authors: [null]}), researcher, collection, pds), null);
  assert.equal(normalizeRecord({...record(), uri: "at://someone-else/collection/key"}, researcher, collection, pds), null);
});

test("the reader follows pagination and resolves the current PDS from a DID", async () => {
  const calls = [];
  const get = async url => {
    calls.push(url);
    if (url.startsWith("https://plc.directory/")) return {id: researcher.did, service: [{type: "AtprotoPersonalDataServer", serviceEndpoint: pds}]};
    if (url.includes("cursor=next")) return {records: [{...record({arxivId: "2606.99999"}), uri: `at://${researcher.did}/${collection}/paper-two`}]};
    return {records: [record()], cursor: "next"};
  };
  assert.equal((await readPublications(researcher, collection, get)).length, 2);
  assert.ok(calls[2].includes("cursor=next"));
});

test("a PDS outage rejects cleanly so the UI can retain imported publications", async () => {
  await assert.rejects(readPublications(researcher, collection, async () => {throw new Error("offline");}), /offline/);
});

test("the reader rejects a mismatched DID document", async () => {
  await assert.rejects(readPublications(researcher, collection, async () => ({id: "did:plc:someoneelse"})), /identity mismatch/);
});
