let session, suggestions = [], publications = [], pdsLoaded = false;
let searchVersion = 0;
let selectionName = "";
const normalizeName = name => String(name || "").normalize("NFC").trim().replace(/\s+/g, " ").toLowerCase();
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

let choosingAuthor = false, authorChoices = [];
function renderAuthorChoices(query) {
  const target = $("#author-choices");
  target.replaceChildren();
  const names = new Map();
  for (const choice of authorChoices) names.set(normalizeName(choice.name), choice);
  for (const paper of suggestions) {
    for (const name of paper.authors || []) {
      if (!names.has(normalizeName(name))) names.set(normalizeName(name), {name, paperId: paper.id});
    }
  }
  if (!names.size) return;
  target.append(node("p", "Choose your name"));
  const tokens = normalizeName(query).match(/[\p{L}\p{N}]+/gu) || [];
  const matches = choice => tokens.length && tokens.every(token => normalizeName(choice.name).includes(token));
  const choices = [...names.values()].sort((a, b) => Number(matches(b)) - Number(matches(a)));
  const hasMatches = choices.some(matches);
  const others = node("details");
  others.append(node("summary", "Other authors"));
  for (const choice of choices) {
    const button = node("button", choice.name);
    button.type = "button";
    button.disabled = choosingAuthor;
    button.addEventListener("click", () => {
      if (choosingAuthor) return;
      choosingAuthor = true;
      $("#remove-profile").disabled = true;
      $("#author").disabled = true;
      $("#find-papers").disabled = true;
      renderAuthorChoices(query);
      run(button, async () => {
        try {
          const data = await api("/api/profile/author", "POST", choice);
          session.researcher = data.researcher;
          selectionName = data.researcher.name;
          $("#author").value = selectionName;
          const link = node("a", selectionName);
          link.href = "/researchers/" + encodeURIComponent(data.researcher.id) + "/";
          $("#profile").replaceChildren(link);
          $("#remove-profile").hidden = false;
          status("Searching…");
          const version = ++searchVersion;
          try {
            const results = await api("/api/papers/search", "POST", {query: selectionName});
            if (version !== searchVersion) return;
            suggestions = results.papers;
            authorChoices = results.authors || [];
            renderSuggestions();
            status(results.warning || "");
          } catch (error) {
            if (version === searchVersion) {
              renderSuggestions();
              status(error.message);
            }
          }
        } finally {
          choosingAuthor = false;
          $("#remove-profile").disabled = false;
          $("#author").disabled = false;
          $("#find-papers").disabled = false;
          renderAuthorChoices($("#author").value);
        }
      });
    });
    if (!hasMatches || matches(choice)) target.append(button);
    else others.append(button);
  }
  if (hasMatches && choices.some(choice => !matches(choice))) target.append(others);
}

function renderSuggestions() {
  const target = $("#suggestions");
  target.replaceChildren();
  const existing = new Set(publications.flatMap(keys));
  const added = paper => keys(paper).some(key => existing.has(key));
  if (!suggestions.length) target.append(node("p", "No suggestions yet. Search by name, title, or DOI."));
  for (const paper of suggestions) {
    const row = node("div");
    row.className = "publication";
    const label = node("label");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.name = "paper";
    checkbox.value = paper.id;
    checkbox.disabled = added(paper) || !pdsLoaded;
    checkbox.checked = !checkbox.disabled && Boolean(normalizeName(selectionName)) &&
      (paper.authors || []).some(author => normalizeName(author) === normalizeName(selectionName));
    label.append(checkbox, document.createTextNode(" " + paper.title));
    const authors = node("p");
    (paper.authors || []).forEach((author, index) => {
      if (index) authors.append(document.createTextNode(", "));
      authors.append(normalizeName(author) === normalizeName(selectionName) ? node("strong", author) : document.createTextNode(author));
    });
    row.append(label, authors);
    const year = new Date(paper.publishedAt).getUTCFullYear();
    if (Number.isFinite(year)) row.append(node("p", "Published " + year));
    row.append(paperLink({...paper, title: "View paper"}));
    const eligibleLabs = session.labs.filter(lab => (lab.professorNames || []).some(name =>
      (paper.authors || []).some(author => normalizeName(author) === normalizeName(name))));
    if (added(paper)) row.append(node("p", "Already added"));
    else if (eligibleLabs.length) {
      const labs = node("fieldset");
      labs.append(node("legend", "Associated labs (optional)"));
      for (const lab of eligibleLabs) {
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
  updateAddButton();
}

function updateAddButton() {
  $("#add").disabled = !session?.researcher || !pdsLoaded || !document.querySelector('input[name="paper"]:checked:not(:disabled)');
}

$("#suggestions").addEventListener("change", updateAddButton);
$("#author").addEventListener("input", () => {
  searchVersion++;
  suggestions = [];
  authorChoices = [];
  $("#author-choices").replaceChildren();
  $("#suggestions").replaceChildren();
  updateAddButton();
  status("");
});
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
  $("#remove-profile").hidden = !session.researcher;
  $("#author").value = session.researcher?.name || "";
  if (session.researcher) {
    const link = node("a", session.researcher.name);
    link.href = "/researchers/" + encodeURIComponent(session.researcher.id) + "/";
    $("#profile").append(link);
    $("#author").value = session.researcher.name;
  }
  ({papers: suggestions} = await api("/api/suggestions"));
  selectionName = session.researcher?.name || "";
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
$("#remove-profile").addEventListener("click", event => {
  if (!session.researcher || choosingAuthor) return;
  if (!confirm(`Remove “${session.researcher.name}” from this account and the researcher directory? Papers already added to Bluesky will remain.`)) return;
  run(event.target, async () => {
    await api("/api/profile", "DELETE");
    searchVersion++;
    selectionName = "";
    authorChoices = [];
    suggestions = [];
    $("#author-choices").replaceChildren();
    await load();
    status("Profile removed. Search to choose a different author name.");
  });
});
$("#logout").addEventListener("click", event => run(event.target, async () => {
  await api("/auth/logout", "POST");
  location.reload();
}));
$("#refresh").addEventListener("click", event => run(event.target, refreshPublications));
$("#search").addEventListener("submit", event => {
  event.preventDefault();
  const version = ++searchVersion;
  const author = $("#author").value.trim();
  selectionName = session.researcher?.name || "";
  suggestions = [];
  authorChoices = [];
  $("#author-choices").replaceChildren();
  $("#suggestions").replaceChildren();
  updateAddButton();
  run(event.submitter, async () => {
    status("Searching…");
    let data;
    try {
      data = await api("/api/papers/search", "POST", {query: author});
    } catch (error) {
      if (version === searchVersion) throw error;
      return;
    }
    if (version !== searchVersion) return;
    selectionName = session.researcher?.name || "";
    suggestions = data.papers;
    authorChoices = data.authors || [];
    renderAuthorChoices(author);
    renderSuggestions();
    if (!suggestions.length) $("#suggestions").replaceChildren(node("p", "No papers found."));
    status(data.warning || "");
  });
});
$("#lab-create").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    await api("/api/labs", "POST", {
      name: $("#lab-name").value.trim(), institution: $("#lab-institution").value.trim(),
      professorName: $("#lab-professor").value.trim(),
    });
    await load();
    $("#lab-create").reset();
    status("Lab added.");
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
load().then(() => { $("#find-papers").disabled = false; }).catch(error => status(error.message));
