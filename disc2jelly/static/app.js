/* Disc2Jelly — vanilla JS UI. Talks to /api/* and listens on /api/events (SSE). */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  drives: [],
  titles: [],
  currentDrive: null,     // device path: "/dev/sr0" or "D:\\"
  currentDiscLabel: "",
  kind: "movie",          // "movie" | "series"
  episodes: [],           // TMDb episode list for the chosen season
  chosen: null,           // {tmdb_id: number|null, title: string, year: number|null}
  jobs: {},               // job_id -> {info, encPct, upPct, fps, eta, status, error}
  logLines: [],
};

/* ---------------------------------------------------------------- helpers */

async function api(path, options) {
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    const msg = (data && data.error) || ("Request failed (" + res.status + ")");
    throw new Error(msg);
  }
  return data;
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  if (h > 0) return h + " h " + m + " min";
  return m + " min";
}

function log(line) {
  if (!line) return;
  state.logLines.push(line);
  if (state.logLines.length > 200) state.logLines.shift();
  $("log-lines").textContent = state.logLines.join("\n");
}

/* ------------------------------------------------------------ status bar */

function setDot(id, value) { // value: true ok / false bad / null unknown
  const dot = $(id);
  dot.classList.remove("ok", "bad", "unknown");
  dot.classList.add(value === true ? "ok" : value === false ? "bad" : "unknown");
}

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    setDot("dot-dvdcss", h.dvdcss_ok === undefined ? null : h.dvdcss_ok);
    setDot("dot-handbrake", h.binaries && h.binaries.handbrake);
    setDot("dot-destination", h.destination_ok === null ? null : h.destination_ok);
    setDot("dot-tmdb", h.tmdb_key_set ? true : null);
    showDvdCssHint(h.dvdcss_ok === false ? (h.dvdcss_hint || "") : "");
  } catch (e) {
    ["dot-dvdcss", "dot-handbrake", "dot-destination", "dot-tmdb"]
      .forEach((id) => setDot(id, null));
    log("Health check failed: " + e.message);
  }
}

/* ------------------------------------------------------------ disc panel */

async function scanDrives() {
  $("disc-status").textContent = "Looking for a disc…";
  $("disc-found").hidden = true;
  try {
    const drives = await api("/api/drives");
    state.drives = drives.filter((d) => d.has_disc);
    if (state.drives.length === 0) {
      $("disc-status").textContent =
        "No disc found. Put a DVD or Blu-ray in the drive, then press “Look for a disc”.";
      return;
    }
    const driveField = $("drive-field");
    const driveSelect = $("drive-select");
    driveSelect.innerHTML = "";
    if (state.drives.length > 1) {
      state.drives.forEach((d) => {
        const opt = document.createElement("option");
        opt.value = d.device;
        opt.textContent = (d.label || "Unnamed disc") + " (" + d.device + ")";
        driveSelect.appendChild(opt);
      });
      driveField.hidden = false;
    } else {
      driveField.hidden = true;
    }
    await loadTitles(state.drives[0].device);
  } catch (e) {
    $("disc-status").textContent = "Could not look for a disc: " + e.message;
  }
}

async function loadTitles(device) {
  const drive = state.drives.find((d) => d.device === device) || state.drives[0];
  state.currentDrive = drive.device;
  state.currentDiscLabel = drive.label || "";
  $("disc-status").textContent = "Reading the disc, this can take a minute…";
  try {
    const titles = await api("/api/titles?device=" + encodeURIComponent(drive.device));
    state.titles = titles;
    if (!titles.length) {
      $("disc-status").textContent = "The disc has no movie titles on it.";
      return;
    }
    $("disc-status").textContent = "";
    $("disc-found").hidden = false;
    $("disc-label").textContent = drive.label || "Unnamed disc";

    // Main title = longest duration (preselected).
    let mainIdx = 0;
    titles.forEach((t, i) => {
      if ((t.duration_s || 0) > (titles[mainIdx].duration_s || 0)) mainIdx = i;
    });

    const sel = $("title-select");
    sel.innerHTML = "";
    titles.forEach((t, i) => {
      const opt = document.createElement("option");
      opt.value = String(t.index);
      opt.textContent = (t.name || ("Title " + t.index)) +
        " — " + fmtDuration(t.duration_s) + ", " + t.chapters + " chapters" +
        (t.duplicate_of ? " (same as title " + t.duplicate_of + ")" : "");
      if (i === mainIdx) opt.selected = true;
      sel.appendChild(opt);
    });

    const extras = $("extras-list");
    extras.innerHTML = "";
    titles.forEach((t, i) => {
      if (i === mainIdx) return;
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = String(t.index);
      label.appendChild(cb);
      label.appendChild(document.createTextNode(
        (t.name || ("Title " + t.index)) + " — " + fmtDuration(t.duration_s)));
      extras.appendChild(label);
    });
    $("extras-details").hidden = titles.length <= 1;

    buildEpisodeRows();
    resetMovieChoice();
    await applyDiscHint(drive.label || "");
  } catch (e) {
    $("disc-status").textContent = "Could not read the disc: " + e.message;
  }
}

