let session, suggestions = [], publications = [], pdsLoaded = false;
let searchVersion = 0;
let removingAll = false;
let selectionName = "";
let searchKind = "";  // "orcid" when results came from the researcher's ORCID iD
let shownInvitationToken = sessionStorage.getItem("shown-lab-invite");
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

// Suggestions never change the input until the user chooses one. The form
// still saves the text value, whether it came from ROR or was typed manually.
function institutionPicker(input, box, id) {
  const list = node("ul");
  const feedback = node("p");
  const hint = node("p", "Suggestions from ROR. You can also type the name yourself.");
  list.id = id + "-options";
  list.className = "institution-options";
  list.hidden = true;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Institution suggestions");
  feedback.className = hint.className = "note";
  feedback.setAttribute("role", "status");
  hint.id = id + "-hint";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", list.id);
  input.setAttribute("aria-describedby", hint.id);
  input.setAttribute("autocomplete", "off");
  box.className = "institution-picker";
  box.append(hint, list, feedback);

  let timer, generation = 0, rows = [], active = -1;
  function close(clearFeedback = true) {
    if (timer !== undefined) clearTimeout(timer);
    generation++;
    rows = [];
    active = -1;
    list.replaceChildren();
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    if (clearFeedback) feedback.textContent = "";
  }

  function select(row) {
    input.value = row.textContent;
    close();
  }

  function search(event) {
    close();
    const query = input.value.trim().replace(/\s+/g, " ");
    if (event.isComposing || query.length > 80 || /@|:\/\//.test(query) ||
        (query.match(/[\p{L}\p{N}]/gu) || []).length < 3) return;
    const requested = generation;
    timer = setTimeout(async () => {
      try {
        const {institutions} = await api("/api/institutions/search", "POST", {query});
        if (requested !== generation) return;
        for (const name of institutions) {
          const row = node("li", name);
          row.id = id + "-option-" + rows.length;
          row.setAttribute("role", "option");
          row.setAttribute("aria-selected", "false");
          // Keep focus in the input until a pointer selection is finished.
          row.addEventListener("pointerdown", event => event.preventDefault());
          row.addEventListener("click", () => select(row));
          rows.push(row);
          list.append(row);
        }
        if (!rows.length) {
          feedback.textContent = "No matches. You can keep the name you typed.";
          return;
        }
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
        feedback.textContent = rows.length + (rows.length === 1 ? " suggestion." : " suggestions.");
      } catch {
        if (requested === generation) feedback.textContent = "Suggestions unavailable. You can keep the name you typed.";
      }
    }, 300);
  }
  input.addEventListener("input", search);
  input.addEventListener("compositionend", search);
  // Preserve the feedback height on blur so clicking Save does not shift the button.
  input.addEventListener("blur", () => close(false));
  input.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Escape") {
      if (!list.hidden) event.preventDefault();
      close();
    } else if (event.key === "Enter" && active >= 0) {
      event.preventDefault();
      select(rows[active]);
    } else if (rows.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      if (active >= 0) rows[active].setAttribute("aria-selected", "false");
      active = active < 0 ? (event.key === "ArrowDown" ? 0 : rows.length - 1) :
        Math.max(0, Math.min(rows.length - 1, active + (event.key === "ArrowDown" ? 1 : -1)));
      rows[active].setAttribute("aria-selected", "true");
      input.setAttribute("aria-activedescendant", rows[active].id);
      rows[active].scrollIntoView({block: "nearest"});
    }
  });
}

function showOrcid() {
  $("#orcid-form").hidden = !session?.researcher;
  $("#orcid").value = session?.researcher?.orcid || "";
  $("#orcid-search").hidden = !session?.researcher?.orcid;
}

let choosingAuthor = false;

function candidate(paper, index) {
  const others = paper.authors.filter((_, other) => other !== index);
  return {name: paper.authors[index], paperId: paper.id, index, title: paper.title, publishedAt: paper.publishedAt,
    affiliation: (paper.authorDetails || [])[index]?.affiliation || "", coauthors: others.slice(0, 4), coauthorCount: others.length};
}

function describeCandidate(choice) {
  const year = new Date(choice.publishedAt).getUTCFullYear();
  const parts = ["“" + (choice.title || "Untitled paper") + "”" + (Number.isFinite(year) ? " (" + year + ")" : "")];
  if (choice.affiliation) parts.push(choice.affiliation);
  if (choice.coauthorCount) {
    const more = choice.coauthorCount - choice.coauthors.length;
    parts.push("with " + choice.coauthors.join(", ") + (more > 0 ? " +" + more + " more" : ""));
  }
  return parts.join(" · ");
}

