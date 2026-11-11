import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

async function page(dataset, config, network) {
  const elements = new Map();
  const element = () => ({children: [], textContent: "", append(...nodes) {this.children.push(...nodes);},
    replaceChildren(...nodes) {this.children = nodes;}, addEventListener() {}});
  const document = {body: {dataset}, createElement: element, querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  }};
  const context = vm.createContext({document, URL, URLSearchParams, AbortSignal,
    fetch: async url => ({ok: true, json: async () => url.endsWith("site.json") ? config : network ? network(url) : []})});
  const source = readFileSync(new URL("../web/app.js", import.meta.url), "utf8").replace(/^export /gm, "");
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return {get: selector => document.querySelector(selector), document};
}

const config = {collection: "test", researchers: [{id: "new-researcher", name: "New <Name>", institution: "New University"}],
  labs: [{id: "new-lab", name: "New Lab", members: ["new-researcher"]}]};

test("static-hosted directories load new entries and link to live profile routes", async () => {
  const ui = await page({page: "directory", kind: "researchers", root: "../", apiRoot: "https://archive.example"}, config);
  const anchor = ui.get("#directory-list").children[0].children[0].children[0];
  assert.equal(anchor.textContent, "New <Name>");
  assert.equal(anchor.href, "https://archive.example/researchers/new-researcher/");
});

test("profile details and lab members come from runtime configuration", async () => {
  const researcher = await page({page: "researcher", id: "new-researcher", root: "../../"}, config);
  assert.equal(researcher.get(".profile h1").textContent, "New <Name>");
  assert.equal(researcher.get("#affiliations").children[0].children[0].href, "../../labs/new-lab/");
  const lab = await page({page: "lab", id: "new-lab", root: "../../"}, config);
  assert.equal(lab.get("#members-heading").textContent, "Researchers (1)");
  assert.equal(lab.get("#lab-members").children[0].children[0].children[0].textContent, "New <Name>");
});

test("lab page loads member papers automatically and does not read nonmembers", async () => {
  const did = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa";
  const outside = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb";
  const requests = [];
  const config = {collection: "test", researchers: [
    {id: "member", name: "Member", did}, {id: "outside", name: "Outside", did: outside}
  ], labs: [{id: "lab", name: "Lab", members: ["member"], about: "Our work", contactEmail: "lab@example.org"}]};
  const ui = await page({page: "lab", id: "lab", root: "../../"}, config, url => {
    requests.push(url);
    if (url.endsWith("papers.json")) return [];
    if (url === "https://plc.directory/" + did) return {id: did, service: [{type: "AtprotoPersonalDataServer", serviceEndpoint: "https://pds.example"}]};
    if (url.startsWith("https://pds.example/")) return {records: [{uri: "at://" + did + "/test/paper", value: {$type: "test", title: "Member paper", authors: ["Member"], publishedAt: "2024-01-01", url: "https://example.org/paper", labIds: []}}]};
    throw new Error("Unexpected request: " + url);
  });
  assert.equal(ui.get("#lab-about").textContent, "Our work");
  assert.equal(ui.get("#lab-contact").children[0].textContent, "lab@example.org");
  assert.equal(ui.get("#publication-list").children[0].children[0].children[0].textContent, "Member paper");
  assert.equal(requests.some(url => url.includes(outside)), false);
});