/* -------------------------------------------------------- series episodes */

/* One row per disc title: a tick box, the duration, and an episode number.
   Titles the scanner flagged as duplicates start unticked so a "play all"
   title does not silently become an extra episode. */
function buildEpisodeRows() {
  const list = $("episode-list");
  list.innerHTML = "";
  state.titles.forEach((t) => {
    const row = document.createElement("label");
    row.className = "episode-row";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.titleIndex = String(t.index);
    cb.checked = !t.duplicate_of;

    const num = document.createElement("input");
    num.type = "number";
    num.min = "1";
    num.max = "999";
    num.className = "episode-number";
    num.style.width = "4.5rem";

    const name = document.createElement("span");
    name.className = "episode-name";

    row.appendChild(cb);
    row.appendChild(num);
    row.appendChild(document.createTextNode(
      " " + fmtDuration(t.duration_s) +
      (t.duplicate_of ? " (same as title " + t.duplicate_of + ") " : " ")));
    row.appendChild(name);
    list.appendChild(row);
  });
  renumberEpisodes();
}

function renumberEpisodes() {
  const first = parseInt($("first-episode-input").value, 10) || 1;
  let n = first;
  $("episode-list").querySelectorAll(".episode-row").forEach((row) => {
    const cb = row.querySelector("input[type=checkbox]");
    const num = row.querySelector(".episode-number");
    if (cb.checked) {
      num.value = String(n);
      n += 1;
    }
    num.disabled = !cb.checked;
  });
  labelEpisodesFromTmdb();
}

/* Show the TMDb episode title next to each number so a wrong ordering is
   visible before anything is encoded. */
function labelEpisodesFromTmdb() {
  const season = parseInt($("season-input").value, 10);
  $("episode-list").querySelectorAll(".episode-row").forEach((row) => {
    const num = parseInt(row.querySelector(".episode-number").value, 10);
    const match = state.episodes.find(
      (e) => e.season === season && e.episode === num);
    row.querySelector(".episode-name").textContent = match ? "— " + match.name : "";
  });
}

function collectEpisodes() {
  const season = parseInt($("season-input").value, 10) || 1;
  const out = [];
  $("episode-list").querySelectorAll(".episode-row").forEach((row) => {
    const cb = row.querySelector("input[type=checkbox]");
    if (!cb.checked) return;
    const episode = parseInt(row.querySelector(".episode-number").value, 10);
    if (!episode) return;
    const match = state.episodes.find(
      (e) => e.season === season && e.episode === episode);
    out.push({
      title_index: parseInt(cb.dataset.titleIndex, 10),
      season: season,
      episode: episode,
      name: match ? match.name : "",
    });
  });
  return out;
}

async function loadSeasonEpisodes() {
  state.episodes = [];
  if (state.kind !== "series" || !state.chosen || !state.chosen.tmdb_id) {
    labelEpisodesFromTmdb();
    return;
  }
  const season = parseInt($("season-input").value, 10) || 1;
  try {
    state.episodes = await api(
      "/api/tmdb/tv/" + state.chosen.tmdb_id + "/season/" + season);
  } catch (e) {
    log("Could not load episode names: " + e.message);
  }
  labelEpisodesFromTmdb();
}

function setKind(kind) {
  state.kind = kind;
  $("movie-mode").hidden = kind !== "movie";
  $("series-mode").hidden = kind !== "series";
  document.querySelectorAll('input[name="disc-kind"]').forEach((r) => {
    r.checked = r.value === kind;
  });
}

