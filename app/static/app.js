const state = {
  view: "library",
  status: null,
  platforms: [],
  devices: [],
  search: "",
  platform: "",
  duplicate: "all",
  offset: 0,
  limit: 200,
  // id -> {display_name, platform}. A map rather than a set so the bulk bar and the
  // confirmation dialog can name selections that current filters have scrolled away.
  selectedRows: new Map(),
  // Duplicate group key -> game id chosen as the copy to keep. A keeper must be
  // explicitly chosen before any group cleanup action is enabled.
  duplicateKeepers: new Map(),
  editingId: null,
  assigningId: null,
  assignmentDevices: [],
  deviceId: null,
  deviceScope: "on_device",
  refreshTimer: null,
  gamesController: null,
  // Job ids already surfaced to the user, so the poller and an awaited job do not
  // both report the same outcome.
  reportedJobs: new Set(),
  namingConfidence: "all",
  namingSelected: new Map(),
  jobReportId: null,
  jobIssueOffset: 0,
  saveTab: "current",
  saveSearch: "",
  saveOffset: 0,
  saveSnapshotId: null,
  saveSnapshotOffset: 0,
  saveSnapshotSearch: "",
};

const view = document.querySelector("#view");
const pageTitle = document.querySelector("#page-title");
const pageSubtitle = document.querySelector("#page-subtitle");
const scanButton = document.querySelector("#scan-button");
const stopJobButton = document.querySelector("#stop-job-button");
const refreshButton = document.querySelector("#refresh-button");
const dialog = document.querySelector("#confirm-dialog");
const dialogTitle = document.querySelector("#dialog-title");
const dialogContent = document.querySelector("#dialog-content");
const dialogConfirm = document.querySelector("#dialog-confirm");
const dialogCancel = document.querySelector("#dialog-cancel");

// Views re-render by replacing their whole subtree, which destroys the element the
// user is typing into. Capturing the focused control and its caret keeps search and
// inline rename usable across the debounced re-render.
function captureFocus() {
  const active = document.activeElement;
  if (!active || !active.id || !view.contains(active)) return null;
  const snapshot = { id: active.id };
  if (typeof active.selectionStart === "number") {
    snapshot.start = active.selectionStart;
    snapshot.end = active.selectionEnd;
  }
  return snapshot;
}

function restoreFocus(snapshot) {
  if (!snapshot) return;
  const element = document.getElementById(snapshot.id);
  if (!element) return;
  element.focus({ preventScroll: true });
  if (typeof snapshot.start === "number" && typeof element.setSelectionRange === "function") {
    try {
      element.setSelectionRange(snapshot.start, snapshot.end);
    } catch {
      /* selectionRange is unsupported on some input types; focus alone is enough. */
    }
  }
}

function setViewHtml(html) {
  const snapshot = captureFocus();
  view.innerHTML = html;
  restoreFocus(snapshot);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.textContent = message;
  document.querySelector("#toasts").append(node);
  setTimeout(() => node.remove(), 4500);
}

function storedAccessToken() {
  const legacyToken = localStorage.getItem("rom-manager-token");
  const token = localStorage.getItem("rommates-token") || legacyToken;
  if (legacyToken && !localStorage.getItem("rommates-token")) {
    localStorage.setItem("rommates-token", legacyToken);
    localStorage.removeItem("rom-manager-token");
  }
  return token;
}

async function api(path, options = {}) {
  const token = storedAccessToken();
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(body?.detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function downloadApiFile(path, filename) {
  const token = storedAccessToken();
  const response = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    let message = `Download failed (${response.status})`;
    try { message = (await response.json()).detail || message; } catch { /* use status */ }
    throw new Error(message);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const JOB_LABELS = {
  scan: "Scanning changed files…",
  rename: "Renaming bundle…",
  bulk_rename: "Applying naming suggestions…",
  delete: "Moving to trash…",
  device_apply: "Applying device changes…",
  restore: "Restoring from trash…",
  purge: "Deleting permanently…",
  save_snapshot: "Snapshotting saves…",
  save_restore: "Restoring saves…",
};

const JOB_POLL_INTERVAL = 700;
const JOB_POLL_TIMEOUT = 30 * 60 * 1000;

async function waitForJob(jobId) {
  const deadline = Date.now() + JOB_POLL_TIMEOUT;
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "complete") return job.result || {};
    if (job.status === "failed") throw new Error(job.detail || "The background job failed");
    if (job.status === "cancelled") {
      const error = new Error(job.detail || "Job stopped by user");
      error.cancelled = true;
      throw error;
    }
    if (Date.now() > deadline) {
      throw new Error(
        "Stopped waiting for this job after 30 minutes. It may still be running — check the Jobs view.",
      );
    }
    await new Promise((resolve) => setTimeout(resolve, JOB_POLL_INTERVAL));
  }
}

async function requestJob(path, options, queuedMessage) {
  const response = await api(path, options);
  toast(queuedMessage);
  await refreshStatus();
  return waitForJob(response.job_id);
}

async function refreshStatus() {
  state.status = await api("/api/status");
  document.querySelector("#nav-games").textContent = state.status.games.toLocaleString();
  document.querySelector("#nav-duplicates").textContent = state.status.duplicates.toLocaleString();
  document.querySelector("#nav-devices").textContent = state.status.devices.toLocaleString();
  document.querySelector("#nav-trash").textContent = state.status.trash.toLocaleString();
  document.querySelector("#nav-saves").textContent = state.status.save_snapshots.toLocaleString();
  document.querySelector("#library-root").textContent = state.status.roots.library;
  const pill = document.querySelector("#job-pill");
  const job = state.status.job;
  const running = job && ["queued", "running", "cancelling"].includes(job.status);
  const scanning = running && job.kind === "scan";
  pill.classList.toggle("hidden", !running);
  pill.textContent = running
    ? scanning ? `${job.progress}% · ${job.detail}` : JOB_LABELS[job.kind] || "Working…"
    : "";
  pill.title = running ? job.detail : "";
  const canStop = running && job.cancellable;
  stopJobButton.classList.toggle("hidden", !canStop);
  stopJobButton.dataset.jobId = canStop ? job.id : "";
  stopJobButton.disabled = job?.status === "cancelling";
  stopJobButton.textContent = job?.status === "cancelling" ? "Stopping…" : "Stop job";
  // Only a scan conflicts with starting another scan; other jobs leave the button usable.
  scanButton.disabled = scanning;
  scanButton.textContent = scanning ? "Scanning…" : "Scan library";
  if (running) scheduleStatusRefresh();
}

function scheduleStatusRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(async () => {
    try {
      const wasRunning = state.status?.job && ["queued", "running", "cancelling"].includes(state.status.job.status);
      await refreshStatus();
      const isRunning = state.status?.job && ["queued", "running", "cancelling"].includes(state.status.job.status);
      if (isRunning && state.view === "jobs") await renderJobs();
      if (wasRunning && !isRunning) {
        await reportJobOutcome(state.status.job);
        await loadReferenceData();
        await renderCurrentView();
      }
    } catch (error) {
      toast(error.message, "error");
    }
  }, 1600);
}

async function loadReferenceData() {
  [state.platforms, state.devices] = await Promise.all([api("/api/platforms"), api("/api/devices")]);
  if (!state.deviceId && state.devices.length) state.deviceId = state.devices[0].id;
}

function setHeading(title, subtitle) {
  pageTitle.textContent = title;
  pageSubtitle.textContent = subtitle;
}

function platformOptions() {
  return `<option value="">All platforms</option>${state.platforms.map((item) => `<option value="${escapeHtml(item.platform)}" ${state.platform === item.platform ? "selected" : ""}>${escapeHtml(item.platform)} (${item.count})</option>`).join("")}`;
}

function duplicateLabel(status) {
  const labels = { exact: "Exact duplicate", possible: "Possible duplicate", unique: "Unique" };
  return `<span class="badge ${status}">${labels[status] || status}</span>`;
}

function deviceStateLabel(device) {
  if (device.state === "pending_add") return `${device.name} · pending`;
  if (device.state === "pending_remove") return `${device.name} · removing`;
  return device.name;
}

function deviceSummary(game, interactive) {
  const devices = game.devices || [];
  const visible = devices.slice(0, 2);
  const fullLabel = devices.length ? devices.map(deviceStateLabel).join(", ") : "Not included on a device";
  const tags = devices.length
    ? `<span class="device-tags">${visible.map((device) => `<span class="device-tag ${device.state}">${escapeHtml(deviceStateLabel(device))}</span>`).join("")}${devices.length > 2 ? `<span class="device-tag">+${devices.length - 2}</span>` : ""}</span>`
    : `<span class="device-empty">Not included</span>`;
  if (!interactive) return `<span title="${escapeHtml(fullLabel)}">${tags}</span>`;
  return `<button class="count-button" data-assign-devices="${game.id}" aria-label="Choose devices for ${escapeHtml(game.display_name)}" title="${escapeHtml(fullLabel)}">${tags}</button>`;
}

async function getGames(deviceId = null, deviceScope = "all") {
  state.gamesController?.abort();
  state.gamesController = new AbortController();
  const params = new URLSearchParams({
    search: state.search,
    platform: state.platform,
    duplicate: state.duplicate,
    limit: state.limit,
    offset: state.offset,
  });
  if (deviceId) params.set("device_id", deviceId);
  if (deviceId) params.set("device_scope", deviceScope);
  return api(`/api/games?${params}`, { signal: state.gamesController.signal });
}

function libraryToolbar(includeDuplicate = true) {
  const duplicateOptions = state.view === "duplicates"
    ? `<option value="exact" ${state.duplicate === "exact" ? "selected" : ""}>Exact content</option><option value="possible" ${state.duplicate === "possible" ? "selected" : ""}>Similar filenames</option>`
    : `<option value="all" ${state.duplicate === "all" ? "selected" : ""}>All statuses</option><option value="exact" ${state.duplicate === "exact" ? "selected" : ""}>Exact duplicates</option><option value="possible" ${state.duplicate === "possible" ? "selected" : ""}>Possible duplicates</option><option value="unique" ${state.duplicate === "unique" ? "selected" : ""}>Unique</option>`;
  return `
    <div class="toolbar">
      <label class="search-field">
        <span class="sr-only">Search ROMs</span>
        <input id="search-input" type="search" value="${escapeHtml(state.search)}" placeholder="Search ROMs" autocomplete="off">
      </label>
      <label>
        <span class="sr-only">Platform</span>
        <select id="platform-filter">${platformOptions()}</select>
      </label>
      ${includeDuplicate ? `<label><span class="sr-only">Duplicate status</span><select id="duplicate-filter">${duplicateOptions}</select></label>` : ""}
    </div>`;
}