function renderAuthorChoices(query) {
  const target = $("#author-choices");
  target.replaceChildren();
  // One choice per author slot on a paper, so people who share a name are never merged
  const choices = suggestions.flatMap(paper => (paper.authors || []).map((_, index) => candidate(paper, index)));
  if (!choices.length) return;
  target.append(node("p", "Choose your name. Different people can share a name, so pick the entry on a paper that is yours."));
  const tokens = normalizeName(query).match(/[\p{L}\p{N}]+/gu) || [];
  const matches = choice => tokens.length && tokens.every(token => normalizeName(choice.name).includes(token));
  choices.sort((a, b) => Number(matches(b)) - Number(matches(a)));
  const hasMatches = choices.some(matches);
  const others = node("details");
  others.append(node("summary", "Other authors"));
  for (const choice of choices) {
    const option = node("div");
    option.className = "author-choice";
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
          const data = await api("/api/profile/author", "POST", {paperId: choice.paperId, name: choice.name, index: choice.index});
          session.researcher = data.researcher;
          showOrcid();
          await renderInvitation();
          await loadLabs();
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
            searchKind = results.kind;
            suggestions = results.papers;
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
    option.append(button, node("p", describeCandidate(choice)));
    if (!hasMatches || matches(choice)) target.append(option);
    else others.append(option);
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
    // ORCID results are this researcher's own papers, so they start selected; a matching name alone is not enough.
    checkbox.checked = !checkbox.disabled && searchKind === "orcid";
    label.append(checkbox, document.createTextNode(" " + paper.title));
    const authors = node("p");
    (paper.authors || []).forEach((author, index) => {
      if (index) authors.append(document.createTextNode(", "));
      authors.append(normalizeName(author) === normalizeName(selectionName) ? node("strong", author) : document.createTextNode(author));
    });
    row.append(label, authors);
    const affiliated = (paper.authorDetails || []).filter(detail => detail.affiliation);
    if (affiliated.length) row.append(node("p", "Affiliations: " + affiliated.map(detail => detail.name + " — " + detail.affiliation).join("; ")));
    const year = new Date(paper.publishedAt).getUTCFullYear();
    if (Number.isFinite(year)) row.append(node("p", "Published " + year));
    row.append(paperLink({...paper, title: "View paper"}));
    if (added(paper)) row.append(node("p", "Already added"));
    target.append(row);
  }
  updateAddButton();
}

function updateAddButton() {
  $("#add").disabled = removingAll || !session?.researcher || !pdsLoaded || !document.querySelector('input[name="paper"]:checked:not(:disabled)');
}