/* Ask the server what the disc label looks like, then preselect the mode,
   season number and search term. */
async function applyDiscHint(label) {
  let hint = { kind: "movie", title: label, season: null, disc: null };
  try {
    hint = await api("/api/disc/hint?label=" + encodeURIComponent(label));
  } catch (e) { /* fall back to the raw label */ }
  setKind(hint.kind);
  if (hint.season !== null && hint.season !== undefined) {
    $("season-input").value = String(hint.season);
  }
  renumberEpisodes();
  await suggestMovie(hint.title || label);
}

/* ------------------------------------------------------------- libdvdcss */

/* There is no install button: VideoLAN publishes libdvdcss as source only,
   so the app can explain what to do but never fetch it. The server sends the
   platform-specific instructions, including the exact target folder. */
function showDvdCssHint(hint) {
  const banner = $("dvdcss-banner");
  if (!banner) return;
  if (!hint) {
    banner.hidden = true;
    return;
  }
  banner.querySelector(".banner-text").textContent = hint;
  banner.hidden = false;
}

/* --------------------------------------------------------- movie picking */

function resetMovieChoice() {
  state.chosen = null;
  $("chosen-movie").hidden = true;
  $("suggestion").hidden = true;
  $("movie-search-field").hidden = true;
  $("search-results").innerHTML = "";
  $("rip-btn").disabled = true;
}

async function suggestMovie(label) {
  if (!label) return;
  try {
    const matches = await api(searchUrl(label));
    if (matches && matches.length > 0) {
      const m = matches[0];
      $("suggestion-title").textContent =
        matchTitle(m) + (m.year ? " (" + m.year + ")" : "");
      $("suggestion").dataset.tmdbId = m.tmdb_id;
      $("suggestion").dataset.title = matchTitle(m);
      $("suggestion").dataset.year = m.year || "";
      $("suggestion").hidden = false;
    } else {
      showMovieSearch(label);
    }
  } catch (e) {
    // No API key configured or lookup failed — manual entry still works.
    showMovieSearch(label);
  }
}

/* TMDb movie results carry .title, TV results carry .name. */
function matchTitle(m) { return m.title || m.name || ""; }

function searchUrl(q) {
  return "/api/tmdb/search?q=" + encodeURIComponent(q) +
    (state.kind === "series" ? "&kind=tv" : "");
}

function showMovieSearch(prefill) {
  $("suggestion").hidden = true;
  $("movie-search-field").hidden = false;
  if (prefill && !$("movie-search").value) $("movie-search").value = prefill;
}

function chooseMovie(choice) {
  state.chosen = choice;
  const el = $("chosen-movie");
  el.textContent = (state.kind === "series" ? "Series: " : "Film: ") + choice.title +
    (choice.year ? " (" + choice.year + ")" : "");
  el.hidden = false;
  $("suggestion").hidden = true;
  $("movie-search-field").hidden = true;
  $("rip-btn").disabled = false;
  $("rip-hint").textContent = "Ready — press the big button.";
  loadSeasonEpisodes();
}

/* Only the WebDAV fields are relevant when WebDAV is the chosen destination. */
function syncDestinationFields() {
  const webdav = $("cfg-destination-kind").value === "webdav";
  $("cfg-local-field").hidden = webdav;
  ["cfg-webdav-url-field", "cfg-webdav-user-field",
   "cfg-webdav-password-field", "cfg-webdav-test-row"]
    .forEach((id) => { $(id).hidden = !webdav; });
}

async function searchMovies() {
  const q = $("movie-search").value.trim();
  if (!q) return;
  const list = $("search-results");
  list.innerHTML = "";
  try {
    const matches = await api(searchUrl(q));
    if (!matches.length) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.textContent = "Nothing found — try different words, or type the name by hand below.";
      b.disabled = true;
      li.appendChild(b);
      list.appendChild(li);
      return;
    }
    matches.slice(0, 8).forEach((m) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.textContent = matchTitle(m) + (m.year ? " (" + m.year + ")" : "");
      b.addEventListener("click", () =>
        chooseMovie({ tmdb_id: m.tmdb_id, title: matchTitle(m), year: m.year || null }));
      li.appendChild(b);
      list.appendChild(li);
    });
  } catch (e) {
    log("Movie search failed: " + e.message);
  }
}