function bindFilters(callback) {
  let debounce;
  document.querySelector("#search-input")?.addEventListener("input", (event) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.search = event.target.value;
      state.offset = 0;
      callback();
    }, 220);
  });
  document.querySelector("#platform-filter")?.addEventListener("change", (event) => {
    state.platform = event.target.value;
    state.offset = 0;
    callback();
  });
  document.querySelector("#duplicate-filter")?.addEventListener("change", (event) => {
    state.duplicate = event.target.value;
    state.offset = 0;
    callback();
  });
}

function gameRows(items, deviceMode = false) {
  return items.map((game) => {
    const checked = deviceMode ? game.selected : state.selectedRows.has(game.id);
    const editor = state.editingId === game.id ? `
      <tr class="inline-editor">
        <td colspan="8">
          <form class="rename-grid" data-rename-form="${game.id}">
            <div class="field"><label>Current bundle name</label><div class="current-name">${escapeHtml(game.display_name)}${escapeHtml(game.extension)}</div></div>
            <span class="rename-arrow" aria-hidden="true">→</span>
            <div class="field"><label for="rename-${game.id}">New bundle name</label><input class="input" id="rename-${game.id}" name="name" value="${escapeHtml(game.display_name)}" required maxlength="255"></div>
            <div class="bulk-actions"><button type="button" class="button secondary small" data-cancel-rename>Cancel</button><button class="button small">Review rename</button></div>
          </form>
        </td>
      </tr>` : "";
    const assignment = !deviceMode && state.assigningId === game.id ? `
      <tr class="inline-editor">
        <td colspan="8">
          <div class="assignment-panel">
            <div class="assignment-head">
              <div><h3>Include “${escapeHtml(game.display_name)}” on devices</h3><p>Selections update immediately. Apply each device when you are ready to copy or remove files.</p></div>
              <button class="button secondary small" data-close-assignment>Close</button>
            </div>
            ${state.assignmentDevices.length ? `<div class="device-choices">${state.assignmentDevices.map((device) => `<label class="device-choice"><input type="checkbox" data-assignment-checkbox data-device-id="${device.id}" data-game-id="${game.id}" ${device.selected ? "checked" : ""}><span>${escapeHtml(device.name)}</span></label>`).join("")}</div>` : `<p class="meta">No device folders have been discovered. Create a device/roms directory and scan again.</p>`}
          </div>
        </td>
      </tr>` : "";
    return `
      <tr>
        <td class="checkbox-cell"><input type="checkbox" aria-label="Select ${escapeHtml(game.display_name)}" data-${deviceMode ? "device" : "row"}-select="${game.id}" ${checked ? "checked" : ""}></td>
        <td class="name-cell" title="${escapeHtml(game.primary_relpath)}"><strong>${escapeHtml(game.display_name)}</strong><span class="path-line">${escapeHtml(game.primary_relpath)}</span></td>
        <td>${escapeHtml(game.platform)}</td>
        ${deviceMode ? "" : `<td>${duplicateLabel(game.duplicate_status)}</td>`}
        <td class="meta">${formatBytes(game.size)}</td>
        <td class="meta optional-column">${game.file_count} ${game.file_count === 1 ? "file" : "files"}</td>
        <td class="meta optional-column">${deviceMode ? deviceTargetState(game) : deviceSummary(game, true)}</td>
        ${deviceMode ? "" : `<td class="nowrap">
          <button class="button secondary small" data-rename="${game.id}" ${deviceMode ? "disabled" : ""}>Rename</button>
          <button class="button danger-subtle small" data-delete="${game.id}" data-name="${escapeHtml(game.display_name)}" ${deviceMode ? "disabled" : ""}>Trash</button>
        </td>`}
      </tr>${editor}${assignment}`;
  }).join("");
}

function deviceTargetState(game) {
  const states = {
    on_device: ["unique", "On device"],
    pending_add: ["naming-strong", "Pending addition"],
    pending_update: ["naming-strong", "On device · pending update"],
    pending_remove: ["possible", "Pending removal"],
    unmanaged: ["cancelled", "On device · unmanaged"],
    available: ["cancelled", "Not selected"],
  };
  const [badge, label] = states[game.device_state] || states.available;
  return `<span class="badge ${badge}">${label}</span>`;
}

