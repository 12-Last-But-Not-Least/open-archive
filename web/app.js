// Public reads only. This file never needs a password or access token.

export function safeURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function publicationKey(paper) {
  if (paper.arxivId) return "arxiv:" + paper.arxivId.replace(/v\d+$/, "");
  if (paper.doi) return "doi:" + paper.doi.toLowerCase().replace(/^https:\/\/doi\.org\//, "");
  return safeURL(paper.url);
}

export function normalizeRecord(item, researcher, collection, pds) {
  const record = item.value;
  if (!record || record.$type !== collection || typeof record.title !== "string" || !record.title.trim()) return null;
  if (!safeURL(record.url) || typeof record.publishedAt !== "string" || !Number.isFinite(Date.parse(record.publishedAt))) return null;
  if (!Array.isArray(record.authors) || !record.authors.length || record.authors.some(author => typeof author !== "string")) return null;
  const prefix = `at://${researcher.did}/${collection}/`;
  if (typeof item.uri !== "string" || !item.uri.startsWith(prefix)) return null;
  const rkey = item.uri.slice(prefix.length);
  if (!/^[a-zA-Z0-9._~:-]+$/.test(rkey)) return null;
  return {
    title: record.title, authors: record.authors, publishedAt: record.publishedAt,
    abstract: typeof record.abstract === "string" ? record.abstract : "", url: record.url,
    arxivId: typeof record.arxivId === "string" && /^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?\/\d{7})(?:v\d+)?$/.test(record.arxivId) ? record.arxivId : "",
    doi: typeof record.doi === "string" ? record.doi : "",
    venue: typeof record.venue === "string" ? record.venue : "",
    labIds: Array.isArray(record.labIds) ? record.labIds.filter(id => typeof id === "string") : [],
    researcherIds: [researcher.id], source: "atproto",
    recordURL: pds + "/xrpc/com.atproto.repo.getRecord?" + new URLSearchParams({repo: researcher.did, collection, rkey}),
  };
}

export function mergePublications(imported, live) {
  const papers = new Map();
  for (const paper of [...imported, ...live]) {
    const key = publicationKey(paper);
    if (!key) continue;
    const existing = papers.get(key);
    papers.set(key, {
      ...existing, ...paper,
      researcherIds: [...new Set([...(existing?.researcherIds || []), ...paper.researcherIds])],
      labIds: [...new Set([...(existing?.labIds || []), ...paper.labIds])],
    });
  }
  return [...papers.values()].sort((a, b) => Date.parse(b.publishedAt) - Date.parse(a.publishedAt));
}

export function unclaimedPublications(imported, researchers) {
  const unclaimed = new Set(researchers.filter(researcher => !researcher.did).map(researcher => researcher.id));
  return imported.map(paper => ({...paper, researcherIds: paper.researcherIds.filter(id => unclaimed.has(id))}))
    .filter(paper => paper.researcherIds.length);
}

export function filterPublications(papers, page, id, memberIds) {
  if (page === "lab" && memberIds) return papers.filter(paper => paper.researcherIds.some(id => memberIds.includes(id)));
  if (page === "researcher") return papers.filter(paper => paper.researcherIds.includes(id));
  if (page === "lab") return papers.filter(paper => paper.labIds.includes(id));
  return papers;
}

async function getJSON(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(12000), credentials: "omit", cache: "no-store"});
  if (!response.ok) throw new Error("Request failed (" + response.status + ")");
  return response.json();
}