$("#suggestions").addEventListener("change", updateAddButton);
$("#author").addEventListener("input", () => {
  searchVersion++;
  searchKind = "";
  suggestions = [];
  $("#author-choices").replaceChildren();
  $("#suggestions").replaceChildren();
  updateAddButton();
  status("");
});
async function refreshPublications() {
  $("#remove-all").disabled = true;
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
        if (removingAll) return;
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
  $("#remove-all").disabled = removingAll || !pdsLoaded || !publications.length;
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
  await renderInvitation();
  if (!session.user) return;
  await loadLabs();
  $("#profile-name").value = session.researcher?.name || "";
  $("#profile-institution").value = session.researcher?.institution || "";
  $("#setup-guidance").textContent = session.researcher
    ? "Next: create a lab or join with an invitation, then add your papers. Your public lab page collects its members’ publications."
    : "Start by opening Create or edit your researcher profile below. Save your name before creating or joining a lab; no publication is required.";
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
  showOrcid();
  searchKind = "";
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
$("#remove-all").addEventListener("click", async () => {
  if (removingAll || !pdsLoaded || !publications.length) return;
  if (!confirm(`Remove all ${publications.length} papers from @${session.user.handle}? This deletes their publication records from your ATProto account. Your researcher profile stays.`)) return;
  removingAll = true;
  $("#remove-all").disabled = true;
  $("#refresh").disabled = true;
  updateAddButton();
  status("Removing papers…");
  try {
    const result = await api("/api/publications", "DELETE");
    await refreshPublications();
    status(result.failed ? `${result.removed} removed. ${result.failed} could not be removed; you can retry.` : "All papers removed.");
  } catch (error) {
    await refreshPublications();
    status(error.message);
  } finally {
    removingAll = false;
    $("#refresh").disabled = false;
    $("#remove-all").disabled = !pdsLoaded || !publications.length;
    updateAddButton();
  }
});
$("#remove-profile").addEventListener("click", event => {
  if (!session.researcher || choosingAuthor) return;
  if (!confirm(`Remove “${session.researcher.name}” from this account and the researcher directory? Papers already added to Bluesky will remain.`)) return;
  run(event.target, async () => {
    await api("/api/profile", "DELETE");
    searchVersion++;
    selectionName = "";
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
    searchKind = data.kind;
    suggestions = data.papers;
    if (searchKind !== "orcid") renderAuthorChoices(author);
    renderSuggestions();
    if (!suggestions.length) $("#suggestions").replaceChildren(node("p", "No papers found."));
    status(data.warning || "");
  });
});
$("#orcid-form").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    const data = await api("/api/profile/orcid", "PUT", {orcid: $("#orcid").value.trim()});
    session.researcher = data.researcher;
    showOrcid();
    status(data.researcher.orcid ? "ORCID iD saved." : "ORCID iD removed.");
  });
});
$("#orcid-search").addEventListener("click", () => {
  if (!session?.researcher?.orcid) return;
  $("#author").value = session.researcher.orcid;
  $("#search").requestSubmit($("#find-papers"));
});
$("#lab-create").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    await api("/api/labs", "POST", {
      name: $("#lab-name").value.trim(), institution: $("#lab-institution").value.trim(),
      about: $("#lab-about").value.trim(), contactEmail: $("#lab-email").value.trim(),
    });
    await loadLabs();
    $("#lab-create").reset();
    status("Lab added. Use Create invitation link under Your labs to invite members, or open the lab name to view its public page.");
  });
});
$("#import").addEventListener("submit", event => {
  event.preventDefault();
  if (removingAll) return;
  run(event.submitter, async () => {
    const papers = [...document.querySelectorAll('input[name="paper"]:checked:not(:disabled)')].map(input => ({
      id: input.value,
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

$("#invitation-dialog").addEventListener("close", () => {
  if (!session.user) {
    $("#handle").focus();
  } else if (!session.researcher) {
    const profileForm = $("#profile-edit");
    const details = profileForm.closest("details");
    (details.open ? $("#profile-name") : details.querySelector("summary")).focus();
  } else {
    $("#lab-invitation .join-lab")?.focus();
  }
});

async function renderInvitation() {
  const queryToken = new URLSearchParams(location.search).get("invite");
  if (queryToken) sessionStorage.setItem("lab-invite", queryToken);
  const token = sessionStorage.getItem("lab-invite");
  const target = $("#lab-invitation");
  target.replaceChildren();
  target.hidden = !token;
  if (!token) return;
  try {
    const {lab} = await api("/api/lab-invites/" + encodeURIComponent(token));
    if (shownInvitationToken !== token) {
      $("#invitation-title").textContent =
        "You've been invited to join " + lab.name;

      $("#invitation-dialog").showModal();
      shownInvitationToken = token;
      sessionStorage.setItem("shown-lab-invite", token);
    }
    target.append(node("p", "Lab invitation"), node("h2", "You've been invited to join " + lab.name));

    const currentStep = !session.user ? 0 : !session.researcher ? 1 : 2;
    const steps = ["Sign in", "Create your profile", "Join lab"];
    const progress = node("ol");
    progress.className = "invitation-steps";
    progress.setAttribute("aria-label", "Steps to join the lab");
    steps.forEach((label, index) => {
      const step = node("li");
      if (index === currentStep) {
        step.setAttribute("aria-current", "step");
        step.append(node("strong", label));
      } else {
        step.textContent = label;
      }
      progress.append(step);
    });
    target.append(progress);
    if (!session.user) target.append(node("p", "Sign in to join this lab."));
    else if (!session.researcher) target.append(node("p", "Create your researcher profile below or choose your author name from a paper, then join the lab."));
    else {
      const join = node("button", "Join lab");
      join.className = "join-lab";
      join.type = "button";
      join.addEventListener("click", () => run(join, async () => {
        await api("/api/labs/" + lab.id + "/join", "POST", {token});
        sessionStorage.removeItem("lab-invite");
        history.replaceState(null, "", "/account/");
        await renderInvitation();
        await loadLabs();
        status("Joined " + lab.name + ".");
      }));
      target.append(join);
    }
  } catch (error) {
    target.append(node("p", error.message));
  }
  const dismiss = node("button", "Dismiss invitation");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => {
    sessionStorage.removeItem("lab-invite");
    history.replaceState(null, "", "/account/");
    target.replaceChildren();
    target.hidden = true;
  });
  target.append(dismiss);
}

let labEditorCount = 0;
function labEditor(lab) {
  const details = node("details");
  details.append(node("summary", "Edit lab details"));
  const form = node("form");
  const fields = {};
  for (const [key, label, type, maxLength, required] of [
    ["name", "Edit lab name", "text", 200, true],
    ["institution", "Edit institution", "text", 200, true],
    ["about", "Edit description", "textarea", 2000, false],
    ["contactEmail", "Edit contact email", "email", 254, false],
  ]) {
    const row = node(key === "institution" ? "div" : "p");
    const caption = node("label", label + " ");
    const input = node(type === "textarea" ? "textarea" : "input");
    if (type !== "textarea") input.type = type;
    input.value = lab[key] || "";
    input.maxLength = maxLength;
    input.required = required;
    if (required) input.minLength = 2;
    caption.append(input);
    row.append(caption);
    if (key === "institution") {
      const box = node("div");
      row.className = "institution-field";
      row.append(box);
      institutionPicker(input, box, "edit-institution-" + (++labEditorCount));
    }
    form.append(row);
    fields[key] = input;
  }
  const save = node("button", "Save lab details");
  save.type = "submit";
  form.append(save);
  form.addEventListener("submit", event => {
    event.preventDefault();
    return run(save, async () => {
      const payload = Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value.trim()]));
      await api("/api/labs/" + encodeURIComponent(lab.id), "PUT", payload);
      await loadLabs();
      status("Lab details saved.");
    });
  });
  details.append(form);
  return details;
}

async function loadLabs() {
  const {labs} = await api("/api/my-labs");
  const target = $("#my-labs");
  target.replaceChildren();
  if (!labs.length) target.append(node("p", "Create a lab or join using an invitation from its admin."));
  for (const lab of labs) {
    const section = node("section");
    const heading = node("h3");
    const link = node("a", lab.name);
    link.href = "/labs/" + encodeURIComponent(lab.id) + "/";
    heading.append(link);
    section.append(heading, node("p", "Your role: " + lab.role));
    const action = (label, callback) => {
      const button = node("button", label);
      button.type = "button";
      button.addEventListener("click", () => run(button, callback));
      return button;
    };
    if (["owner", "admin"].includes(lab.role)) {
      section.append(labEditor(lab));
      const invite = node("div");
      section.append(action("Create invitation link", async () => {
        const result = await api("/api/labs/" + lab.id + "/invite", "POST");
        const input = node("input");
        input.type = "text";
        input.readOnly = true;
        input.value = result.url;
        input.setAttribute("aria-label", "Lab invitation link");
        input.addEventListener("click", () => input.select());
        invite.replaceChildren(input, node("p", "Share this link with lab members. Creating another link replaces the previous one."));
      }), invite);
    }
    for (const person of lab.people) {
      const row = node("p", person.name + (person.detail ? " " + person.detail : "") + " (" + person.role + ") ");
      const change = async actionName => {
        await api("/api/labs/" + lab.id + "/members", "POST", {did: person.did, action: actionName});
        await loadLabs();
      };
      if (person.role !== "owner" && lab.role === "owner") {
        row.append(action(person.role === "admin" ? "Remove admin access" : "Make admin", () => change(person.role === "admin" ? "member" : "admin")));
      }
      const self = person.did === session.user.did;
      if (person.role !== "owner" && (self || lab.role === "owner" || (lab.role === "admin" && person.role === "member"))) {
        row.append(action(self ? "Leave lab" : "Remove member", async () => {
          if (confirm(self ? "Leave " + lab.name + "?" : "Remove " + person.name + (person.detail ? " " + person.detail : "") + " from " + lab.name + "?")) await change("remove");
        }));
      }
      section.append(row);
    }
    target.append(section);
  }
}

$("#profile-edit").addEventListener("submit", event => {
  event.preventDefault();
  run(event.submitter, async () => {
    await api("/api/profile", "PUT", {
      name: $("#profile-name").value.trim(), institution: $("#profile-institution").value.trim(),
    });
    await load();
    status("Profile saved.");
  });
});

for (const id of ["profile-institution", "lab-institution"]) {
  institutionPicker($("#" + id), $("#" + id + "-picker"), id);
}