function gamesTable(data, deviceMode = false) {
  if (!data.items.length) {
    const noLibrary = state.status?.games === 0;
    return `<div class="empty-state"><div><h2>${noLibrary ? "Your library has not been indexed" : "No ROMs match these filters"}</h2><p>${noLibrary ? "Mount the platform folders at the configured library root, then scan to build the searchable catalog." : "Try a different title, platform, or duplicate status."}</p>${noLibrary ? '<button class="button" data-scan>Scan library</button>' : '<button class="button secondary" data-clear-filters>Clear filters</button>'}</div></div>`;
  }
  const end = Math.min(data.offset + data.items.length, data.total);
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="checkbox-cell"><input type="checkbox" aria-label="Select visible ROMs" data-select-all></th>
          <th>Filename</th><th>Platform</th>${deviceMode ? "" : "<th>Duplicate status</th>"}<th>Size</th><th class="optional-column">Bundle</th><th class="optional-column">${deviceMode ? "Target state" : "Devices"}</th>${deviceMode ? "" : "<th>Actions</th>"}
        </tr></thead>
        <tbody>${gameRows(data.items, deviceMode)}</tbody>
      </table>
    </div>
    <div class="pager"><span>Showing ${data.offset + 1}–${end} of ${data.total.toLocaleString()}</span><div class="bulk-actions"><button class="button secondary small" data-page="previous" ${data.offset === 0 ? "disabled" : ""}>Previous</button><button class="button secondary small" data-page="next" ${end >= data.total ? "disabled" : ""}>Next</button></div></div>`;
}

function bulkBarHtml() {
  const count = state.selectedRows.size;
  if (!count) return "";
  // Selections survive filter changes, so say plainly when some are no longer on screen.
  const visible = view.querySelectorAll("[data-row-select]");
  let offScreen = 0;
  for (const id of state.selectedRows.keys()) {
    if (![...visible].some((box) => Number(box.dataset.rowSelect) === id)) offScreen += 1;
  }
  const hint = offScreen
    ? `<span class="meta"> · ${offScreen} not shown by the current filters</span>`
    : `<span class="meta"> for library cleanup</span>`;
  return `<div class="bulk-bar"><div><strong>${count} selected</strong>${hint}</div><div class="bulk-actions"><button class="button secondary" data-clear-selection>Clear selection</button><button class="button danger" data-delete-selected>Move ${count} to trash</button></div></div>`;
}

// Row selection is client-side state, so refresh just this strip instead of refetching
// and rebuilding the whole table on every checkbox toggle.
function renderBulkBar() {
  const slot = view.querySelector("#bulk-bar-slot");
  if (!slot) return;
  slot.innerHTML = bulkBarHtml();
  bindBulkBarEvents();
}

function bindBulkBarEvents() {
  view.querySelector("[data-clear-selection]")?.addEventListener("click", () => {
    state.selectedRows.clear();
    view.querySelectorAll("[data-row-select]").forEach((box) => { box.checked = false; });
    const selectAll = view.querySelector("[data-select-all]");
    if (selectAll) { selectAll.checked = false; selectAll.indeterminate = false; }
    renderBulkBar();
  });
  view.querySelector("[data-delete-selected]")?.addEventListener("click", deleteSelected);
}

function syncSelectAll() {
  const selectAll = view.querySelector("[data-select-all]");
  if (!selectAll) return;
  const boxes = [...view.querySelectorAll("[data-row-select]")];
  const checked = boxes.filter((box) => box.checked).length;
  selectAll.checked = boxes.length > 0 && checked === boxes.length;
  selectAll.indeterminate = checked > 0 && checked < boxes.length;
}

async function renderLibrary() {
  setHeading("Library", "Browse, rename, and clean the canonical collection.");
  const data = await getGames();
  setViewHtml(`${libraryToolbar(true)}${gamesTable(data)}<div id="bulk-bar-slot"></div>`);
  renderBulkBar();
  syncSelectAll();
  bindFilters(renderLibrary);
  bindGameEvents(data, false);
}

async function renderDuplicates() {
  setHeading("Duplicates", "Exact hashes first, filename matches for manual review.");
  if (state.duplicate === "all" || state.duplicate === "unique") state.duplicate = "exact";
  const params = new URLSearchParams({
    kind: state.duplicate,
    search: state.search,
    platform: state.platform,
    limit: 30,
    offset: state.offset,
  });
  const data = await api(`/api/duplicates?${params}`);
  if (data.total > 0 && state.offset >= data.total) {
    state.offset = Math.floor((data.total - 1) / 30) * 30;
    return renderDuplicates();
  }
  const possible = state.duplicate === "possible";
  const content = data.items.length
    ? `<div class="duplicate-groups">${data.items.map((group, index) => duplicateGroupHtml(group, index)).join("")}</div>${duplicatePager(data)}`
    : `<div class="empty-state duplicate-empty"><div><h2>No ${possible ? "possible" : "exact"} duplicate groups found</h2><p>${state.search || state.platform ? "Try a different title or platform." : possible ? "No same-platform filenames need manual comparison." : "Every indexed bundle has unique content."}</p></div></div>`;
  setViewHtml(`${libraryToolbar(true)}<div class="section-heading duplicate-heading"><div><h2>${possible ? "Possible duplicate groups" : "Exact duplicate groups"}</h2><p>${possible ? "Names normalize to the same title. Inspect paths and sizes before choosing what to keep." : "Every section contains bundles with the same content hash. Choose one keeper before cleaning up the rest."}</p></div><span class="meta">${data.total.toLocaleString()} ${data.total === 1 ? "group" : "groups"}</span></div>${content}`);
  bindFilters(renderDuplicates);
  bindDuplicateGroups(data.items);
}

function duplicateGroupHtml(group, index) {
  const keeper = state.duplicateKeepers.get(group.key);
  const removeCount = group.items.length - 1;
  const exact = group.kind === "exact";
  const signature = exact ? `Hash ${group.key.slice(0, 10)}` : "Filename match";
  const rows = group.items.map((game) => {
    const checked = keeper === game.id;
    const suggested = group.recommended_keeper_id === game.id;
    const present = game.present_devices || [];
    const selected = (game.selected_devices || []).filter((name) => !present.includes(name));
    const deviceState = present.length || selected.length
      ? `${present.length ? `<span class="device-presence"><strong>On device:</strong> ${escapeHtml(present.join(", "))}</span>` : ""}${selected.length ? `<span class="device-selection"><strong>Selected:</strong> ${escapeHtml(selected.join(", "))}</span>` : ""}`
      : '<span class="device-empty">Not on a device</span>';
    return `<tr class="${checked ? "keeper-row" : ""} ${suggested ? "suggested-keeper-row" : ""}" data-duplicate-row="${game.id}">
      <td class="keeper-cell"><label class="keeper-choice"><input type="radio" name="keeper-${index}" value="${game.id}" data-duplicate-keeper="${index}" ${checked ? "checked" : ""}><span>Keep</span></label>${suggested ? '<span class="badge naming-exact keeper-suggestion">Suggested</span>' : ""}</td>
      <td class="name-cell" title="${escapeHtml(game.primary_relpath)}"><strong>${escapeHtml(game.display_name)}</strong><span class="path-line">${escapeHtml(game.primary_relpath)}</span></td>
      <td>${escapeHtml(game.platform)}</td>
      <td class="meta">${formatBytes(game.size)}</td>
      <td class="meta optional-column">${game.file_count} ${game.file_count === 1 ? "file" : "files"}</td>
      <td class="meta optional-column"><span class="duplicate-device-state">${deviceState}</span></td>
    </tr>`;
  }).join("");
  const deviceGuidance = group.device_conflict
    ? '<p class="duplicate-conflict"><strong>Multiple copies are in use.</strong> Different device folders or pending selections point to different filenames, so ROMmates cannot recommend one keeper.</p>'
    : group.recommended_keeper_id
      ? `<p class="duplicate-recommendation"><strong>Suggested keeper:</strong> ${escapeHtml(group.recommendation_reason)}. Select its Keep option to accept.</p>`
      : '<p>No device currently uses a copy from this set. Choose based on naming and folder organization.</p>';
  return `<section class="duplicate-group" data-duplicate-group="${index}">
    <div class="duplicate-group-head">
      <div><div class="duplicate-group-title"><strong>${group.copies} ${exact ? "identical" : "similarly named"} ${group.copies === 1 ? "copy" : "copies"}</strong><span class="badge ${exact ? "exact" : "possible"}">${signature}</span></div><p>${exact ? `${formatBytes(group.bytes)} across this set. Only one copy is needed.` : "Content differs. Confirm the correct edition, region, or revision before removing anything."}</p>${deviceGuidance}</div>
      <button class="button danger-subtle small" data-clean-duplicate="${index}" ${keeper && !group.device_conflict ? "" : "disabled"}>${group.device_conflict ? "Resolve device usage first" : `Trash ${removeCount} other ${removeCount === 1 ? "copy" : "copies"}`}</button>
    </div>
    <div class="duplicate-table-wrap"><table><thead><tr><th>Decision</th><th>Filename and path</th><th>Platform</th><th>Size</th><th class="optional-column">Bundle</th><th class="optional-column">Devices</th></tr></thead><tbody>${rows}</tbody></table></div>
  </section>`;
}

function duplicatePager(data) {
  const end = Math.min(data.offset + data.items.length, data.total);
  return `<div class="pager"><span>Showing groups ${data.offset + 1}–${end} of ${data.total.toLocaleString()}</span><div class="bulk-actions"><button class="button secondary small" data-duplicate-page="previous" ${data.offset === 0 ? "disabled" : ""}>Previous</button><button class="button secondary small" data-duplicate-page="next" ${end >= data.total ? "disabled" : ""}>Next</button></div></div>`;
}

function bindDuplicateGroups(groups) {
  view.querySelectorAll("[data-duplicate-keeper]").forEach((radio) => radio.addEventListener("change", () => {
    const index = Number(radio.dataset.duplicateKeeper);
    const group = groups[index];
    state.duplicateKeepers.set(group.key, Number(radio.value));
    const section = view.querySelector(`[data-duplicate-group="${index}"]`);
    section.querySelectorAll("[data-duplicate-row]").forEach((row) => row.classList.toggle("keeper-row", Number(row.dataset.duplicateRow) === Number(radio.value)));
    section.querySelector("[data-clean-duplicate]").disabled = group.device_conflict;
  }));
  view.querySelectorAll("[data-clean-duplicate]").forEach((button) => button.addEventListener("click", () => cleanDuplicateGroup(groups[Number(button.dataset.cleanDuplicate)])));
  view.querySelectorAll("[data-duplicate-page]").forEach((button) => button.addEventListener("click", () => {
    state.offset = Math.max(0, state.offset + (button.dataset.duplicatePage === "next" ? 30 : -30));
    renderDuplicates();
  }));
}

async function cleanDuplicateGroup(group) {
  const keeperId = state.duplicateKeepers.get(group.key);
  const keeper = group.items.find((game) => game.id === keeperId);
  if (!keeper) return;
  const removals = group.items.filter((game) => game.id !== keeperId);
  const affectedDevices = [...new Set(removals.flatMap((game) => [
    ...(game.present_devices || []),
    ...(game.selected_devices || []),
  ]))];
  const confirmed = await confirmAction({
    title: `Keep “${keeper.display_name}” and trash ${removals.length} ${removals.length === 1 ? "copy" : "copies"}?`,
    content: `<p class="warning-copy">Keeping <strong>${escapeHtml(keeper.primary_relpath)}</strong>. The following recoverable bundles will move to Trash, including their companion files and managed device copies.</p><ul class="confirm-list">${removals.map((game) => `<li>${escapeHtml(game.primary_relpath)} (${formatBytes(game.size)})</li>`).join("")}</ul>${affectedDevices.length ? `<p class="issue-warning"><strong>Device impact:</strong> A removed copy is present or selected on ${escapeHtml(affectedDevices.join(", "))}. Keeping the suggested copy avoids this warning.</p>` : ""}${group.kind === "possible" ? '<p class="issue-warning"><strong>Content is not identical.</strong> These files only have similar names.</p>' : ""}`,
    confirmLabel: `Trash ${removals.length} ${removals.length === 1 ? "copy" : "copies"}`,
    cancelLabel: "Review again",
    danger: true,
  });
  if (!confirmed) return;
  let completed = 0;
  for (const game of removals) {
    try {
      await requestJob(`/api/games/${game.id}`, { method: "DELETE" }, `Moving duplicate ${completed + 1} of ${removals.length}`);
      completed += 1;
    } catch (error) {
      toast(`Stopped after ${completed}: ${error.message}`, "error");
      break;
    }
  }
  state.duplicateKeepers.delete(group.key);
  toast(`Kept ${keeper.display_name}; moved ${completed} ${completed === 1 ? "copy" : "copies"} to trash`);
  await refreshStatus();
  await loadReferenceData();
  await renderDuplicates();
}

function bindGameEvents(data, deviceMode) {
  const byId = new Map(data.items.map((game) => [game.id, game]));
  view.querySelectorAll("[data-row-select]").forEach((checkbox) => checkbox.addEventListener("change", () => {
    const id = Number(checkbox.dataset.rowSelect);
    const game = byId.get(id);
    if (checkbox.checked) {
      state.selectedRows.set(id, { display_name: game?.display_name ?? `Game ${id}`, platform: game?.platform ?? "" });
    } else {
      state.selectedRows.delete(id);
    }
    syncSelectAll();
    renderBulkBar();
  }));
  if (!deviceMode) {
    view.querySelector("[data-select-all]")?.addEventListener("change", (event) => {
      const checked = event.target.checked;
      data.items.forEach((game) => {
        if (checked) state.selectedRows.set(game.id, { display_name: game.display_name, platform: game.platform });
        else state.selectedRows.delete(game.id);
      });
      view.querySelectorAll("[data-row-select]").forEach((box) => { box.checked = checked; });
      event.target.indeterminate = false;
      renderBulkBar();
    });
  }
  view.querySelectorAll("[data-rename]").forEach((button) => button.addEventListener("click", () => {
    state.editingId = state.editingId === Number(button.dataset.rename) ? null : Number(button.dataset.rename);
    state.assigningId = null;
    renderCurrentView();
  }));
  view.querySelectorAll("[data-cancel-rename]").forEach((button) => button.addEventListener("click", () => {
    state.editingId = null;
    renderCurrentView();
  }));
  view.querySelectorAll("[data-rename-form]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = Number(form.dataset.renameForm);
    const name = new FormData(form).get("name");
    try {
      const detail = await api(`/api/games/${id}`);
      const visibleBundleNames = detail.files.slice(0, 8).map((item) => item.relpath.split("/").pop());
      const bundleNames = visibleBundleNames.join(", ");
      const bundleOverflow = detail.files.length > visibleBundleNames.length
        ? `, and ${detail.files.length - visibleBundleNames.length} more`
        : "";
      const confirmed = await confirmAction({
        title: "Rename this bundle?",
        content: `<p class="warning-copy">ROMmates will rename the complete file or folder bundle. References inside CUE, GDI, and M3U descriptors will be updated.</p><p><strong>${escapeHtml(bundleNames + bundleOverflow)}</strong></p>`,
        confirmLabel: "Rename bundle",
        cancelLabel: "Keep current name",
        danger: false,
      });
      if (!confirmed) return;
      const result = await requestJob(
        `/api/games/${id}/rename`,
        { method: "PATCH", body: JSON.stringify({ name }) },
        "Bundle rename queued",
      );
      state.editingId = null;
      toast(`Renamed ${result.old_name} to ${result.new_name}`);
      await loadReferenceData();
      await refreshStatus();
      await renderCurrentView();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => deleteOne(Number(button.dataset.delete), button.dataset.name)));
  view.querySelectorAll("[data-assign-devices]").forEach((button) => button.addEventListener("click", async () => {
    const gameId = Number(button.dataset.assignDevices);
    if (state.assigningId === gameId) {
      state.assigningId = null;
      state.assignmentDevices = [];
      await renderCurrentView();
      return;
    }
    try {
      const detail = await api(`/api/games/${gameId}`);
      state.assigningId = gameId;
      state.assignmentDevices = detail.devices;
      state.editingId = null;
      await renderCurrentView();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelector("[data-close-assignment]")?.addEventListener("click", () => {
    state.assigningId = null;
    state.assignmentDevices = [];
    renderCurrentView();
  });
  view.querySelectorAll("[data-assignment-checkbox]").forEach((checkbox) => checkbox.addEventListener("change", async () => {
    checkbox.disabled = true;
    const deviceId = Number(checkbox.dataset.deviceId);
    const gameId = Number(checkbox.dataset.gameId);
    try {
      await api(`/api/devices/${deviceId}/selection`, {
        method: "PUT",
        body: JSON.stringify({ game_id: gameId, selected: checkbox.checked }),
      });
      const device = state.assignmentDevices.find((item) => item.id === deviceId);
      if (device) device.selected = checkbox.checked ? 1 : 0;
      toast(`${checkbox.checked ? "Included on" : "Removed from"} ${device?.name || "device"}. Apply that device to sync files.`);
      await renderCurrentView();
    } catch (error) {
      checkbox.checked = !checkbox.checked;
      checkbox.disabled = false;
      toast(error.message, "error");
    }
  }));
  view.querySelector("[data-clear-filters]")?.addEventListener("click", () => { state.search = ""; state.platform = ""; state.duplicate = state.view === "duplicates" ? "exact" : "all"; renderCurrentView(); });
  view.querySelector("[data-scan]")?.addEventListener("click", () => startScan());
  view.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => {
    state.offset = Math.max(0, state.offset + (button.dataset.page === "next" ? state.limit : -state.limit));
    renderCurrentView();
  }));
}

async function deleteOne(id, name) {
  try {
    const detail = await api(`/api/games/${id}`);
    const selectedDevices = detail.devices.filter((item) => item.selected).map((item) => item.name);
    const confirmed = await confirmAction({
      title: `Move “${name}” to trash?`,
      content: `<p class="warning-copy">This moves all <strong>${detail.files.length} bundle ${detail.files.length === 1 ? "file" : "files"}</strong> out of the canonical library.${selectedDevices.length ? ` Deployed copies on <strong>${escapeHtml(selectedDevices.join(", "))}</strong> will be removed.` : ""} You can restore the bundle from Trash.</p>`,
      confirmLabel: "Move bundle to trash",
      cancelLabel: "Keep ROM",
      danger: true,
    });
    if (!confirmed) return;
    const result = await requestJob(`/api/games/${id}`, { method: "DELETE" }, "Move to trash queued");
    state.selectedRows.delete(id);
    toast(`Moved ${name} and ${result.files} ${result.files === 1 ? "file" : "files"} to trash`);
    await refreshStatus();
    await loadReferenceData();
    await renderCurrentView();
  } catch (error) { toast(error.message, "error"); }
}

async function deleteSelected() {
  const entries = [...state.selectedRows.entries()];
  const count = entries.length;
  // Selections persist across filter and page changes, so name every bundle that is
  // actually about to be trashed rather than only the ones currently on screen.
  const names = entries.map(([, game]) => `${game.display_name}${game.platform ? ` (${game.platform})` : ""}`);
  const preview = names.slice(0, 12).map((name) => `<li>${escapeHtml(name)}</li>`).join("");
  const overflow = count > 12 ? `<p class="meta">…and ${count - 12} more.</p>` : "";
  const confirmed = await confirmAction({
    title: `Move ${count} ${count === 1 ? "bundle" : "bundles"} to trash?`,
    content: `<p class="warning-copy">Each selected game and every file in its bundle will move to recoverable trash. Deployed copies managed by this app will be removed.</p><ul class="confirm-list">${preview}</ul>${overflow}`,
    confirmLabel: `Move ${count} ${count === 1 ? "bundle" : "bundles"} to trash`,
    cancelLabel: "Keep selected ROMs",
    danger: true,
  });
  if (!confirmed) return;
  let completed = 0;
  for (const [id] of entries) {
    try { await requestJob(`/api/games/${id}`, { method: "DELETE" }, `Moving bundle ${completed + 1} of ${count}`); completed += 1; }
    catch (error) { toast(`Stopped after ${completed}: ${error.message}`, "error"); break; }
  }
  state.selectedRows.clear();
  toast(`Moved ${completed} ${completed === 1 ? "bundle" : "bundles"} to trash`);
  await refreshStatus();
  await loadReferenceData();
  await renderCurrentView();
}

async function renderDevices() {
  setHeading("Devices", "See what is present now, then choose what changes next.");
  if (!state.devices.length) {
    setViewHtml(`<div class="empty-state"><div><h2>No device folders found</h2><p>Create a directory such as <code>/devices/retroid/roms</code>, then scan the library. Device folders are discovered automatically.</p><button class="button" data-scan>Scan again</button></div></div>`);
    view.querySelector("[data-scan]")?.addEventListener("click", () => startScan());
    return;
  }
  const device = state.devices.find((item) => item.id === Number(state.deviceId)) || state.devices[0];
  state.deviceId = device.id;
  const [data, preview] = await Promise.all([
    getGames(device.id, state.deviceScope),
    api(`/api/devices/${device.id}/preview`),
  ]);
  const inventory = data.device_inventory;
  const noFilters = !state.search && !state.platform;
  let table = gamesTable(data, true);
  if (!data.items.length && noFilters && state.deviceScope === "on_device") {
    table = `<div class="empty-state device-empty-state"><div><h2>No matching library ROMs are on ${escapeHtml(device.name)}</h2><p>ROMmates checks the actual device directory. Browse the library to choose games, or scan the canonical library if these filenames should already match.</p><button class="button secondary" data-device-empty-browse>Browse library</button></div></div>`;
  } else if (!data.items.length && noFilters && state.deviceScope === "changes") {
    table = `<div class="empty-state device-empty-state"><div><h2>${escapeHtml(device.name)} is up to date</h2><p>The desired selection matches the ROMs currently present in its device directory.</p></div></div>`;
  }
  setViewHtml(`
    <div class="device-strip">
      <label class="field"><span>Target device</span><select id="device-select">${state.devices.map((item) => `<option value="${item.id}" ${item.id === device.id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>
      <div class="device-summary"><span><strong>${inventory.present_games}</strong> currently on device</span><span><strong>${preview.games}</strong> desired</span><span><strong>${preview.additions}</strong> files to add/update</span><span><strong>${preview.removals}</strong> files to remove</span><button class="button" id="apply-device" ${preview.additions === 0 && preview.removals === 0 ? "disabled" : ""}>Review and apply</button></div>
    </div>
    <div class="device-scope" role="group" aria-label="Device ROM view">
      <button class="scope-button ${state.deviceScope === "on_device" ? "active" : ""}" data-device-scope="on_device" aria-pressed="${state.deviceScope === "on_device"}">On device <span>${inventory.present_games.toLocaleString()}</span></button>
      <button class="scope-button ${state.deviceScope === "changes" ? "active" : ""}" data-device-scope="changes" aria-pressed="${state.deviceScope === "changes"}">Pending changes <span>${inventory.changes.toLocaleString()}</span></button>
      <button class="scope-button ${state.deviceScope === "all" ? "active" : ""}" data-device-scope="all" aria-pressed="${state.deviceScope === "all"}">Browse library</button>
    </div>
    ${inventory.unmatched_files ? `<p class="device-inventory-note"><strong>${inventory.unmatched_files.toLocaleString()}</strong> ${inventory.unmatched_files === 1 ? "file does" : "files do"} not match a bundle in the current library index.</p>` : ""}
    ${libraryToolbar(false)}${table}`);
  bindFilters(renderDevices);
  document.querySelector("#device-select").addEventListener("change", (event) => {
    state.deviceId = Number(event.target.value);
    state.deviceScope = "on_device";
    state.offset = 0;
    renderDevices();
  });
  view.querySelectorAll("[data-device-scope]").forEach((button) => button.addEventListener("click", () => {
    state.deviceScope = button.dataset.deviceScope;
    state.offset = 0;
    renderDevices();
  }));
  view.querySelector("[data-device-empty-browse]")?.addEventListener("click", () => {
    state.deviceScope = "all";
    state.offset = 0;
    renderDevices();
  });
  view.querySelectorAll("[data-device-select]").forEach((checkbox) => checkbox.addEventListener("change", async () => {
    checkbox.disabled = true;
    try {
      await api(`/api/devices/${device.id}/selection`, { method: "PUT", body: JSON.stringify({ game_id: Number(checkbox.dataset.deviceSelect), selected: checkbox.checked }) });
      await renderDevices();
    } catch (error) { checkbox.checked = !checkbox.checked; checkbox.disabled = false; toast(error.message, "error"); }
  }));
  view.querySelector("[data-select-all]")?.addEventListener("change", async (event) => {
    const checkbox = event.target;
    checkbox.disabled = true;
    try {
      await api(`/api/devices/${device.id}/selections`, {
        method: "PUT",
        body: JSON.stringify({ game_ids: data.items.map((game) => game.id), selected: checkbox.checked }),
      });
      toast(`${checkbox.checked ? "Selected" : "Unselected"} ${data.items.length} visible games`);
      await renderDevices();
    } catch (error) { checkbox.checked = !checkbox.checked; checkbox.disabled = false; toast(error.message, "error"); }
  });
  view.querySelector("#apply-device")?.addEventListener("click", async () => {
    const confirmed = await confirmAction({
      title: `Apply changes to ${device.name}?`,
      content: `<p class="warning-copy"><strong>${preview.additions} ${preview.additions === 1 ? "file" : "files"}</strong> will be copied and <strong>${preview.removals} managed ${preview.removals === 1 ? "file" : "files"}</strong> will be removed. AppleDouble and .DS_Store metadata in the device ROM directory will also be cleaned.</p>`,
      confirmLabel: "Apply device changes",
      cancelLabel: "Keep current device files",
      danger: preview.removals > 0,
    });
    if (!confirmed) return;
    try {
      const result = await requestJob(`/api/devices/${device.id}/apply`, { method: "POST" }, `Applying ${device.name}`);
      toast(`Applied ${device.name}: ${result.copied} copied, ${result.removed} removed, ${result.unchanged} unchanged`);
      await loadReferenceData();
      await renderDevices();
    } catch (error) { toast(error.message, "error"); }
  });
  bindGameEvents(data, true);
}

async function renderTrash() {
  setHeading("Trash", "Restore bundles or permanently delete them.");
  const items = await api("/api/trash");
  if (!items.length) {
    setViewHtml(`<div class="empty-state"><div><h2>Trash is empty</h2><p>Deleted ROM bundles remain recoverable here until you permanently delete them.</p></div></div>`);
    return;
  }
  setViewHtml(`<div class="table-wrap"><table><thead><tr><th>Game</th><th>Platform</th><th>Files</th><th>Deleted</th><th>Actions</th></tr></thead><tbody>${items.map((item) => `<tr><td class="name-cell"><strong>${escapeHtml(item.game_name)}</strong><span class="path-line">${escapeHtml(item.original_relpath)}</span></td><td>${escapeHtml(item.platform)}</td><td class="meta">${item.file_count}</td><td class="meta">${escapeHtml(item.deleted_at)} UTC</td><td><button class="button secondary small" data-restore="${item.id}">Restore</button> <button class="button danger-subtle small" data-purge="${item.id}" data-name="${escapeHtml(item.game_name)}">Delete permanently</button></td></tr>`).join("")}</tbody></table></div>`);
  view.querySelectorAll("[data-restore]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const result = await requestJob(`/api/trash/${button.dataset.restore}/restore`, { method: "POST" }, "Restore queued");
      toast(`Restored ${result.restored}`);
      await refreshStatus(); await loadReferenceData(); await renderTrash();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelectorAll("[data-purge]").forEach((button) => button.addEventListener("click", async () => {
    const confirmed = await confirmAction({ title: `Permanently delete “${button.dataset.name}”?`, content: `<p class="warning-copy">This erases the trashed bundle and cannot be undone.</p>`, confirmLabel: "Delete permanently", cancelLabel: "Keep in trash", danger: true });
    if (!confirmed) return;
    try {
      const result = await requestJob(`/api/trash/${button.dataset.purge}`, { method: "DELETE" }, "Permanent deletion queued");
      toast(`Permanently deleted ${result.purged}`);
      await refreshStatus(); await renderTrash();
    } catch (error) { toast(error.message, "error"); }
  }));
}

function saveTabs(overview) {
  return `<div class="device-scope save-tabs" role="tablist" aria-label="Save management view">
    <button class="scope-button ${state.saveTab === "current" ? "active" : ""}" data-save-tab="current" role="tab" aria-selected="${state.saveTab === "current"}">Current saves</button>
    <button class="scope-button ${state.saveTab === "snapshots" ? "active" : ""}" data-save-tab="snapshots" role="tab" aria-selected="${state.saveTab === "snapshots"}">Snapshots <span>${overview.snapshot_count.toLocaleString()}</span></button>
    <button class="scope-button ${state.saveTab === "settings" ? "active" : ""}" data-save-tab="settings" role="tab" aria-selected="${state.saveTab === "settings"}">Settings</button>
  </div>`;
}

function saveHeader(overview) {
  const settings = overview.settings;
  const latest = overview.latest_snapshot;
  return `<div class="save-strip">
    <div class="save-source"><div><span class="badge ${settings.available ? "unique" : "exact"}">${settings.available ? "Source available" : "Source unavailable"}</span><strong>RetroArch cloud saves</strong></div><code title="${escapeHtml(settings.source_root)}">${escapeHtml(settings.source_root)}</code><span class="meta">${latest ? `Last snapshot ${escapeHtml(latest.created_at)} UTC` : "No snapshots yet"}</span></div>
    <form class="snapshot-now" data-snapshot-form><label class="field"><span>Snapshot note (optional)</span><input class="input" name="note" maxlength="500" placeholder="Before a long trip, before testing a core…"></label><button class="button" ${settings.available ? "" : "disabled"}>Snapshot now</button></form>
  </div>`;
}

function currentSavesHtml(data) {
  const toolbar = `<div class="toolbar"><label class="search-field"><span class="sr-only">Search current saves</span><input id="save-search" type="search" value="${escapeHtml(state.saveSearch)}" placeholder="Search save paths" autocomplete="off"></label></div>`;
  if (!data.available) {
    return `${toolbar}<div class="empty-state save-empty"><div><h2>Save source is not mounted</h2><p>Mount the WebDAV backing directory at the configured save source path, then refresh ROMmates.</p></div></div>`;
  }
  if (!data.items.length) {
    return `${toolbar}<div class="empty-state save-empty"><div><h2>${state.saveSearch ? "No saves match this search" : "No save files found"}</h2><p>${state.saveSearch ? "Try part of the filename or relative directory." : "RetroArch has not uploaded anything to this WebDAV directory yet."}</p></div></div>`;
  }
  const end = Math.min(data.offset + data.items.length, data.total);
  return `${toolbar}<div class="table-wrap"><table><thead><tr><th>Save path</th><th>Size</th><th>Modified</th></tr></thead><tbody>${data.items.map((item) => `<tr><td class="name-cell"><code class="save-path">${escapeHtml(item.relpath)}</code></td><td class="meta">${formatBytes(item.size)}</td><td class="meta">${new Date(Number(item.mtime_ns) / 1e6).toLocaleString()}</td></tr>`).join("")}</tbody></table></div><div class="pager"><span>Showing ${data.offset + 1}–${end} of ${data.total.toLocaleString()}</span><div class="bulk-actions"><button class="button secondary small" data-save-page="previous" ${data.offset === 0 ? "disabled" : ""}>Previous</button><button class="button secondary small" data-save-page="next" ${end >= data.total ? "disabled" : ""}>Next</button></div></div>`;
}

function snapshotChangeSummary(snapshot) {
  if (!snapshot.added_count && !snapshot.changed_count && !snapshot.removed_count) return "No content changes";
  return `+${snapshot.added_count} · ~${snapshot.changed_count} · −${snapshot.removed_count}`;
}

function snapshotDetailHtml(detail, comparison) {
  const snapshot = detail.snapshot;
  const sourceAvailable = Boolean(comparison);
  const end = Math.min(detail.offset + detail.files.length, detail.total);
  const files = detail.files.length
    ? `<div class="table-wrap snapshot-files"><table><thead><tr><th>Historical file</th><th>Size</th><th>Action</th></tr></thead><tbody>${detail.files.map((item) => `<tr><td class="name-cell"><code class="save-path">${escapeHtml(item.relpath)}</code></td><td class="meta">${formatBytes(item.size)}</td><td><button class="button secondary small" data-download-save="${escapeHtml(item.relpath)}">Download</button></td></tr>`).join("")}</tbody></table></div><div class="pager"><span>Showing ${detail.offset + 1}–${end} of ${detail.total.toLocaleString()}</span><div class="bulk-actions"><button class="button secondary small" data-snapshot-file-page="previous" ${detail.offset === 0 ? "disabled" : ""}>Previous</button><button class="button secondary small" data-snapshot-file-page="next" ${end >= detail.total ? "disabled" : ""}>Next</button></div></div>`
    : `<p class="report-empty">This snapshot contains no files.</p>`;
  const changes = comparison ? [
    ...comparison.restore.map((path) => ["Restore missing", path]),
    ...comparison.overwrite.map((path) => ["Overwrite current", path]),
    ...comparison.delete.map((path) => ["Delete current", path]),
  ] : [];
  const changeReview = !sourceAvailable
    ? `<p class="issue-warning" role="note">The live save source is unavailable. Historical files can still be inspected and downloaded, but comparison and restore are disabled.</p>`
    : changes.length
    ? `<details class="restore-changes"><summary>Review all ${changes.length.toLocaleString()} filesystem changes</summary><div class="table-wrap"><table><thead><tr><th>Action</th><th>Path</th></tr></thead><tbody>${changes.map(([action, path]) => `<tr><td>${escapeHtml(action)}</td><td><code class="save-path">${escapeHtml(path)}</code></td></tr>`).join("")}</tbody></table></div></details>`
    : `<p class="report-empty">The live cloud state already matches this snapshot.</p>`;
  const fileSearch = `<div class="toolbar snapshot-search"><label class="search-field"><span class="sr-only">Search snapshot files</span><input id="snapshot-search" type="search" value="${escapeHtml(state.saveSnapshotSearch)}" placeholder="Search files in this snapshot" autocomplete="off"></label></div>`;
  return `<section class="snapshot-detail" aria-labelledby="snapshot-detail-title"><div class="section-heading report-heading"><div><h2 id="snapshot-detail-title">Snapshot #${snapshot.id}</h2><p>${escapeHtml(snapshot.created_at)} UTC · ${escapeHtml(snapshot.trigger)}${snapshot.note ? ` · ${escapeHtml(snapshot.note)}` : ""}</p></div><button class="button secondary small" data-close-snapshot>Close</button></div><dl class="report-grid report-summary"><div><dt>Files</dt><dd>${snapshot.file_count.toLocaleString()}</dd></div><div><dt>Logical size</dt><dd>${formatBytes(snapshot.logical_bytes)}</dd></div><div><dt>New storage</dt><dd>${formatBytes(snapshot.new_bytes)}</dd></div><div><dt>Restore missing</dt><dd>${sourceAvailable ? comparison.restore.length.toLocaleString() : "Unavailable"}</dd></div><div><dt>Overwrite</dt><dd>${sourceAvailable ? comparison.overwrite.length.toLocaleString() : "Unavailable"}</dd></div><div><dt>Delete current</dt><dd>${sourceAvailable ? comparison.delete.length.toLocaleString() : "Unavailable"}</dd></div></dl>${changeReview}<div class="restore-panel"><div><h3>Restore this complete cloud state</h3><p>ROMmates will first make a safety snapshot, then restore saves and RetroArch sync manifests together. If the live files changed after this comparison, the job will stop.</p><label class="restore-check"><input type="checkbox" data-retroarch-closed ${sourceAvailable ? "" : "disabled"}> <span>I closed RetroArch on every device and let the last sync finish.</span></label></div><button class="button danger" data-restore-snapshot ${sourceAvailable && changes.length ? "" : "disabled"}>Review restore</button></div><div class="section-heading snapshot-file-heading"><div><h3>Files in this snapshot</h3><p>Historical files can be downloaded without changing the live WebDAV directory.</p></div></div>${fileSearch}${files}</section>`;
}

function snapshotsHtml(data, detail, comparison) {
  const table = data.items.length
    ? `<div class="table-wrap"><table><thead><tr><th>Snapshot</th><th>Trigger</th><th>Changes</th><th>Files</th><th>Logical size</th><th>New storage</th><th>Actions</th></tr></thead><tbody>${data.items.map((item) => `<tr${state.saveSnapshotId === item.id ? ` class="selected-row"` : ""}><td class="nowrap"><strong>#${item.id}</strong><span class="path-line">${escapeHtml(item.created_at)} UTC</span></td><td>${escapeHtml(item.trigger)}${item.pinned ? ` <span class="badge naming-strong">Pinned</span>` : ""}</td><td class="meta">${snapshotChangeSummary(item)}</td><td class="meta">${item.file_count.toLocaleString()}</td><td class="meta">${formatBytes(item.logical_bytes)}</td><td class="meta">${formatBytes(item.new_bytes)}</td><td><div class="bulk-actions"><button class="button secondary small" data-open-snapshot="${item.id}">${state.saveSnapshotId === item.id ? "Refresh" : "Compare"}</button><button class="button secondary small" data-pin-snapshot="${item.id}" data-pinned="${item.pinned ? "true" : "false"}">${item.pinned ? "Unpin" : "Pin"}</button></div></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty-state save-empty"><div><h2>No save snapshots yet</h2><p>Create one manually now. Automatic snapshots begin after the source is mounted and scheduling is enabled.</p></div></div>`;
  return `${table}${detail ? snapshotDetailHtml(detail, comparison) : ""}`;
}

function saveSettingsHtml(settings) {
  return `<form class="save-settings" data-save-settings><div class="settings-section"><h2>Schedule</h2><p>Set the interval to 0 to disable scheduled snapshots while leaving manual snapshots available.</p><label class="device-choice"><input type="checkbox" name="enabled" ${settings.enabled ? "checked" : ""}><span>Enable automatic snapshots</span></label><label class="field"><span>Interval in minutes</span><input class="input" type="number" name="interval_minutes" min="0" max="10080" value="${settings.interval_minutes}"></label></div><div class="settings-section"><h2>Retention</h2><p>Pinned snapshots are always retained. Each older tier keeps one representative snapshot per period.</p><div class="retention-grid"><label class="field"><span>Recent snapshots</span><input class="input" type="number" name="retention_recent" min="1" max="1000" value="${settings.retention_recent}"></label><label class="field"><span>Daily copies</span><input class="input" type="number" name="retention_daily" min="0" max="3650" value="${settings.retention_daily}"></label><label class="field"><span>Weekly copies</span><input class="input" type="number" name="retention_weekly" min="0" max="520" value="${settings.retention_weekly}"></label><label class="field"><span>Monthly copies</span><input class="input" type="number" name="retention_monthly" min="0" max="240" value="${settings.retention_monthly}"></label></div></div><div class="settings-paths"><span>Live source <code>${escapeHtml(settings.source_root)}</code></span><span>Snapshot storage <code>${escapeHtml(settings.snapshots_root)}</code></span></div><button class="button">Save settings</button></form>`;
}

async function renderSaves() {
  setHeading("Saves", "Snapshot and restore the complete RetroArch cloud state.");
  const overview = await api("/api/saves");
  let content = "";
  let currentData = null;
  let snapshotData = null;
  let snapshotDetail = null;
  let comparison = null;
  if (state.saveTab === "current") {
    const params = new URLSearchParams({ search: state.saveSearch, limit: 250, offset: state.saveOffset });
    currentData = await api(`/api/saves/current?${params}`);
    content = currentSavesHtml(currentData);
  } else if (state.saveTab === "snapshots") {
    snapshotData = await api("/api/saves/snapshots?limit=100");
    if (state.saveSnapshotId && snapshotData.items.some((item) => item.id === state.saveSnapshotId)) {
      const params = new URLSearchParams({ search: state.saveSnapshotSearch, limit: 250, offset: state.saveSnapshotOffset });
      if (overview.settings.available) {
        [snapshotDetail, comparison] = await Promise.all([
          api(`/api/saves/snapshots/${state.saveSnapshotId}?${params}`),
          api(`/api/saves/snapshots/${state.saveSnapshotId}/compare`),
        ]);
      } else {
        snapshotDetail = await api(`/api/saves/snapshots/${state.saveSnapshotId}?${params}`);
      }
    } else {
      state.saveSnapshotId = null;
    }
    content = snapshotsHtml(snapshotData, snapshotDetail, comparison);
  } else {
    content = saveSettingsHtml(overview.settings);
  }
  setViewHtml(`${saveHeader(overview)}${saveTabs(overview)}${content}`);
  view.querySelectorAll("[data-save-tab]").forEach((button) => button.addEventListener("click", () => {
    state.saveTab = button.dataset.saveTab;
    state.saveOffset = 0;
    renderSaves();
  }));
  view.querySelector("[data-snapshot-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      const note = new FormData(event.currentTarget).get("note") || "";
      const result = await requestJob(
        "/api/saves/snapshots",
        { method: "POST", body: JSON.stringify({ note }) },
        "Save snapshot started",
      );
      toast(result.unchanged ? "Save files have not changed" : `Created save snapshot #${result.snapshot_id}`);
      await refreshStatus();
      await renderSaves();
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  });
  const saveSearch = view.querySelector("#save-search");
  let searchTimer;
  saveSearch?.addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.saveSearch = event.target.value;
      state.saveOffset = 0;
      renderSaves();
    }, 220);
  });
  view.querySelectorAll("[data-save-page]").forEach((button) => button.addEventListener("click", () => {
    state.saveOffset = Math.max(0, state.saveOffset + (button.dataset.savePage === "next" ? 250 : -250));
    renderSaves();
  }));
  view.querySelectorAll("[data-open-snapshot]").forEach((button) => button.addEventListener("click", () => {
    state.saveSnapshotId = Number(button.dataset.openSnapshot);
    state.saveSnapshotOffset = 0;
    renderSaves();
  }));
  view.querySelector("[data-close-snapshot]")?.addEventListener("click", () => {
    state.saveSnapshotId = null;
    renderSaves();
  });
  const snapshotSearch = view.querySelector("#snapshot-search");
  let snapshotSearchTimer;
  snapshotSearch?.addEventListener("input", (event) => {
    clearTimeout(snapshotSearchTimer);
    snapshotSearchTimer = setTimeout(() => {
      state.saveSnapshotSearch = event.target.value;
      state.saveSnapshotOffset = 0;
      renderSaves();
    }, 220);
  });
  view.querySelectorAll("[data-pin-snapshot]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/saves/snapshots/${button.dataset.pinSnapshot}/pin`, {
        method: "PUT",
        body: JSON.stringify({ pinned: button.dataset.pinned !== "true" }),
      });
      await renderSaves();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelectorAll("[data-snapshot-file-page]").forEach((button) => button.addEventListener("click", () => {
    state.saveSnapshotOffset = Math.max(0, state.saveSnapshotOffset + (button.dataset.snapshotFilePage === "next" ? 250 : -250));
    renderSaves();
  }));
  view.querySelectorAll("[data-download-save]").forEach((button) => button.addEventListener("click", async () => {
    const relpath = button.dataset.downloadSave;
    const encoded = relpath.split("/").map(encodeURIComponent).join("/");
    try { await downloadApiFile(`/api/saves/snapshots/${state.saveSnapshotId}/files/${encoded}`, relpath.split("/").pop()); }
    catch (error) { toast(error.message, "error"); }
  }));
  view.querySelector("[data-restore-snapshot]")?.addEventListener("click", async () => {
    if (!comparison) return;
    if (!view.querySelector("[data-retroarch-closed]")?.checked) {
      toast("Confirm that RetroArch is closed on every device first", "error");
      return;
    }
    const confirmed = await confirmAction({
      title: `Restore save snapshot #${state.saveSnapshotId}?`,
      content: `<p class="warning-copy">ROMmates will create a safety snapshot, then overwrite <strong>${comparison.overwrite.length}</strong>, restore <strong>${comparison.restore.length}</strong>, and remove <strong>${comparison.delete.length}</strong> current files. RetroArch sync manifests are restored with the saves.</p>`,
      confirmLabel: "Restore complete snapshot",
      cancelLabel: "Keep current saves",
      danger: true,
    });
    if (!confirmed) return;
    try {
      const result = await requestJob(
        `/api/saves/snapshots/${state.saveSnapshotId}/restore`,
        { method: "POST", body: JSON.stringify({ expected_tree_hash: comparison.current_tree_hash, retroarch_closed: true }) },
        "Save restore started",
      );
      toast(`Restored ${result.files} files; safety snapshot #${result.safety_snapshot_id} was created`);
      state.saveSnapshotId = null;
      await refreshStatus();
      await renderSaves();
    } catch (error) { toast(error.message, "error"); }
  });
  view.querySelector("[data-save-settings]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const payload = {
      enabled: values.get("enabled") === "on",
      interval_minutes: Number(values.get("interval_minutes")),
      retention_recent: Number(values.get("retention_recent")),
      retention_daily: Number(values.get("retention_daily")),
      retention_weekly: Number(values.get("retention_weekly")),
      retention_monthly: Number(values.get("retention_monthly")),
    };
    try {
      const result = await api("/api/saves/settings", { method: "PUT", body: JSON.stringify(payload) });
      toast(result.pruned ? `Settings saved; pruned ${result.pruned} expired snapshots` : "Save settings updated");
      await renderSaves();
    } catch (error) { toast(error.message, "error"); }
  });
}

