"use strict";
// Every piece of text from a novel goes in through textContent, never as HTML.

const view = document.getElementById("view");
let pollTimer = null;
let routeNumber = 0;

// A slow page must not draw over the page you've since moved to.
function stillCurrent() {
  const number = routeNumber;
  return () => number === routeNumber;
}

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value; // The page's security policy blocks the style attribute.
    else if (key === "value") node.value = value;
    else if (key === "checked" || key === "disabled" || key === "selected") node[key] = Boolean(value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

async function api(path, options) {
  const init = { method: "GET", ...(options || {}) };
  if (init.json !== undefined) {
    init.method = "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(init.json);
    delete init.json;
  }
  let response;
  try {
    response = await fetch("/api/" + path, init);
  } catch {
    throw new Error("The app isn't responding. Is its window still open?");
  }
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || "That didn't work.");
  return value;
}

function toast(message) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 6000);
}

async function act(work) {
  try {
    return await work();
  } catch (error) {
    toast(error.message);
    return undefined;
  }
}

function money(value) {
  if (value === null || value === undefined) return "none set";
  return "$" + Number(value).toFixed(value < 1 ? 3 : 2);
}

function duration(seconds) {
  if (seconds < 3600) return `about ${Math.max(1, Math.round(seconds / 60))} minutes`;
  if (seconds < 172800) return `about ${Math.round(seconds / 3600)} hours`;
  return `about ${Math.round(seconds / 86400)} days`;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function show(...nodes) {
  fill(view, ...nodes);
}

function fail(error) {
  show(h("div", { class: "notice bad" }, error.message));
}

function poll(work, seconds) {
  clearInterval(pollTimer);
  pollTimer = setInterval(work, seconds * 1000);
}

function encodeId(id) {
  return encodeURIComponent(id);
}

function novelTabs(id, current) {
  const link = (name, label, path) => h("a", { href: `#/novel/${encodeId(id)}${path}`, class: name === current ? "current" : "" }, label);
  return h("div", { class: "tabs" },
    link("run", "Run", ""), link("review", "Review", "/review"),
    link("glossary", "Glossary", "/glossary"), link("chapters", "Chapters", "/chapters"));
}

function progressBar(completed, final) {
  return h("div", { class: "bar" }, h("div", { style: `width:${final ? Math.min(100, completed / final * 100) : 0}%` }));
}

// Library -----------------------------------------------------------------

async function library() {
  const current = stillCurrent();
  const draw = async () => {
    const data = await api("novels");
    if (!current()) return;
    const queue = queuePanel(data.queue, data.novels);
    const cards = data.novels.map((novel) => novel.valid
      ? h("a", { class: "panel card", href: `#/novel/${encodeId(novel.id)}` },
          h("h2", {}, novel.title),
          h("span", { class: `badge ${novel.state}` }, novel.state_text), " ",
          novel.flagged ? h("span", { class: "badge flag" }, `${plural(novel.flagged, "item")} to review`) : null,
          progressBar(novel.completed, novel.final),
          h("p", { class: "muted" }, `${novel.completed} of ${novel.final} chapters. ${money(novel.cost_usd)} spent so far.`,
            novel.epub ? " EPUB ready." : ""))
      : h("div", { class: "panel" }, h("h2", {}, novel.title), h("p", { class: "notice bad" }, novel.error)));
    show(
      h("h1", {}, "Library"),
      queue,
      cards.length ? h("div", { class: "cards" }, cards)
        : h("div", { class: "panel" }, h("p", {}, "No novels yet."), h("a", { class: "button", href: "#/add" }, "Add your first novel")),
    );
  };
  await draw();
  if (current()) poll(() => draw().catch(fail), 5);
}

function queuePanel(queue, novels) {
  if (!queue.length) return null;
  const title = (id) => (novels.find((novel) => novel.id === id) || { title: id }).title;
  const order = queue.map((entry) => entry.novel_id);
  const move = (index, step) => act(async () => {
    const next = [...order];
    [next[index], next[index + step]] = [next[index + step], next[index]];
    await api("queue", { json: { order: next } });
    library();
  });
  return h("section", { class: "panel" },
    h("h2", {}, "Queue"),
    h("p", { class: "muted" }, "One novel runs at a time. The queue only moves on while this app is open."),
    h("ol", { class: "list" }, queue.map((entry, index) => h("li", {},
      h("div", { class: "row" }, h("span", {}, `${index + 1}. ${title(entry.novel_id)}`),
        h("button", { disabled: index === 0, onclick: () => move(index, -1) }, "Earlier"),
        h("button", { disabled: index === queue.length - 1, onclick: () => move(index, 1) }, "Later"),
        h("button", { onclick: () => act(async () => { await api("queue/remove", { json: { novel_id: entry.novel_id } }); library(); }) }, "Remove"))))));
}

// Add novel ---------------------------------------------------------------

function addNovel() {
  clearInterval(pollTimer);
  const input = h("input", { type: "file", accept: ".epub,.txt" });
  const out = h("div", {});
  const button = h("button", { class: "primary", onclick: () => act(async () => {
    const file = input.files[0];
    if (!file) throw new Error("Choose a file first.");
    button.disabled = true;
    button.textContent = "Reading the file...";
    try {
      const result = await api(`uploads?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
      drawAnalysis(out, result);
    } finally {
      button.disabled = false;
      button.textContent = "Read this file";
    }
  }) }, "Read this file");
  show(h("h1", {}, "Add novel"),
    h("section", { class: "panel" },
      h("p", {}, "Choose an EPUB or TXT file. Reading it is free and doesn't change anything yet."),
      h("div", { class: "row" }, input, button)),
    out);
}

function drawAnalysis(out, result) {
  const analysis = result.analysis;
  const facts = h("dl", { class: "facts" },
    fact("Language", { zh: "Chinese (Simplified)", "zh-Hant": "Chinese (Traditional)", ko: "Korean" }[analysis.language] || "Unknown"),
    fact("Chapters", analysis.chapter_count),
    fact("Encoding", analysis.encoding || "Unknown"),
    fact("Author", analysis.author_guess || "Not found"));
  const problems = [
    ...analysis.warnings,
    analysis.gaps.length ? `Missing chapter numbers: ${analysis.gaps.slice(0, 20).join(", ")}` : null,
    analysis.duplicates.length ? `Repeated chapter numbers: ${analysis.duplicates.slice(0, 20).join(", ")}` : null,
    analysis.suspected_junk.length ? `${plural(analysis.suspected_junk.length, "part")} may not be story text.` : null,
  ].filter(Boolean);
  const samples = h("details", {}, h("summary", {}, "Sample chapters"),
    analysis.samples.map((sample) => h("div", {}, h("h2", {}, `Chapter ${sample.chapter}: ${sample.title}`), h("p", { class: "prose" }, sample.text))));
  if (analysis.error) {
    fill(out, h("section", { class: "panel" }, h("div", { class: "notice bad" }, `This file can't be used: ${analysis.error}`), facts));
    return;
  }
  const title = h("input", { value: analysis.title_guess || "" });
  const novelId = h("input", { value: result.suggested_id });
  const profile = h("select", {}, result.profiles.map((item) => h("option", { value: item.id }, item.display_name)));
  const addendum = h("textarea", { style: "min-height:8rem" });
  const budget = h("input", { type: "number", min: "0", step: "any", inputmode: "decimal" });
  const trial = h("input", { type: "number", min: "1", step: "1", inputmode: "numeric" });
  const adviceBox = h("div", {});
  const adviceButton = h("button", { onclick: () => act(async () => {
    adviceButton.disabled = true;
    try {
      const data = await api(`uploads/${result.token}/advice`, { json: { confirm_paid: true } });
      if (data.advice.profile_id) profile.value = data.advice.profile_id;
      if (data.advice.addendum) addendum.value = data.advice.addendum;
      fill(adviceBox, h("div", { class: "notice" },
        h("p", {}, `Suggestion (${data.advice.confidence} confidence): ${data.advice.reason || "no reason given"}`),
        h("p", {}, `It used ${data.usage.prompt_tokens + data.usage.completion_tokens} tokens. The form below now shows it. Change anything you like.`)),
        data.advice.new_profile_hint ? saveProfileForm(result.token, data.advice.new_profile_hint, profile) : "");
    } finally {
      adviceButton.disabled = false;
    }
  }) }, "Suggest settings (one paid request)");
  const create = h("button", { class: "primary", onclick: () => act(async () => {
    const created = await api(`uploads/${result.token}/create`, { json: {
      title: title.value, novel_id: novelId.value, profile: profile.value, addendum: addendum.value } });
    const extras = { budget_usd: number(budget), trial: number(trial) };
    if (extras.budget_usd !== null) {
      const started = await api(`novels/${encodeId(created.id)}/start`, { json: extras });
      toast(started.message);
    }
    location.hash = `#/novel/${encodeId(created.id)}`;
  }) }, "Add this novel");
  fill(out, 
    h("section", { class: "panel" }, h("h2", {}, "What's in the file"), facts,
      problems.length ? h("div", { class: "notice", style: "margin-top:1rem" }, problems.map((text) => h("p", {}, text))) : null,
      samples),
    h("section", { class: "panel" }, h("h2", {}, "Settings for this novel"),
      h("div", { class: "row" }, adviceButton), adviceBox,
      h("div", { class: "fields" },
        h("label", {}, "Title", title),
        h("label", {}, "Folder name", novelId),
        h("label", {}, "Translation profile", profile)),
      h("label", {}, "Extra instructions (optional)", addendum),
      h("div", { class: "fields" },
        h("label", {}, "Budget in US dollars (leave empty to start later)", budget),
        h("label", {}, "Trial: stop after this many chapters (optional)", trial)),
      h("div", { class: "row" }, create)));
}

function saveProfileForm(token, hint, select) {
  const id = h("input", { value: hint.id || "" });
  return h("div", { class: "row" },
    h("label", {}, "The suggestion includes a new profile. Save it as", id),
    h("button", { onclick: () => act(async () => {
      const data = await api("profiles", { json: { token, id: id.value, base_profile: select.value } });
      fill(select, ...data.profiles.map((item) => h("option", { value: item.id }, item.display_name)));
      select.value = id.value;
      toast("Profile saved.");
    }) }, "Save profile"));
}

function fact(label, value) {
  return h("div", {}, h("dt", {}, label), h("dd", {}, String(value)));
}

function number(input) {
  return input.value.trim() === "" ? null : Number(input.value);
}

// Novel -------------------------------------------------------------------

async function novelPage(id) {
  const current = stillCurrent();
  const budget = h("input", { type: "number", min: "0", step: "any", inputmode: "decimal" });
  const trial = h("input", { type: "number", min: "1", step: "1", inputmode: "numeric" });
  const header = h("div", {});
  const status = h("div", {});
  const controls = h("div", { class: "row" });
  const logBox = h("div", {});
  const epubBox = h("div", {});
  const run = (path, json) => act(async () => {
    const result = await api(`novels/${encodeId(id)}/${path}`, { json: json || {} });
    if (result.message) toast(result.message);
    await draw();
  });
  const draw = async () => {
    const novel = await api(`novels/${encodeId(id)}`);
    const live = novel.state === "running" || novel.state === "pausing";
    fill(header, h("h1", {}, novel.title), novelTabs(id, "run"));
    const left = novel.final - novel.completed;
    fill(status, 
      h("p", {}, h("span", { class: `badge ${novel.state}` }, novel.state_text)),
      novel.message ? h("div", { class: novel.state === "stopped" ? "notice bad" : "notice" },
        h("p", {}, novel.message), novel.action ? h("p", {}, novel.action) : null,
        (novel.blockers || []).map((text) => h("p", {}, String(text)))) : null,
      progressBar(novel.completed, novel.final),
      h("dl", { class: "facts" },
        fact("Translated", `${novel.completed} of ${novel.final} chapters`),
        fact("Spent so far", money(novel.cost_usd)),
        fact("Budget left", money(novel.budget_left_usd)),
        fact("To review", novel.flagged ? plural(novel.flagged, "item") : "Nothing"),
        novel.seconds_per_chapter && left > 0 ? fact("Time left at this pace", duration(novel.seconds_per_chapter * left)) : null,
        novel.cost_per_chapter && left > 0 ? fact("Cost left at this pace", money(novel.cost_per_chapter * left)) : null,
        fact("Audited through", novel.last_audited ? `Chapter ${novel.last_audited}` : "No audit yet")));
    const startJson = () => ({ budget_usd: number(budget), trial: number(trial) });
    fill(controls, 
      !live && !novel.paused && novel.state !== "queued" && novel.state !== "complete"
        ? h("button", { class: "primary", onclick: () => run("start", startJson()) }, "Start") : null,
      novel.paused ? h("button", { class: "primary", onclick: () => run("resume") }, "Resume") : null,
      live && !novel.paused ? h("button", { onclick: () => run("pause") }, "Pause") : null,
      h("button", { onclick: () => {
        if (number(budget) === null) return toast("Enter a budget first.");
        run("budget", { budget_usd: number(budget) });
      } }, "Set budget"));
    fill(logBox, 
      h("h2", {}, "Latest activity"),
      novel.log.events.length || novel.log.output.length
        ? h("pre", { class: "log" }, [...novel.log.events, ...(novel.log.output.length ? ["", "Run output:", ...novel.log.output] : [])].join("\n"))
        : h("p", { class: "muted" }, "Nothing has run yet."));
    fill(epubBox, 
      h("h2", {}, "EPUB"),
      novel.epubs.length ? h("ul", { class: "list" }, novel.epubs.map((name) =>
        h("li", {}, h("a", { href: `/api/novels/${encodeId(id)}/epub/${encodeURIComponent(name)}`, download: name }, name))))
        : h("p", { class: "muted" }, "No EPUB yet. One is made at the end of each run."),
      h("div", { class: "row" }, h("button", { disabled: live, onclick: () => run("epub/rebuild") }, "Rebuild EPUB")));
  };
  await draw();
  if (!current()) return;
  show(header,
    h("section", { class: "panel" }, status),
    h("section", { class: "panel" }, h("h2", {}, "Run"),
      h("div", { class: "fields" },
        h("label", {}, "Budget from now on, in US dollars. This replaces what's left.", budget),
        h("label", {}, "Trial: stop after this many more chapters (optional)", trial)),
      controls),
    h("section", { class: "panel" }, epubBox),
    h("section", { class: "panel" }, logBox));
  poll(() => draw().catch(fail), 4);
}

// Review ------------------------------------------------------------------

async function reviewPage(id) {
  clearInterval(pollTimer);
  const current = stillCurrent();
  const novel = await api(`novels/${encodeId(id)}`);
  const data = await api(`novels/${encodeId(id)}/review`);
  const items = data.items.map((item) => {
    const open = h("div", {});
    return h("li", {},
      h("div", { class: "row" }, h("strong", {}, `Chapter ${item.chapter}`), h("span", { class: "badge flag" }, item.kind === "commit_qa" ? "Quality check" : item.kind)),
      h("p", {}, item.detail || "No detail was recorded."),
      h("div", { class: "row" },
        h("button", { onclick: () => act(async () => {
          const chapter = await api(`novels/${encodeId(id)}/chapters/${item.chapter}`);
          fill(open, sideBySide(chapter), h("a", { href: `#/novel/${encodeId(id)}/chapters/${item.chapter}` }, "Open this chapter"));
        }) }, "Show source and English"),
        h("button", { onclick: () => act(async () => {
          await api(`novels/${encodeId(id)}/review/resolve`, { json: { chapter: item.chapter, kind: item.kind } });
          toast("Marked as handled.");
          reviewPage(id);
        }) }, "Mark as handled")),
      open);
  });
  if (!current()) return;
  show(h("h1", {}, novel.title), novelTabs(id, "review"),
    h("section", { class: "panel" },
      items.length ? h("ul", { class: "list" }, items) : h("p", {}, "Nothing is waiting for review.")));
}

function sideBySide(chapter) {
  return h("div", { class: "side" },
    h("div", {}, h("h2", {}, "Source"), h("p", { class: "prose", lang: "zh" }, chapter.source)),
    h("div", {}, h("h2", {}, "English"), h("p", { class: "prose" }, chapter.english === null ? "Not translated yet." : chapter.english)));
}

// Glossary ----------------------------------------------------------------

async function glossaryPage(id) {
  clearInterval(pollTimer);
  const current = stillCurrent();
  const novel = await api(`novels/${encodeId(id)}`);
  const search = h("input", { type: "search", placeholder: "Search source or English" });
  const status = h("select", {});
  const category = h("select", {});
  const results = h("div", {});
  let offset = 0;
  let filtersReady = false;
  const load = () => act(async () => {
    const params = new URLSearchParams({ q: search.value, status: status.value, category: category.value, offset });
    const data = await api(`novels/${encodeId(id)}/glossary?${params}`);
    if (!filtersReady) {
      fill(status, h("option", { value: "" }, "All statuses"), ...data.statuses.map((name) => h("option", { value: name }, name)));
      fill(category, h("option", { value: "" }, "All categories"), ...data.categories.map((name) => h("option", { value: name }, name)));
      filtersReady = true;
    }
    const waiting = data.corrections_waiting;
    fill(results, 
      waiting.length ? h("div", { class: "notice" },
        h("p", {}, novel.has_next_audit
          ? `${plural(waiting.length, "correction")} will apply at the next audit:`
          : `${plural(waiting.length, "correction")} waiting. This novel's last audit has already run, so these won't apply on their own:`),
        waiting.map((item) => h("p", {}, `${item.chinese} to ${item.new_english}`))) : null,
      h("p", { class: "muted" }, `${plural(data.total, "term")} found.`),
      h("ul", { class: "list" }, data.entries.map((entry) => glossaryRow(id, entry, novel, load))),
      data.total > data.page_size ? h("div", { class: "row" },
        h("button", { disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - data.page_size); load(); } }, "Previous"),
        h("button", { disabled: offset + data.page_size >= data.total, onclick: () => { offset += data.page_size; load(); } }, "Next")) : null);
  });
  const reset = () => { offset = 0; load(); };
  search.addEventListener("input", reset);
  status.addEventListener("change", reset);
  category.addEventListener("change", reset);
  await load();
  if (!current()) return;
  show(h("h1", {}, novel.title), novelTabs(id, "glossary"),
    h("section", { class: "panel" },
      h("div", { class: "fields" }, h("label", {}, "Search", search), h("label", {}, "Status", status), h("label", {}, "Category", category)),
      results));
}

function glossaryRow(id, entry, novel, reload) {
  const body = h("div", {});
  const edit = () => {
    const english = h("input", { value: entry.english });
    const category = h("input", { value: entry.category });
    const notes = h("input", { value: entry.notes });
    const correct = h("input", { type: "checkbox" });
    const save = (extra) => act(async () => {
      const changes = { chinese: entry.chinese, english: english.value, category: category.value, notes: notes.value, correct_past: correct.checked, ...extra };
      const result = await api(`novels/${encodeId(id)}/glossary`, { json: changes });
      toast(result.message);
      reload();
    });
    fill(body, h("div", { class: "fields" },
      h("label", {}, "English", english), h("label", {}, "Category", category), h("label", {}, "Notes", notes)),
      h("label", { class: "check" }, correct, h("span", {}, novel.has_next_audit
        ? "Also correct past chapters. The change then happens at the next audit, not now."
        : "Also correct past chapters. This novel's last audit has already run, so the request would only wait.")),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => save({}) }, "Save"),
        entry.status === "locked"
          ? h("button", { onclick: () => save({ status: "provisional" }) }, "Save and unlock")
          : h("button", { onclick: () => save({ status: "locked" }) }, "Save and lock"),
        h("button", { onclick: () => fill(body, ) }, "Cancel")));
  };
  return h("li", {},
    h("div", { class: "gloss" },
      h("span", { lang: "zh" }, entry.chinese),
      h("strong", { class: "english" }, String(entry.english)),
      h("span", {}, h("span", { class: "badge" }, entry.status), " ",
        h("button", { onclick: edit }, "Edit"))),
    h("div", { class: "muted" }, [entry.category, entry.first_chapter ? `first seen in chapter ${entry.first_chapter}` : "", entry.notes].filter(Boolean).join(". ")),
    body);
}

// Chapters ----------------------------------------------------------------

async function chaptersPage(id) {
  clearInterval(pollTimer);
  const current = stillCurrent();
  const novel = await api(`novels/${encodeId(id)}`);
  const data = await api(`novels/${encodeId(id)}/chapters`);
  if (!current()) return;
  show(h("h1", {}, novel.title), novelTabs(id, "chapters"),
    h("section", { class: "panel" },
      h("p", { class: "muted" }, data.audited_through
        ? `Chapters up to ${data.audited_through} are recorded by an audit and can't be edited by hand. Later translated chapters can.`
        : "No audit has run yet, so translated chapters can be edited by hand."),
      h("div", { class: "chapter-list" }, data.chapters.map((chapter) =>
        h("a", { href: `#/novel/${encodeId(id)}/chapters/${chapter.number}` },
          `${chapter.number}. ${chapter.title}`,
          chapter.flagged ? " (flagged)" : "", chapter.translated ? "" : " (not yet)")))));
}

async function chapterPage(id, number) {
  clearInterval(pollTimer);
  const current = stillCurrent();
  const novel = await api(`novels/${encodeId(id)}`);
  const chapter = await api(`novels/${encodeId(id)}/chapters/${number}`);
  const body = h("div", {});
  const reading = () => fill(body, sideBySide(chapter));
  reading();
  const steps = h("div", { class: "row" },
    number > 1 ? h("a", { class: "button", href: `#/novel/${encodeId(id)}/chapters/${number - 1}` }, "Previous") : null,
    number < novel.final ? h("a", { class: "button", href: `#/novel/${encodeId(id)}/chapters/${number + 1}` }, "Next") : null,
    chapter.editable ? h("button", { onclick: () => {
      const text = h("textarea", { value: chapter.english });
      fill(body, 
        h("div", { class: "side" },
          h("div", {}, h("h2", {}, "Source"), h("p", { class: "prose", lang: "zh" }, chapter.source)),
          h("div", {}, h("h2", {}, "English"), text)),
        h("div", { class: "row" },
          h("button", { class: "primary", onclick: () => act(async () => {
            const result = await api(`novels/${encodeId(id)}/chapters/${number}`, { json: { text: text.value, expected_sha256: chapter.sha256 } });
            toast(result.message);
            chapterPage(id, number);
          }) }, "Save"),
          h("button", { onclick: reading }, "Cancel")));
    } }, "Edit English") : null);
  if (!current()) return;
  show(h("h1", {}, novel.title), novelTabs(id, "chapters"),
    h("section", { class: "panel" },
      h("h2", {}, `Chapter ${number}: ${chapter.title}`), steps,
      chapter.editable ? h("p", { class: "muted" }, "A saved edit keeps the earlier text in the backup folder. Once an audit records this chapter, it can't be edited again here.") : null,
      !chapter.editable && chapter.english !== null ? h("div", { class: "notice" }, chapter.edit_refusal) : null,
      body));
}

// Settings ----------------------------------------------------------------

async function settingsPage() {
  clearInterval(pollTimer);
  const current = stillCurrent();
  const data = await api("settings");
  const key = h("input", { type: "password", autocomplete: "off" });
  const budget = h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", value: data.default_budget_usd === null ? "" : data.default_budget_usd });
  if (!current()) return;
  show(h("h1", {}, "Settings"),
    h("section", { class: "panel" }, h("h2", {}, "API key"),
      h("p", {}, data.api_key_present ? "A key is stored, encrypted for your Windows account." : "No key is stored yet."),
      h("p", { class: "muted" }, "Once saved, the key can't be shown again here. Saving a new one replaces the old one."),
      h("label", {}, data.api_key_present ? "New key" : "Key", key),
      h("button", { class: "primary", onclick: () => act(async () => {
        await api("settings/api-key", { json: { key: key.value } });
        key.value = "";
        toast("Key saved.");
        settingsPage();
      }) }, "Save key")),
    h("section", { class: "panel" }, h("h2", {}, "Default budget"),
      h("p", { class: "muted" }, "Used when you start a novel that has no budget and you don't enter one."),
      h("label", {}, "US dollars per novel", budget),
      h("button", { onclick: () => act(async () => {
        await api("settings", { json: { default_budget_usd: number(budget) } });
        toast("Saved.");
      }) }, "Save")),
    h("section", { class: "panel" }, h("h2", {}, "Off-peak hours"),
      h("p", {}, "There's no setting for this. The default provider doesn't have peak pricing, so runs aren't held back by the time of day.")),
    h("section", { class: "panel" }, h("h2", {}, "Novels folder"), h("p", {}, data.novels_folder)));
}

// Routing -----------------------------------------------------------------

async function route() {
  routeNumber += 1;
  clearInterval(pollTimer);
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  try {
    if (parts[0] === "add") return addNovel();
    if (parts[0] === "settings") return await settingsPage();
    if (parts[0] === "novel" && parts[1]) {
      const id = parts[1];
      if (parts[2] === "review") return await reviewPage(id);
      if (parts[2] === "glossary") return await glossaryPage(id);
      if (parts[2] === "chapters" && parts[3]) return await chapterPage(id, Number(parts[3]));
      if (parts[2] === "chapters") return await chaptersPage(id);
      return await novelPage(id);
    }
    return await library();
  } catch (error) {
    fail(error);
  }
}

window.addEventListener("hashchange", route);
route();
