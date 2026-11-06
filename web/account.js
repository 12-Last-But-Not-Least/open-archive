let session, suggestions = [], publications = [], pdsLoaded = false;
const $ = selector => document.querySelector(selector);
const status = message => { $("#status").textContent = message; };
function keys(paper) {
  const result = [];
  if (paper.arxivId) result.push("arxiv:" + paper.arxivId.replace(/v\d+$/, ""));
  if (paper.doi) result.push("doi:" + paper.doi.toLowerCase().replace(/^https:\/\/doi\.org\//, ""));
  return result.length ? result : [paper.url];
}

function node(tag, text) {
  const el = document.createElement(tag);
  if (text) el.textContent = text;
  return el;
}

function paperLink(paper) {
  const el = node("a", paper.title || "Untitled paper");
  try {
    const url = new URL(paper.url);
    if (url.protocol === "https:" && !url.username && !url.password) el.href = url.href;
  } catch { /* Invalid remote links stay plain text. */ }
  return el;
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method, credentials: "same-origin", cache: "no-store",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": session?.csrf || ""},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed. Check the fields and try again.");
  return data;
}

function renderSuggestions() {
  const target = $("#suggestions");
  target.replaceChildren();
  const existing = new Set(publications.flatMap(keys));
  const added = paper => keys(paper).some(key => existing.has(key));
  if (!suggestions.length) target.append(node("p", "No suggestions yet. Search by author name."));
  for (const paper of suggestions) {
    const row = node("div");
    row.className = "publication";
    const label = node("label");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.name = "paper";
    checkbox.value = paper.id;
    checkbox.disabled = added(paper) || !pdsLoaded;
    checkbox.checked = !checkbox.disabled;
    label.append(checkbox, document.createTextNode(" " + paper.title));
    row.append(label, node("p", (paper.authors || []).join(", ")), paperLink({...paper, title: "View paper"}));
    if (added(paper)) row.append(node("p", "Already added"));
    else if (session.labs.length) {
      const labs = node("fieldset");
      labs.append(node("legend", "Associated labs (optional)"));
      for (const lab of session.labs) {
        const option = node("label");
        const input = node("input");
        input.type = "checkbox";
        input.name = "lab";
        input.value = lab.id;
        input.checked = (paper.labIds || []).includes(lab.id);
        option.append(input, document.createTextNode(" " + lab.name));
        labs.append(option);
      }
      row.append(labs);
    }
    target.append(row);
  }
  $("#add").disabled = !pdsLoaded || !suggestions.some(paper => !added(paper));
}

async function refreshPublications() {
  pdsLoaded = false;
  $("#add").disabled = true;
  $("#publications-status").textContent = "Loading…";
  try {
    ({papers: publications} = await api("/api/publications"));
    pdsLoaded = true;
    const target = $("#own-papers");
    target.replaceChildren();
    if (!publications.length) target.append(node("p", "No publications added yet."));
    for (const paper of publications) {
      const row = node("div");
      row.className = "publication";
      const heading = node("h3");
      heading.append(paperLink(paper));
      const remove = node("button", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => run(remove, async () => {
        if (!confirm(`Remove “${paper.title}” from your ATProto publication list?`)) return;
        await api("/api/publications/" + encodeURIComponent(paper.rkey), "DELETE");
        await refreshPublications();
        status("Removed.");
      }));
      row.append(heading, node("p", (paper.authors || []).join(", ")), remove);
      target.append(row);
    }
    $("#publications-status").textContent = "";
  } catch (error) {
    $("#own-papers").replaceChildren();
    $("#publications-status").textContent = error.message;
  }
  renderSuggestions();
}

async function run(button, action) {
  button.disabled = true;
  try { await action(); }
  catch (error) { status(error.message); }
  finally {
    button.disabled = false;
    if (button.id === "add") renderSuggestions();
  }
}

async function load() {
  session = await api("/api/session");
  $("#login").hidden = Boolean(session.user);
  $("#account").hidden = !session.user;
  status("");
  if (new URLSearchParams(location.search).has("error")) {
    status("Sign-in was not completed. Try again.");
    history.replaceState(null, "", "/account/");
  }
  if (!session.user) return;
  $("#identity").textContent = "@" + session.user.handle;
  $("#profile").replaceChildren();
  if (session.researcher) {
    const link = node("a", session.researcher.name);
    link.href = "/researchers/" + encodeURIComponent(session.researcher.id) + "/";
    $("#profile").append(link);
    $("#author").value = session.researcher.name;
  }
  $("#claim-section").hidden = Boolean(session.researcher) || (!session.researchers.length && !session.claim);
  $("#researcher").replaceChildren();
  for (const researcher of session.researchers) {
    const option = node("option", researcher.name);
    option.value = researcher.id;
    $("#researcher").append(option);
  }
  $("#claim-status").textContent = session.claim?.status === "pending" ? "Pending email verification by the site maintainer." : "";
  ({papers: suggestions} = await api("/api/suggestions"));
  await refreshPublications();
}

$("#login").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    status("Opening sign-in…");
    const data = await api("/auth/login", "POST", {handle: $("#handle").value.trim()});
    location.assign(data.url);
  });
});
$("#logout").addEventListener("click", event => run(event.target, async () => {
  await api("/auth/logout", "POST");
  location.reload();
}));
$("#refresh").addEventListener("click", event => run(event.target, refreshPublications));
$("#claim").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    await api("/api/claims", "POST", {researcherId: $("#researcher").value});
    await load();
    status("Profile requested. Contact the site maintainer for email verification.");
  });
});
$("#search").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    status("Searching arXiv…");
    const data = await api("/api/arxiv/search", "POST", {author: $("#author").value.trim()});
    suggestions = [...new Map([...suggestions, ...data.papers].map(paper => [paper.id, paper])).values()];
    renderSuggestions();
    status(data.papers.length + " results. Select the papers that are yours.");
  });
});
$("#import").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    const papers = [...document.querySelectorAll('input[name="paper"]:checked:not(:disabled)')].map(input => ({
      id: input.value,
      labIds: [...input.closest(".publication").querySelectorAll('input[name="lab"]:checked')].map(lab => lab.value),
    }));
    if (!papers.length) { status("Select at least one paper."); return; }
    status("Adding papers…");
    const data = await api("/api/publications", "POST", {papers});
    await refreshPublications();
    const failed = data.results.filter(result => result.status === "failed").length;
    status(failed ? `${failed} paper(s) could not be added. You can retry them.` : "Papers added to your account.");
  });
});
load().catch(error => status(error.message));