function namingConfidenceLabel(value) {
  return { exact: "Exact DAT match", strong: "Strong name match", cleanup: "Cleanup only" }[value] || value;
}

async function renderNaming() {
  setHeading("Naming", "Review canonical filenames before changing bundles.");
  const params = new URLSearchParams({
    search: state.search,
    platform: state.platform,
    confidence: state.namingConfidence,
    limit: state.limit,
    offset: state.offset,
  });
  const [data, catalogs] = await Promise.all([api(`/api/naming/suggestions?${params}`), api("/api/naming/catalogs")]);
  const platformChoices = state.platforms.map((item) => `<option value="${escapeHtml(item.platform)}">${escapeHtml(item.platform)}</option>`).join("");
  const catalogList = catalogs.length
    ? `<div class="catalog-list">${catalogs.map((catalog) => `<span class="catalog-chip"><span><strong>${escapeHtml(catalog.platform)}</strong> · ${escapeHtml(catalog.name)} · ${catalog.entry_count.toLocaleString()}</span><button class="icon-button compact" data-delete-catalog="${catalog.id}" aria-label="Remove ${escapeHtml(catalog.name)}">×</button></span>`).join("")}</div>`
    : `<p class="meta">No DAT catalogs imported. Conservative filename cleanup is still available.</p>`;
  const importer = `<details class="naming-import">
    <summary>Import a DAT catalog</summary>
    <div class="import-body">
      <p>Use a No-Intro, Redump, or Logiqx XML DAT. Assign it to the matching ROMmates platform.</p>
      <form class="import-form" id="dat-import-form">
        <label class="field"><span>Platform</span><select name="platform" required><option value="">Choose platform</option>${platformChoices}</select></label>
        <label class="field file-field"><span>DAT file</span><input class="input" name="dat" type="file" accept=".dat,.xml,text/xml,application/xml" required></label>
        <button class="button" ${state.platforms.length ? "" : "disabled"}>Import catalog</button>
      </form>
      ${catalogList}
    </div>
  </details>`;
  const toolbar = `<div class="toolbar naming-toolbar">
    <label class="search-field"><span class="sr-only">Search suggestions</span><input id="search-input" type="search" value="${escapeHtml(state.search)}" placeholder="Search current or suggested names" autocomplete="off"></label>
    <label><span class="sr-only">Platform</span><select id="platform-filter">${platformOptions()}</select></label>
    <label><span class="sr-only">Confidence</span><select id="naming-confidence">
      <option value="all" ${state.namingConfidence === "all" ? "selected" : ""}>All suggestions</option>
      <option value="exact" ${state.namingConfidence === "exact" ? "selected" : ""}>Exact DAT matches</option>
      <option value="strong" ${state.namingConfidence === "strong" ? "selected" : ""}>Strong name matches</option>
      <option value="cleanup" ${state.namingConfidence === "cleanup" ? "selected" : ""}>Cleanup only</option>
    </select></label>
  </div>`;
  let content;
  if (!data.items.length) {
    content = `<div class="empty-state naming-empty"><div><h2>No naming suggestions</h2><p>${state.search || state.platform || state.namingConfidence !== "all" ? "Try broader filters." : "Your current filenames do not need conservative cleanup. Import a DAT catalog to find canonical matches."}</p></div></div>`;
  } else {
    const end = Math.min(data.offset + data.items.length, data.total);
    content = `<div class="table-wrap"><table><thead><tr><th class="checkbox-cell"><input type="checkbox" data-naming-select-all aria-label="Select visible safe suggestions"></th><th>Current filename</th><th>Suggested filename</th><th>Confidence</th><th>Source</th></tr></thead><tbody>${data.items.map((item) => {
      const checked = state.namingSelected.has(item.game_id);
      return `<tr class="${item.collision ? "collision-row" : ""}">
        <td class="checkbox-cell"><input type="checkbox" data-naming-select="${item.game_id}" ${checked ? "checked" : ""} ${item.collision ? "disabled" : ""} aria-label="Select suggestion for ${escapeHtml(item.current_name)}"></td>
        <td class="name-cell"><strong>${escapeHtml(item.current_name)}</strong><span class="path-line">${escapeHtml(item.primary_relpath)}</span></td>
        <td class="suggestion-cell"><input class="input suggestion-input" data-suggestion-name="${item.game_id}" value="${escapeHtml(state.namingSelected.get(item.game_id)?.name || item.suggested_name)}" maxlength="255" ${item.collision ? "disabled" : ""}>${item.collision ? `<span class="collision-note">${escapeHtml(item.collision_detail || "A file with this name already exists")}</span>` : ""}</td>
        <td><span class="badge naming-${item.confidence}">${escapeHtml(namingConfidenceLabel(item.confidence))}</span></td>
        <td class="meta">${escapeHtml(item.source)}</td>
      </tr>`;
    }).join("")}</tbody></table></div><div class="pager"><span>Showing ${data.offset + 1}–${end} of ${data.total.toLocaleString()}</span><div class="bulk-actions"><button class="button secondary small" data-page="previous" ${data.offset === 0 ? "disabled" : ""}>Previous</button><button class="button secondary small" data-page="next" ${end >= data.total ? "disabled" : ""}>Next</button></div></div>`;
  }
  const selectedCount = state.namingSelected.size;
  const bulk = selectedCount ? `<div class="bulk-bar"><div><strong>${selectedCount} selected</strong><span class="meta"> · bundle-aware rename</span></div><div class="bulk-actions"><button class="button secondary" data-clear-naming>Clear</button><button class="button" data-apply-naming>Review and apply</button></div></div>` : "";
  setViewHtml(`${importer}${toolbar}${content}${bulk}`);

  bindFilters(renderNaming);
  view.querySelector("#naming-confidence")?.addEventListener("change", (event) => { state.namingConfidence = event.target.value; state.offset = 0; renderNaming(); });
  view.querySelector("#dat-import-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const file = fields.get("dat");
    const button = form.querySelector("button");
    button.disabled = true;
    button.textContent = "Importing…";
    try {
      const result = await api("/api/naming/catalogs", { method: "POST", body: JSON.stringify({ source_name: file.name, platform: fields.get("platform"), content: await file.text() }) });
      toast(`Imported ${result.entries.toLocaleString()} names from ${result.name}`);
      await renderNaming();
    } catch (error) { toast(error.message, "error"); button.disabled = false; button.textContent = "Import catalog"; }
  });
  view.querySelectorAll("[data-delete-catalog]").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/api/naming/catalogs/${button.dataset.deleteCatalog}`, { method: "DELETE" }); await renderNaming(); }
    catch (error) { toast(error.message, "error"); }
  }));
  const byId = new Map(data.items.map((item) => [item.game_id, item]));
  const selectSuggestion = (id, selected) => {
    const item = byId.get(id);
    const input = view.querySelector(`[data-suggestion-name="${id}"]`);
    if (selected && item) state.namingSelected.set(id, { name: input?.value || item.suggested_name, current: item.current_name });
    else state.namingSelected.delete(id);
  };
  view.querySelectorAll("[data-naming-select]").forEach((box) => box.addEventListener("change", () => { selectSuggestion(Number(box.dataset.namingSelect), box.checked); renderNaming(); }));
  view.querySelectorAll("[data-suggestion-name]").forEach((input) => input.addEventListener("input", () => {
    const id = Number(input.dataset.suggestionName);
    if (state.namingSelected.has(id)) state.namingSelected.get(id).name = input.value;
  }));
  view.querySelector("[data-naming-select-all]")?.addEventListener("change", (event) => {
    data.items.filter((item) => !item.collision).forEach((item) => selectSuggestion(item.game_id, event.target.checked));
    renderNaming();
  });
  view.querySelector("[data-clear-naming]")?.addEventListener("click", () => { state.namingSelected.clear(); renderNaming(); });
  view.querySelector("[data-apply-naming]")?.addEventListener("click", async () => {
    const items = [...state.namingSelected.entries()].map(([game_id, item]) => ({ game_id, name: item.name }));
    const preview = items.slice(0, 12).map((item) => `<li>${escapeHtml(state.namingSelected.get(item.game_id).current)} → <strong>${escapeHtml(item.name)}</strong></li>`).join("");
    const confirmed = await confirmAction({ title: `Apply ${items.length} naming ${items.length === 1 ? "suggestion" : "suggestions"}?`, content: `<p class="warning-copy">ROMmates will rename complete file or folder bundles and update CUE, GDI, and M3U references. Existing device selections stay attached.</p><ul class="confirm-list">${preview}${items.length > 12 ? `<li>and ${items.length - 12} more</li>` : ""}</ul>`, confirmLabel: "Apply renames", cancelLabel: "Keep reviewing", danger: false });
    if (!confirmed) return;
    try {
      const result = await requestJob("/api/naming/apply", { method: "POST", body: JSON.stringify({ items }) }, "Naming changes queued");
      state.namingSelected.clear();
      toast(`Renamed ${result.renamed} ${result.renamed === 1 ? "bundle" : "bundles"}`);
      await refreshStatus(); await loadReferenceData(); await renderNaming();
    } catch (error) { toast(error.message, "error"); }
  });
  view.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => { state.offset = Math.max(0, state.offset + (button.dataset.page === "next" ? state.limit : -state.limit)); renderNaming(); }));
}

async function renderJobs() {
  setHeading("Jobs", "Detailed reports for scans and filesystem activity.");
  const [jobs, activity] = await Promise.all([api("/api/jobs"), api("/api/activity")]);
  if (state.jobReportId && !jobs.some((job) => job.id === state.jobReportId)) state.jobReportId = null;
  let report = null;
  let issues = null;
  if (state.jobReportId) {
    [report, issues] = await Promise.all([
      api(`/api/jobs/${state.jobReportId}`),
      api(`/api/jobs/${state.jobReportId}/issues?limit=250&offset=${state.jobIssueOffset}`),
    ]);
  }
  const jobsHtml = jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Detail</th><th>Started</th><th>Finished</th><th>Action</th></tr></thead><tbody>${jobs.map((job) => `<tr${state.jobReportId === job.id ? ` class="selected-row"` : ""}><td>${escapeHtml(job.kind)}</td><td><span class="badge ${job.status === "failed" ? "exact" : job.status === "complete" ? "unique" : job.status === "cancelled" ? "cancelled" : "possible"}">${escapeHtml(job.status)}${["running", "cancelling"].includes(job.status) ? ` · ${job.progress}%` : ""}</span></td><td class="name-cell">${escapeHtml(job.detail)}</td><td class="meta">${escapeHtml(job.created_at)}</td><td class="meta">${escapeHtml(job.completed_at || "In progress")}</td><td><div class="bulk-actions"><button class="button secondary small" data-job-report="${job.id}" aria-expanded="${state.jobReportId === job.id}">${state.jobReportId === job.id ? "Close" : "Report"}${job.reported_issue_count ? ` · ${job.reported_issue_count} issues` : ""}</button>${job.cancellable ? `<button class="button danger-subtle small" data-cancel-job="${job.id}" ${job.status === "cancelling" ? "disabled" : ""}>${job.status === "cancelling" ? "Stopping…" : "Stop"}</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty-state"><div><h2>No jobs yet</h2><p>Library scans will appear here.</p></div></div>`;
  const reportHtml = report ? renderJobReport(report, issues) : "";
  const activityHtml = activity.length ? `<div class="section-heading"><div><h2>Activity</h2><p>Rename, delete, restore, and deployment history.</p></div></div><div class="table-wrap"><table><thead><tr><th>Action</th><th>Detail</th><th>Time</th></tr></thead><tbody>${activity.map((item) => `<tr><td>${escapeHtml(item.action)}</td><td class="name-cell">${escapeHtml(item.detail)}</td><td class="meta">${escapeHtml(item.created_at)} UTC</td></tr>`).join("")}</tbody></table></div>` : "";
  setViewHtml(jobsHtml + reportHtml + activityHtml);
  view.querySelectorAll("[data-cancel-job]").forEach((button) => button.addEventListener("click", () => cancelJob(Number(button.dataset.cancelJob), button)));
  view.querySelectorAll("[data-job-report]").forEach((button) => button.addEventListener("click", () => {
    const jobId = Number(button.dataset.jobReport);
    state.jobReportId = state.jobReportId === jobId ? null : jobId;
    state.jobIssueOffset = 0;
    renderJobs();
  }));
  view.querySelectorAll("[data-issue-page]").forEach((button) => button.addEventListener("click", () => {
    state.jobIssueOffset = Math.max(0, state.jobIssueOffset + (button.dataset.issuePage === "next" ? 250 : -250));
    renderJobs();
  }));
  view.querySelector("[data-copy-issues]")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(issues.items.map((item) => item.detail).join("\n"));
      toast(`Copied ${issues.items.length} issue paths`);
    } catch {
      toast("The browser could not copy the issue list", "error");
    }
  });
}

