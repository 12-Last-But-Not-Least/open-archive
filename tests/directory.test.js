import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

async function page(dataset, config) {
  const elements = new Map();
  const element = () => ({children: [], textContent: "", append(...nodes) {this.children.push(...nodes);},
    replaceChildren(...nodes) {this.children = nodes;}, addEventListener() {}});
  const document = {body: {dataset}, createElement: element, querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  }};
  const context = vm.createContext({document, URL, URLSearchParams, AbortSignal,
    fetch: async url => ({ok: true, json: async () => url.endsWith("site.json") ? config : []})});
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