/* --------------------------------------------------------------- ripping */

async function startRip() {
  if (!state.chosen || !state.currentDrive) return;
  const body = {
    drive: state.currentDrive,
    kind: state.kind,
    tmdb_id: state.chosen.tmdb_id,
    title: state.chosen.title,
    year: state.chosen.year,
    profile: $("profile-select").value,
    disc_name: state.currentDiscLabel,
  };
  if (state.kind === "series") {
    body.episodes = collectEpisodes();
    if (!body.episodes.length) {
      alert("Tick at least one episode first.");
      return;
    }
  } else {
    const mainTitle = parseInt($("title-select").value, 10);
    const extras = Array.from(
      $("extras-list").querySelectorAll("input[type=checkbox]:checked"))
      .map((cb) => parseInt(cb.value, 10));
    body.titles = [mainTitle, ...extras.filter((t) => t !== mainTitle)];
  }
  $("rip-btn").disabled = true;
  try {
    await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    log("Added to queue: " + body.title);
    await refreshJobs();
    $("queue-panel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    alert("Could not start: " + e.message);
    $("rip-btn").disabled = false;
  }
}

/* ----------------------------------------------------------- queue panel */

const BADGE_TEXT = {
  DETECT: "Waiting…", IDENTIFY: "Waiting…",
  ENCODE: "Copying from disc…", UPLOAD: "Saving…", CLEANUP: "Tidying up…",
  DONE: "Done", ERROR: "Something went wrong", CANCELLED: "Cancelled",
};

function badgeClass(status) {
  if (status === "DONE") return "badge done";
  if (status === "ERROR") return "badge error";
  if (status === "CANCELLED") return "badge cancelled";
  return "badge running";
}

function jobCard(job) {
  const card = document.createElement("div");
  card.className = "job-card";
  card.id = "job-" + job.id;
  card.innerHTML =
    '<div class="job-head">' +
    '  <span class="job-name"></span>' +
    '  <span class="badge running"></span>' +
    "</div>" +
    stageRow("enc", "Rip &amp; shrink") +
    stageRow("up", "Save") +
    '<p class="job-error" hidden></p>' +
    '<button class="btn btn-danger job-cancel">Cancel</button>';
  card.querySelector(".job-name").textContent =
    job.display_title + (job.year ? " (" + job.year + ")" : "");
  card.querySelector(".job-cancel").addEventListener("click", async () => {
    try {
      await api("/api/jobs/" + job.id + "/cancel", { method: "POST" });
    } catch (e) {
      log("Cancel failed: " + e.message);
    }
  });
  return card;
}

function stageRow(key, label) {
  return (
    '<div class="stage-row" data-stage="' + key + '">' +
    '  <div class="stage-label"><span>' + label + '</span>' +
    '  <span class="stage-info">0%</span></div>' +
    '  <div class="bar"><div class="fill"></div></div>' +
    "</div>"
  );
}

function setBar(card, key, pct, infoText) {
  const row = card.querySelector('[data-stage="' + key + '"]');
  if (!row) return;
  const fill = row.querySelector(".fill");
  const info = row.querySelector(".stage-info");
  if (pct !== null && pct !== undefined) {
    fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
    info.textContent = Math.round(pct) + "%" + (infoText ? " · " + infoText : "");
  } else if (infoText) {
    info.textContent = infoText;
  }
}

function updateJobCard(job) {
  let card = $("job-" + job.id);
  if (!card) {
    card = jobCard(job);
    $("job-list").appendChild(card);
  }
  const badge = card.querySelector(".badge");
  badge.className = badgeClass(job.status);
  badge.textContent = BADGE_TEXT[job.status] || job.status;

  const err = card.querySelector(".job-error");
  if (job.error) {
    err.textContent = job.error;
    err.hidden = false;
  } else {
    err.hidden = true;
  }
  const finished = ["DONE", "ERROR", "CANCELLED"].includes(job.status);
  card.querySelector(".job-cancel").hidden = finished;

  const st = state.jobs[job.id] || {};
  setBar(card, "enc", st.encPct || (job.status === "DONE" ? 100 : 0),
    st.encExtra || "");
  setBar(card, "up", st.upPct || (job.status === "DONE" ? 100 : 0));

  if (job.last_event) applyEventToCard(job.last_event, card);
}

async function refreshJobs() {
  try {
    const jobs = await api("/api/jobs");
    $("queue-empty").hidden = jobs.length > 0;
    const seen = new Set();
    jobs.forEach((job) => {
      seen.add(job.id);
      state.jobs[job.id] = state.jobs[job.id] || {};
      updateJobCard(job);
    });
    // Drop cards for jobs no longer known (should not happen, but stay tidy).
    Array.from($("job-list").children).forEach((card) => {
      if (!seen.has(card.id.replace(/^job-/, ""))) card.remove();
    });
  } catch (e) {
    log("Could not load the queue: " + e.message);
  }
}

/* ------------------------------------------------------------ SSE events */

/* Pure badge logic (DOM-less testable): decide whether an event should
 * advance a card's status badge. Returns {className, text} or null.
 * Advances on a new "running" stage and on terminal events. */
function badgeForEvent(st, ev) {
  const stage = ev.stage;
  if (!stage || !BADGE_TEXT[stage]) return null;
  const terminal = ["DONE", "ERROR", "CANCELLED"].includes(stage);
  if (!terminal &&
      !(ev.status === "running" && st.badgeStage !== stage)) {
    return null;
  }
  st.badgeStage = stage;
  return { className: badgeClass(stage), text: BADGE_TEXT[stage] };
}

function applyEventToCard(ev, card) {
  const jid = ev.job_id;
  if (!jid) return;
  const st = (state.jobs[jid] = state.jobs[jid] || {});
  // refreshJobs() only fires on APP/terminal/unknown-card events, so the
  // badge must be advanced here or it would say "Waiting…" for a whole job.
  const badgeUpdate = badgeForEvent(st, ev);
  if (badgeUpdate) {
    const badge = card.querySelector(".badge");
    if (badge) {
      badge.className = badgeUpdate.className;
      badge.textContent = badgeUpdate.text;
    }
  }
  const pct = ev.percent;
  if (ev.stage === "ENCODE") {
    if (pct !== null && pct !== undefined) st.encPct = pct;
    let extra = "";
    if (ev.fps) extra += Math.round(ev.fps) + " fps";
    if (ev.eta) extra += (extra ? " · " : "") + "left " + ev.eta;
    st.encExtra = extra;
    setBar(card, "enc", st.encPct, extra);
  } else if (ev.stage === "UPLOAD") {
    if (pct !== null && pct !== undefined) st.upPct = pct;
    setBar(card, "up", st.upPct);
  }
}

function handleEvent(ev) {
  if (ev.log) log(ev.log);
  else if (ev.detail) log(ev.stage + ": " + ev.detail);

  if (ev.job_id) {
    const card = $("job-" + ev.job_id);
    if (card) applyEventToCard(ev, card);
    if (["DONE", "ERROR", "CANCELLED"].includes(ev.stage) ||
        ev.status === "error" || ev.status === "cancelled") {
      refreshJobs();
    } else if (!card && ev.stage !== "APP") {
      refreshJobs();
    }
  }
  if (ev.stage === "APP") refreshJobs();
}

let sseBackoff = 1000;
function connectEvents() {
  const src = new EventSource("/api/events");
  src.onopen = () => {
    sseBackoff = 1000;
    refreshJobs();
  };
  src.onmessage = (msg) => {
    try { handleEvent(JSON.parse(msg.data)); } catch (e) { /* ignore */ }
  };
  src.onerror = () => {
    src.close();
    log("Connection to the app was lost — retrying…");
    setTimeout(connectEvents, sseBackoff);
    sseBackoff = Math.min(sseBackoff * 2, 30000);
  };
}

/* ---------------------------------------------------------- settings modal */

const CFG_FIELDS = [
  ["destination_kind", "cfg-destination-kind"],
  ["local_path", "cfg-local-path"],
  ["webdav_url", "cfg-webdav-url"],
  ["webdav_user", "cfg-webdav-user"],
  ["webdav_password", "cfg-webdav-password"],
  ["tmdb_api_key", "cfg-tmdb-key"],
  ["temp_dir", "cfg-temp-dir"],
  ["encoder", "cfg-encoder"],
  ["handbrake_path", "cfg-handbrake-path"],
];

async function openSettings() {
  $("settings-errors").textContent = "";
  $("test-webdav-result").textContent = "";
  try {
    const cfg = await api("/api/config");
    CFG_FIELDS.forEach(([key, id]) => { $(id).value = cfg[key] || ""; });
    syncDestinationFields();
    $("cfg-hevc-quality").value = cfg.hevc_quality;
    $("cfg-h264-quality").value = cfg.h264_quality;
    $("cfg-min-title").value = cfg.min_title_seconds;
  } catch (e) {
    $("settings-errors").textContent = "Could not load settings: " + e.message;
  }
  $("settings-modal").hidden = false;
}

async function saveSettings() {
  const body = {};
  CFG_FIELDS.forEach(([key, id]) => { body[key] = $(id).value.trim(); });
  body.hevc_quality = parseInt($("cfg-hevc-quality").value, 10);
  body.h264_quality = parseInt($("cfg-h264-quality").value, 10);
  body.min_title_seconds = parseInt($("cfg-min-title").value, 10);
  try {
    await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("settings-modal").hidden = true;
    refreshHealth();
    applyDefaultProfile();
  } catch (e) {
    $("settings-errors").textContent = e.message;
  }
}

/* Preselect the disc panel's file-type dropdown from the saved default
 * ("encoder" in /api/config). Per-job choice still wins afterwards. */
async function applyDefaultProfile() {
  try {
    const cfg = await api("/api/config");
    if (cfg && (cfg.encoder === "hevc" || cfg.encoder === "h264")) {
      $("profile-select").value = cfg.encoder;
    }
  } catch (e) { /* keep the markup default */ }
}

async function testWebdav() {
  const out = $("test-webdav-result");
  out.textContent = "Testing…";
  try {
    const r = await api("/api/config/test-webdav", { method: "POST" });
    out.textContent = r.ok ? "Works! " + (r.message || "") : "Failed: " + (r.message || "");
  } catch (e) {
    out.textContent = "Failed: " + e.message;
  }
}

/* ------------------------------------------------------------------ wiring */

function init() {
  $("rescan-btn").addEventListener("click", scanDrives);
  $("drive-select").addEventListener("change", (e) => loadTitles(e.target.value));
  $("suggestion-use").addEventListener("click", () => {
    const s = $("suggestion");
    chooseMovie({
      tmdb_id: s.dataset.tmdbId ? parseInt(s.dataset.tmdbId, 10) : null,
      title: s.dataset.title,
      year: s.dataset.year ? parseInt(s.dataset.year, 10) : null,
    });
  });
  $("suggestion-other").addEventListener("click", () =>
    showMovieSearch(state.currentDiscLabel));
  $("movie-search-btn").addEventListener("click", searchMovies);
  $("movie-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") searchMovies();
  });
  $("manual-apply").addEventListener("click", () => {
    const title = $("manual-title").value.trim();
    if (!title) {
      alert("Please type the movie name first.");
      return;
    }
    const yearVal = $("manual-year").value.trim();
    chooseMovie({
      tmdb_id: null,
      title: title,
      year: yearVal ? parseInt(yearVal, 10) : null,
    });
  });
  document.querySelectorAll('input[name="disc-kind"]').forEach((radio) => {
    radio.addEventListener("change", async (e) => {
      setKind(e.target.value);
      // The TMDb endpoint differs per mode, so re-run the lookup.
      resetMovieChoice();
      await suggestMovie(state.currentDiscLabel);
    });
  });
  $("renumber-btn").addEventListener("click", renumberEpisodes);
  $("first-episode-input").addEventListener("change", renumberEpisodes);
  $("season-input").addEventListener("change", loadSeasonEpisodes);
  $("episode-list").addEventListener("change", (e) => {
    if (e.target.type === "checkbox") renumberEpisodes();
    else labelEpisodesFromTmdb();
  });

  $("rip-btn").addEventListener("click", startRip);
  $("settings-btn").addEventListener("click", openSettings);
  $("cfg-destination-kind").addEventListener("change", syncDestinationFields);
  $("settings-save").addEventListener("click", saveSettings);
  $("settings-close").addEventListener("click", () => {
    $("settings-modal").hidden = true;
  });
  $("test-webdav-btn").addEventListener("click", testWebdav);

  refreshHealth();
  applyDefaultProfile();
  scanDrives();
  refreshJobs();
  connectEvents();
  setInterval(refreshHealth, 60000);
}

document.addEventListener("DOMContentLoaded", init);