function resultLabel(key) {
  return key.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function resultValue(value) {
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  if (typeof value === "number") return value.toLocaleString();
  if (value === null || value === undefined || value === "") return "None";
  return String(value);
}

function jobDuration(job) {
  if (!job.completed_at) return "In progress";
  const started = Date.parse(`${job.created_at}Z`);
  const finished = Date.parse(`${job.completed_at}Z`);
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return "—";
  const seconds = Math.max(0, Math.round((finished - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function renderJobReport(job, issues) {
  const resultEntries = job.result && typeof job.result === "object"
    ? Object.entries(job.result).filter(([key]) => key !== "skipped" && key !== "skipped_count")
    : [];
  const results = resultEntries.length
    ? `<dl class="report-grid">${resultEntries.map(([key, value]) => `<div><dt>${escapeHtml(resultLabel(key))}</dt><dd>${escapeHtml(resultValue(value))}</dd></div>`).join("")}</dl>`
    : `<p class="report-empty">No structured result was recorded for this job.</p>`;
  const captureWarning = issues && !issues.captured_all
    ? `<p class="issue-warning" role="note">This older scan reported <strong>${issues.reported_total.toLocaleString()}</strong> unreadable files, but retained details for only <strong>${issues.total.toLocaleString()}</strong>. Run a new scan to capture the complete list; unchanged ROM hashes will be reused.</p>`
    : "";
  const issueRows = issues?.items.length
    ? `<div class="table-wrap issue-table"><table><thead><tr><th>Unreadable file and reason</th></tr></thead><tbody>${issues.items.map((item) => `<tr><td><code class="issue-path">${escapeHtml(item.detail)}</code></td></tr>`).join("")}</tbody></table></div><div class="pager"><span>Showing ${(issues.offset + 1).toLocaleString()}–${Math.min(issues.offset + issues.items.length, issues.total).toLocaleString()} of ${issues.total.toLocaleString()} captured</span><div class="bulk-actions"><button class="button secondary small" data-copy-issues>Copy page</button><button class="button secondary small" data-issue-page="previous" ${issues.offset === 0 ? "disabled" : ""}>Previous</button><button class="button secondary small" data-issue-page="next" ${issues.offset + issues.limit >= issues.total ? "disabled" : ""}>Next</button></div></div>`
    : `<p class="report-empty">No unreadable files were recorded for this job.</p>`;
  const scanIssues = job.kind === "scan"
    ? `<div class="report-section"><div class="report-section-head"><div><h3>Unreadable files</h3><p>${issues.reported_total ? `${issues.reported_total.toLocaleString()} reported by this scan.` : "Paths and reasons captured during scanning."}</p></div></div>${captureWarning}${issueRows}</div>`
    : "";
  return `<section class="job-report" aria-labelledby="job-report-title"><div class="section-heading report-heading"><div><h2 id="job-report-title">Job #${job.id} report</h2><p>${escapeHtml(job.detail)}</p></div><span class="badge ${job.status === "failed" ? "exact" : job.status === "complete" ? "unique" : job.status === "cancelled" ? "cancelled" : "possible"}">${escapeHtml(job.status)}</span></div><dl class="report-grid report-summary"><div><dt>Job type</dt><dd>${escapeHtml(job.kind)}</dd></div><div><dt>Started</dt><dd>${escapeHtml(job.created_at)} UTC</dd></div><div><dt>Finished</dt><dd>${escapeHtml(job.completed_at || "In progress")}${job.completed_at ? " UTC" : ""}</dd></div><div><dt>Duration</dt><dd>${escapeHtml(jobDuration(job))}</dd></div><div><dt>Progress</dt><dd>${job.progress}%</dd></div></dl><div class="report-section"><h3>Result</h3>${results}</div>${scanIssues}</section>`;
}

async function cancelJob(jobId, button = stopJobButton) {
  if (!jobId) return;
  button.disabled = true;
  button.textContent = "Stopping…";
  try {
    const result = await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    toast(result.already_finished ? "Job already finished" : "Stop requested. Finishing the current file safely.");
    await refreshStatus();
    if (state.view === "jobs") await renderJobs();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Stop";
    toast(error.message, "error");
  }
}

function confirmAction({ title, content, confirmLabel, cancelLabel = "Cancel", danger }) {
  dialogTitle.textContent = title;
  dialogContent.innerHTML = content;
  dialogConfirm.textContent = confirmLabel;
  dialogCancel.textContent = cancelLabel;
  dialogConfirm.className = `button ${danger ? "danger" : ""}`;
  dialog.showModal();
  return new Promise((resolve) => {
    const close = () => { dialog.removeEventListener("close", close); resolve(dialog.returnValue === "confirm"); };
    dialog.addEventListener("close", close);
  });
}

async function startScan(confirmPrune = false) {
  let response;
  try {
    response = await api(`/api/scan${confirmPrune ? "?confirm_prune=true" : ""}`, { method: "POST" });
    toast(confirmPrune ? "Rescanning and removing missing games" : "Library scan started");
    await refreshStatus();
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  if (response.already_running) return;
  // Await the job here rather than relying on the status poller: a scan that finishes
  // before the first poll would otherwise report nothing at all, success or failure.
  let outcome;
  try {
    await waitForJob(response.job_id);
    outcome = { status: "complete" };
  } catch (error) {
    outcome = { status: error.cancelled ? "cancelled" : "failed", detail: error.message };
  }
  await reportJobOutcome({ id: response.job_id, kind: "scan", ...outcome });
  await refreshStatus();
  await loadReferenceData();
  await renderCurrentView();
}

// Both the status poller and a directly awaited job can observe the same completion.
// Report each job once so a failure cannot raise two dialogs or two toasts.
async function reportJobOutcome(job) {
  if (!job || state.reportedJobs.has(job.id)) return;
  state.reportedJobs.add(job.id);
  if (state.reportedJobs.size > 200) {
    state.reportedJobs = new Set([...state.reportedJobs].slice(-100));
  }
  if (job.status === "complete") {
    if (job.detail) toast(job.detail);
    return;
  }
  if (job.status === "cancelled") {
    toast("Job stopped safely");
    return;
  }
  if (isPruneGuardFailure(job.detail)) {
    await offerPruneConfirmation(job.detail);
    return;
  }
  const label = (JOB_LABELS[job.kind] || "Job").replace(/[….]+$/, "");
  toast(`${label} failed: ${job.detail}`, "error");
}

function isPruneGuardFailure(detail) {
  return typeof detail === "string" && detail.startsWith("Scan would remove ");
}

// The server refuses a scan that would delete most of the catalog, because that
// usually means the library volume is missing rather than the ROMs. Surface the
// reason and require an explicit confirmation before pruning for real.
async function offerPruneConfirmation(detail) {
  const confirmed = await confirmAction({
    title: "Scan stopped to protect your catalog",
    content: `<p class="warning-copy">${escapeHtml(detail)}</p><p>Only confirm if you deliberately removed those files. Confirming deletes the catalog entries and every device selection that depends on them.</p>`,
    confirmLabel: "Confirm removal and rescan",
    cancelLabel: "Keep catalog unchanged",
    danger: true,
  });
  if (confirmed) await startScan(true);
}

async function renderCurrentView() {
  view.setAttribute("aria-busy", "true");
  try {
    const renderers = { library: renderLibrary, duplicates: renderDuplicates, naming: renderNaming, devices: renderDevices, saves: renderSaves, jobs: renderJobs, trash: renderTrash };
    await renderers[state.view]();
  } catch (error) {
    if (error.name === "AbortError") return;
    if (error.status === 401) {
      renderAuthentication();
      return;
    }
    setViewHtml(`<div class="empty-state"><div><h2>This view could not load</h2><p>${escapeHtml(error.message)}</p><button class="button secondary" data-retry>Try again</button></div></div>`);
    view.querySelector("[data-retry]")?.addEventListener("click", renderCurrentView);
  } finally { view.removeAttribute("aria-busy"); }
}

function renderAuthentication() {
  setHeading("Private access", "Enter the token configured on your ROMmates server.");
  setViewHtml(`<div class="auth-panel"><h2>Access token required</h2><p>The token stays in this browser and is sent only to this ROMmates server.</p><form class="auth-form" id="auth-form"><label class="field" for="access-token"><span>Access token</span><input class="input" id="access-token" name="token" type="password" autocomplete="current-password" required minlength="16"></label><button class="button">Unlock ROMmates</button></form></div>`);
  document.querySelector("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = new FormData(event.currentTarget).get("token").trim();
    localStorage.setItem("rommates-token", token);
    try {
      await refreshStatus();
      await loadReferenceData();
      await renderCurrentView();
    } catch (error) {
      if (error.status === 401) {
        localStorage.removeItem("rommates-token");
        localStorage.removeItem("rom-manager-token");
      }
      toast(error.status === 401 ? "That access token was not accepted" : error.message, "error");
    }
  });
  document.querySelector("#access-token").focus();
}

document.querySelector("#navigation").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  state.view = button.dataset.view;
  state.offset = 0;
  state.search = "";
  state.platform = "";
  state.duplicate = state.view === "duplicates" ? "exact" : "all";
  state.selectedRows.clear();
  state.editingId = null;
  state.assigningId = null;
  state.assignmentDevices = [];
  if (state.view !== "naming") state.namingSelected.clear();
  if (state.view !== "saves") state.saveSnapshotId = null;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
  renderCurrentView();
});

scanButton.addEventListener("click", () => startScan());
stopJobButton.addEventListener("click", () => cancelJob(Number(stopJobButton.dataset.jobId), stopJobButton));
refreshButton.addEventListener("click", async () => {
  try { await refreshStatus(); await loadReferenceData(); await renderCurrentView(); }
  catch (error) { toast(error.message, "error"); }
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    document.querySelector("#search-input")?.focus();
  }
});

async function initialize() {
  try {
    await refreshStatus();
    await loadReferenceData();
    await renderCurrentView();
  } catch (error) {
    if (error.status === 401) {
      renderAuthentication();
      return;
    }
    setViewHtml(`<div class="empty-state"><div><h2>ROMmates could not start</h2><p>${escapeHtml(error.message)}</p></div></div>`);
  }
}

initialize();
