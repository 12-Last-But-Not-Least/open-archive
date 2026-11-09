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
      if (!readFileSync(new URL("../web/account.html", import.meta.url), "utf8").includes(`id="${selector.slice(1)}"`)) return null;
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
  let prevented = false;
  ui.get("#search").listeners.submit({preventDefault() { prevented = true; }, submitter: {disabled: false}});
  assert.equal(prevented, true, "search must prevent page navigation");
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
  ui.run('session.researcher = {name: "Soham Phadke"}; api = async () => ({papers: [{id: "exact", title: "Exact", authors: ["SOHAM  PHADKE"]}, {id: "partial", title: "Partial", authors: ["Soham"]}]});');
  ui.get("#author").value = "Soham Phadke";
  search(ui);
  await settle();
  const checked = ui.get("#suggestions").children.map(row => row.children[0].children[0].checked);
  assert.deepEqual(checked, [true, false]);
});

test("DOI lookup previews a paper using the linked name without publishing", async () => {
  const ui = account();
  ui.run('var requests = []; session.researcher = {name: "Soham Phadke"}; api = async (path, method, body) => { requests.push({path, body}); return {kind: "doi", papers: [{id: "doi:10.1234/test", title: "Paper", authors: ["Soham Phadke"]}]}; };');
  ui.get("#author").value = "https://doi.org/10.1234/test";
  ui.get("#search").listeners.submit({preventDefault() {}, submitter: {disabled: false}});
  await settle();
  assert.equal(ui.run("requests.length"), 1);
  assert.equal(ui.run("requests[0].path"), "/api/papers/search");
  assert.equal(ui.get("#suggestions").children[0].children[0].children[0].checked, true);
});

test("DOI preview stays unchecked when no researcher name is linked", async () => {
  const ui = account();
  ui.run('api = async () => ({kind: "doi", papers: [{id: "doi:10.1234/test", title: "Paper", authors: ["Soham Phadke"]}]});');
  ui.get("#author").value = "Soham Phadke";
  ui.get("#author").value = "10.1234/test";
  ui.get("#search").listeners.submit({preventDefault() {}, submitter: {disabled: false}});
  await settle();
  assert.equal(ui.get("#suggestions").children[0].children[0].children[0].checked, false);
});


test("search shows publication year and only labs whose professor authored the paper", async () => {
  const ui = account();
  ui.run('session.labs = [{id: "arc", name: "ARC", professorNames: ["Michael Yip"]}, {id: "unknown", name: "No professor"}]; api = async () => ({papers: [{id: "yes", title: "Match", authors: ["MICHAEL  YIP"], publishedAt: "2022-09-08T00:00:00Z"}, {id: "no", title: "Other", authors: ["Soham Phadke"], labIds: ["arc"]}]});');
  search(ui);
  await settle();
  const [matching, other] = ui.get("#suggestions").children;
  assert.equal(matching.children[2].textContent, "Published 2022");
  assert.equal(matching.children[4].children.length, 2);
  assert.equal(matching.children[4].children[1].children[0].value, "arc");
  assert.equal(other.children.length, 3, "no lab fieldset when professor is absent");
});


test("partial-name search offers the full author name, saves it, selects matching papers and bolds it", async () => {
  const ui = account();
  ui.run('var calls = []; api = async (path, method, body) => { calls.push({path, body}); if (path === "/api/profile/author") return {researcher: {id: "soham", name: "Soham Phadke"}}; return {papers: [{id: "paper", title: "Kidney paper", authors: ["Alex Whelan", "Soham Phadke"]}]}; };');
  ui.get("#author").value = "Soham";
  search(ui);
  await settle();
  assert.equal(ui.get("#suggestions").children[0].children[0].children[0].checked, false);
  const choice = ui.get("#author-choices").children[1];
  assert.equal(choice.textContent, "Soham Phadke");
  choice.listeners.click();
  await settle();
  assert.equal(ui.get("#author").value, "Soham Phadke");
  assert.equal(ui.run('session.researcher.name'), "Soham Phadke");
  assert.equal(ui.get("#suggestions").children[0].children[0].children[0].checked, true);
  const authorNodes = ui.get("#suggestions").children[0].children[1].children;
  assert.equal(authorNodes[2].textContent, "Soham Phadke");
  assert.equal(ui.run('calls[1].path'), "/api/profile/author");
  assert.equal(ui.run('calls[1].body.paperId'), "paper");
});
