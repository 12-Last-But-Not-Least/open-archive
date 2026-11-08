import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

function account() {
  const elements = new Map();
  function element() {
    return {value: "", children: [], listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; },
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); }};
  }
  const document = {
    querySelector(selector) {
      if (!selector.startsWith("#")) return null;
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    },
    createElement: element,
    createTextNode: text => text,
  };
  const context = vm.createContext({document, URL, fetch: () => new Promise(() => {})});
  vm.runInContext(readFileSync(new URL("../web/account.js", import.meta.url), "utf8"), context);
  vm.runInContext('session = {labs: []}; pdsLoaded = true;', context);
  return {context, get: selector => document.querySelector(selector),
    run: code => vm.runInContext(code, context)};
}

const settle = () => new Promise(resolve => setImmediate(resolve));
function search(ui) {
  ui.get("#search").listeners.submit({preventDefault() {}, submitter: {disabled: false}});
}

test("a new author search replaces old results and does not select papers", async () => {
  const ui = account();
  ui.run('suggestions = [{id: "old", title: "Old paper"}]; api = async () => ({papers: [{id: "new", title: "New paper"}]});');
  ui.get("#author").value = "Soham";
  search(ui);
  await settle();
  assert.equal(ui.run('suggestions.map(p => p.id).join(",")'), "new");
  const row = ui.get("#suggestions").children[0];
  assert.equal(row.children[0].children[0].checked, false);
  assert.equal(ui.get("#add").disabled, true);
  assert.equal(ui.get("#status").textContent, "");
});

test("editing the author clears results and ignores an older response", async () => {
  const ui = account();
  ui.run('var resolveSearch; api = () => new Promise(resolve => { resolveSearch = resolve; });');
  search(ui);
  ui.get("#author").value = "Soham";
  ui.get("#author").listeners.input();
  ui.run('resolveSearch({papers: [{id: "old", title: "Old paper"}]});');
  await settle();
  assert.equal(ui.run("suggestions.length"), 0);
  assert.equal(ui.get("#suggestions").children.length, 0);
  assert.equal(ui.get("#status").textContent, "");
});

test("an empty search removes old papers", async () => {
  const ui = account();
  ui.run('suggestions = [{id: "old"}]; api = async () => ({papers: []});');
  search(ui);
  await settle();
  assert.equal(ui.run("suggestions.length"), 0);
  assert.equal(ui.get("#suggestions").children[0].textContent, "No papers found.");
});

test("only an exact full author name is preselected", async () => {
  const ui = account();
  ui.run('api = async () => ({papers: [{id: "exact", title: "Exact", authors: ["SOHAM  PHADKE"]}, {id: "partial", title: "Partial", authors: ["Soham"]}]});');
  ui.get("#author").value = "Soham Phadke";
  search(ui);
  await settle();
  const checked = ui.get("#suggestions").children.map(row => row.children[0].children[0].checked);
  assert.deepEqual(checked, [true, false]);
});

test("DOI lookup previews a paper using the linked name without publishing", async () => {
  const ui = account();
  ui.run('var requests = []; session.researcher = {name: "Soham Phadke"}; api = async (path, method, body) => { requests.push({path, body}); return {papers: [{id: "doi:10.1234/test", title: "Paper", authors: ["Soham Phadke"]}]}; };');
  ui.get("#doi").value = "https://doi.org/10.1234/test";
  ui.get("#doi-search").listeners.submit({preventDefault() {}, submitter: {disabled: false}});
  await settle();
  assert.equal(ui.run("requests.length"), 1);
  assert.equal(ui.run("requests[0].path"), "/api/doi/lookup");
  assert.equal(ui.get("#suggestions").children[0].children[0].children[0].checked, true);
});

test("DOI preview stays unchecked when no researcher name is linked", async () => {
  const ui = account();
  ui.run('api = async () => ({papers: [{id: "doi:10.1234/test", title: "Paper", authors: ["Soham Phadke"]}]});');
  ui.get("#author").value = "Soham Phadke";
  ui.get("#doi").value = "10.1234/test";
  ui.get("#doi-search").listeners.submit({preventDefault() {}, submitter: {disabled: false}});
  await settle();
  assert.equal(ui.get("#suggestions").children[0].children[0].children[0].checked, false);
});
