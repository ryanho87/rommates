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
  editingId: null,
  assigningId: null,
  assignmentDevices: [],
  deviceId: null,
  refreshTimer: null,
  gamesController: null,
  // Job ids already surfaced to the user, so the poller and an awaited job do not
  // both report the same outcome.
  reportedJobs: new Set(),
};

const view = document.querySelector("#view");
const pageTitle = document.querySelector("#page-title");
const pageSubtitle = document.querySelector("#page-subtitle");
const scanButton = document.querySelector("#scan-button");
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

async function api(path, options = {}) {
  const legacyToken = localStorage.getItem("rom-manager-token");
  const token = localStorage.getItem("rommates-token") || legacyToken;
  if (legacyToken && !localStorage.getItem("rommates-token")) {
    localStorage.setItem("rommates-token", legacyToken);
    localStorage.removeItem("rom-manager-token");
  }
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

const JOB_LABELS = {
  scan: "Scanning changed files…",
  rename: "Renaming bundle…",
  delete: "Moving to trash…",
  device_apply: "Applying device changes…",
  restore: "Restoring from trash…",
  purge: "Deleting permanently…",
};

const JOB_POLL_INTERVAL = 700;
const JOB_POLL_TIMEOUT = 30 * 60 * 1000;

async function waitForJob(jobId) {
  const deadline = Date.now() + JOB_POLL_TIMEOUT;
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "complete") return job.result || {};
    if (job.status === "failed") throw new Error(job.detail || "The background job failed");
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
  document.querySelector("#library-root").textContent = state.status.roots.library;
  const pill = document.querySelector("#job-pill");
  const job = state.status.job;
  const running = job && ["queued", "running"].includes(job.status);
  const scanning = running && job.kind === "scan";
  pill.classList.toggle("hidden", !running);
  pill.textContent = running ? JOB_LABELS[job.kind] || "Working…" : "";
  // Only a scan conflicts with starting another scan; other jobs leave the button usable.
  scanButton.disabled = scanning;
  scanButton.textContent = scanning ? "Scanning…" : "Scan library";
  if (running) scheduleStatusRefresh();
}

function scheduleStatusRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(async () => {
    try {
      const wasRunning = state.status?.job && ["queued", "running"].includes(state.status.job.status);
      await refreshStatus();
      const isRunning = state.status?.job && ["queued", "running"].includes(state.status.job.status);
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

async function getGames(deviceId = null) {
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
  return api(`/api/games?${params}`, { signal: state.gamesController.signal });
}

function libraryToolbar(includeDuplicate = true) {
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
      ${includeDuplicate ? `<label><span class="sr-only">Duplicate status</span><select id="duplicate-filter">
        <option value="all" ${state.duplicate === "all" ? "selected" : ""}>All statuses</option>
        <option value="exact" ${state.duplicate === "exact" ? "selected" : ""}>Exact duplicates</option>
        <option value="possible" ${state.duplicate === "possible" ? "selected" : ""}>Possible duplicates</option>
        <option value="unique" ${state.duplicate === "unique" ? "selected" : ""}>Unique</option>
      </select></label>` : ""}
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
        <td>${duplicateLabel(game.duplicate_status)}</td>
        <td class="meta">${formatBytes(game.size)}</td>
        <td class="meta optional-column">${game.file_count} ${game.file_count === 1 ? "file" : "files"}</td>
        <td class="meta optional-column">${deviceSummary(game, !deviceMode)}</td>
        ${deviceMode ? "" : `<td class="nowrap">
          <button class="button secondary small" data-rename="${game.id}" ${deviceMode ? "disabled" : ""}>Rename</button>
          <button class="button danger-subtle small" data-delete="${game.id}" data-name="${escapeHtml(game.display_name)}" ${deviceMode ? "disabled" : ""}>Trash</button>
        </td>`}
      </tr>${editor}${assignment}`;
  }).join("");
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
          <th>Filename</th><th>Platform</th><th>Duplicate status</th><th>Size</th><th class="optional-column">Bundle</th><th class="optional-column">Devices</th>${deviceMode ? "" : "<th>Actions</th>"}
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
  const data = await getGames();
  setViewHtml(`${libraryToolbar(true)}<div class="section-heading"><div><h2>${state.duplicate === "possible" ? "Possible duplicates" : "Exact duplicates"}</h2><p>${state.duplicate === "possible" ? "Names normalize to the same title within a platform. Compare before deleting." : "Bundle content hashes match exactly."}</p></div></div>${gamesTable(data)}<div id="bulk-bar-slot"></div>`);
  renderBulkBar();
  syncSelectAll();
  bindFilters(renderDuplicates);
  bindGameEvents(data, false);
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
      const bundleNames = detail.files.map((item) => item.relpath.split("/").pop()).join(", ");
      const confirmed = await confirmAction({
        title: "Rename this bundle?",
        content: `<p class="warning-copy">The primary file and prefix-matching companions will be renamed. References inside CUE and M3U files will be updated.</p><p><strong>${escapeHtml(bundleNames)}</strong></p>`,
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
  setHeading("Devices", "Choose the desired library for each Syncthing target.");
  if (!state.devices.length) {
    setViewHtml(`<div class="empty-state"><div><h2>No device folders found</h2><p>Create a directory such as <code>/devices/retroid/roms</code>, then scan the library. Device folders are discovered automatically.</p><button class="button" data-scan>Scan again</button></div></div>`);
    view.querySelector("[data-scan]")?.addEventListener("click", () => startScan());
    return;
  }
  const device = state.devices.find((item) => item.id === Number(state.deviceId)) || state.devices[0];
  state.deviceId = device.id;
  const [data, preview] = await Promise.all([getGames(device.id), api(`/api/devices/${device.id}/preview`)]);
  setViewHtml(`
    <div class="device-strip">
      <label class="field"><span>Target device</span><select id="device-select">${state.devices.map((item) => `<option value="${item.id}" ${item.id === device.id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></label>
      <div class="device-summary"><span><strong>${preview.games}</strong> games selected</span><span><strong>${preview.additions}</strong> files to add</span><span><strong>${preview.removals}</strong> files to remove</span><button class="button" id="apply-device" ${preview.additions === 0 && preview.removals === 0 ? "disabled" : ""}>Review and apply</button></div>
    </div>
    ${libraryToolbar(false)}${gamesTable(data, true)}`);
  bindFilters(renderDevices);
  document.querySelector("#device-select").addEventListener("change", (event) => { state.deviceId = Number(event.target.value); renderDevices(); });
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

async function renderJobs() {
  setHeading("Jobs", "Recent scans and filesystem activity.");
  const [jobs, activity] = await Promise.all([api("/api/jobs"), api("/api/activity")]);
  const jobsHtml = jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Detail</th><th>Started</th><th>Finished</th></tr></thead><tbody>${jobs.map((job) => `<tr><td>${escapeHtml(job.kind)}</td><td><span class="badge ${job.status === "failed" ? "exact" : job.status === "complete" ? "unique" : "possible"}">${escapeHtml(job.status)}</span></td><td class="name-cell">${escapeHtml(job.detail)}</td><td class="meta">${escapeHtml(job.created_at)}</td><td class="meta">${escapeHtml(job.completed_at || "In progress")}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty-state"><div><h2>No jobs yet</h2><p>Library scans will appear here.</p></div></div>`;
  const activityHtml = activity.length ? `<div class="section-heading"><div><h2>Activity</h2><p>Rename, delete, restore, and deployment history.</p></div></div><div class="table-wrap"><table><thead><tr><th>Action</th><th>Detail</th><th>Time</th></tr></thead><tbody>${activity.map((item) => `<tr><td>${escapeHtml(item.action)}</td><td class="name-cell">${escapeHtml(item.detail)}</td><td class="meta">${escapeHtml(item.created_at)} UTC</td></tr>`).join("")}</tbody></table></div>` : "";
  setViewHtml(jobsHtml + activityHtml);
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
    outcome = { status: "failed", detail: error.message };
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
    const renderers = { library: renderLibrary, duplicates: renderDuplicates, devices: renderDevices, jobs: renderJobs, trash: renderTrash };
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
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
  renderCurrentView();
});

scanButton.addEventListener("click", () => startScan());
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