export async function readPublications(researcher, collection, get = getJSON) {
  // A DID is the stable account id, even if the researcher changes their handle.
  let documentURL;
  if (/^did:plc:[a-z2-7]{24}$/.test(researcher.did)) {
    documentURL = "https://plc.directory/" + researcher.did;
  } else if (/^did:web:[a-zA-Z0-9.-]+$/.test(researcher.did)) {
    documentURL = "https://" + researcher.did.slice(8) + "/.well-known/did.json";
  } else { throw new Error("Unsupported account identifier"); }
  const identity = await get(documentURL);
  if (identity.id !== researcher.did) throw new Error("Account identity mismatch");
  const service = identity.service?.find(item => item.type === "AtprotoPersonalDataServer");
  const endpoint = safeURL(service?.serviceEndpoint);
  if (!endpoint) throw new Error("Account has no HTTPS data server");
  const pds = endpoint.replace(/\/$/, "");
  let cursor;
  const seen = new Set();
  const papers = [];
  do {
    const params = new URLSearchParams({repo: researcher.did, collection, limit: "100"});
    if (cursor) params.set("cursor", cursor);
    const result = await get(pds + "/xrpc/com.atproto.repo.listRecords?" + params);
    if (!Array.isArray(result.records)) throw new Error("Invalid data server response");
    for (const item of result.records) {
      const paper = normalizeRecord(item, researcher, collection, pds);
      if (paper) papers.push(paper);
    }
    cursor = result.cursor;
    if (cursor && seen.has(cursor)) throw new Error("Data server repeated a page");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return papers;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function link(text, url) {
  const node = element("a", text);
  node.href = safeURL(url) || "#";
  return node;
}

function renderPublications(papers, target) {
  target.replaceChildren();
  if (!papers.length) {
    target.append(element("p", "No publications added yet.", "empty"));
    return;
  }
  for (const paper of papers) {
    const article = element("article", null, "publication");
    const heading = element("h3");
    heading.append(link(paper.title, paper.url));
    article.append(heading, element("p", paper.authors.join(", "), "authors"));
    const date = new Date(paper.publishedAt).toLocaleDateString("en-US", {year: "numeric", month: "short", day: "numeric", timeZone: "UTC"});
    const citation = paper.arxivId ? "arXiv:" + paper.arxivId : paper.venue || (paper.doi ? "doi:" + paper.doi : "");
    const meta = element("p", date + (citation ? " · " + citation : ""), "meta");
    article.append(meta);
    const links = element("p", null, "links");
    links.append(link("Paper", paper.url));
    if (paper.projectURL && safeURL(paper.projectURL)) links.append(link("Project", paper.projectURL));
    if (paper.recordURL && safeURL(paper.recordURL)) links.append(link("ATProto record", paper.recordURL));
    article.append(links);
    if (paper.abstract) {
      const details = element("details");
      details.append(element("summary", "Abstract"), element("p", paper.abstract));
      article.append(details);
    }
    target.append(article);
  }
}

function renderProfile(config, page, id, root, apiRoot) {
  if (!["researcher", "lab"].includes(page)) return;
  const entry = config[page === "researcher" ? "researchers" : "labs"].find(item => item.id === id);
  if (!entry) throw new Error("Profile not found");
  document.querySelector(".profile h1").textContent = entry.name;
  document.querySelector(".profile .institution").textContent = entry.institution || "";
  document.title = entry.name + " · Open Archive";
  const profileLink = (item, kind) => {
    const anchor = element("a", item.name);
    anchor.href = (apiRoot ? apiRoot + "/" : root) + kind + "/" + encodeURIComponent(item.id) + "/";
    return anchor;
  };
  if (page === "researcher") {
    const labs = config.labs.filter(lab => (lab.members || []).includes(id));
    document.querySelector("#affiliations").replaceChildren(...labs.map(lab => {
      const p = element("p");
      p.append(profileLink(lab, "labs"));
      return p;
    }));
    document.querySelector("#researcher-account").replaceChildren(entry.did ?
      link("@" + entry.handle, "https://bsky.app/profile/" + encodeURIComponent(entry.did)) :
      element("span", "No ATProto account linked yet."));
  } else {
    document.querySelector("#lab-about").textContent = entry.about || "";
    const contact = document.querySelector("#lab-contact");
    contact.replaceChildren();
    if (entry.contactEmail && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(entry.contactEmail)) {
      const email = element("a", entry.contactEmail);
      email.href = "mailto:" + encodeURIComponent(entry.contactEmail);
      contact.append(email);
    }
    const members = config.researchers.filter(researcher => (entry.members || []).includes(researcher.id));
    document.querySelector("#members-heading").textContent = "Researchers (" + members.length + ")";
    document.querySelector("#lab-members").replaceChildren(...members.map(member => {
      const row = element("div", null, "directory-item");
      const heading = element("h3");
      heading.append(profileLink(member, "researchers"));
      row.append(heading, element("p", member.institution || ""));
      return row;
    }));
  }
}

async function startDirectory() {
  const {root, apiRoot, kind} = document.body.dataset;
  const target = document.querySelector("#directory-list");
  const status = document.querySelector("#directory-status");
  try {
    const config = await getJSON(apiRoot ? apiRoot + "/data/site.json" : root + "data/site.json");
    target.replaceChildren();
    for (const item of config[kind]) {
      const row = element("div", null, "directory-item");
      const heading = element("h3");
      const anchor = element("a", item.name);
      anchor.href = (apiRoot ? apiRoot + "/" : root) + kind + "/" + encodeURIComponent(item.id) + "/";
      heading.append(anchor);
      row.append(heading, element("p", item.institution || ""));
      target.append(row);
    }
    status.textContent = config[kind].length ? "" : "No " + kind + " added yet.";
  } catch {
    status.textContent = "Could not refresh the directory. Reload to try again.";
  }
}

async function start() {
  const {root, page, id, apiRoot} = document.body.dataset;
  const status = document.querySelector("#sync-status");
  const button = document.querySelector("#refresh");
  const target = document.querySelector("#publication-list");
  try {
    const [config, imported] = await Promise.all([getJSON(apiRoot ? apiRoot + "/data/site.json" : root + "data/site.json"), getJSON(root + "data/papers.json")]);
    renderProfile(config, page, id, root, apiRoot);
    const memberIds = page === "lab" ? (config.labs.find(lab => lab.id === id)?.members || []) : undefined;
    const unclaimed = filterPublications(unclaimedPublications(imported, config.researchers), page, id, memberIds);
    const researchers = config.researchers.filter(researcher => researcher.did && (page !== "researcher" || researcher.id === id) && (!memberIds || memberIds.includes(researcher.id)));
    let refreshing = false;
    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      button.disabled = true;
      status.textContent = "Loading…";
      try {
        const results = await Promise.allSettled(researchers.map(researcher => readPublications(researcher, config.collection)));
        const live = results.flatMap(result => result.status === "fulfilled" ? result.value : []);
        const failures = results.filter(result => result.status === "rejected").length;
        const papers = mergePublications(unclaimed, filterPublications(live, page, id, memberIds));
        renderPublications(papers, target);
        if (!researchers.length) status.textContent = "Imported publications. No account linked.";
        else if (failures) status.textContent = "Some accounts unavailable. Showing available publications.";
        else if (!live.length) status.textContent = "No ATProto publications added yet.";
        else status.textContent = "";
      } finally {
        button.disabled = false;
        refreshing = false;
      }
    }
    button.addEventListener("click", refresh);
    await refresh();
  } catch {
    target.replaceChildren();
    status.textContent = "Publications unavailable. Reload the page to try again.";
    button.disabled = true;
  }
}

if (typeof document !== "undefined") {
  if (document.body.dataset.page === "directory") startDirectory();
  else start();
}
