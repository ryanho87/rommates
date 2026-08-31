const VIEW_ROUTES = Object.freeze({
  overview: "/",
  library: "/library",
  artwork: "/artwork",
  transfers: "/transfers",
  duplicates: "/duplicates",
  naming: "/naming",
  devices: "/devices",
  saves: "/saves",
  jobs: "/jobs",
  notifications: "/notifications",
  users: "/users",
  trash: "/trash",
});

const ROUTE_VIEWS = new Map(Object.entries(VIEW_ROUTES).map(([viewName, path]) => [path, viewName]));

function normalizedRoute(pathname = window.location.pathname) {
  const route = pathname.replace(/\/+$/, "");
  return route || "/";
}

function viewFromLocation() {
  return ROUTE_VIEWS.get(normalizedRoute()) || "overview";
}

const state = {
  view: viewFromLocation(),
  status: null,
  platforms: [],
  devices: [],
  search: "",
  platform: "",
  duplicate: "all",
  sort: "name_asc",
  rankingOpen: false,
  offset: 0,
  limit: 100,
  // id -> {display_name, platform}. A map rather than a set so the bulk bar and the
  // confirmation dialog can name selections that current filters have scrolled away.
  selectedRows: new Map(),
  // kind + group key -> reviewed group and chosen keeper. Decisions persist across
  // duplicate pages so hundreds of groups can be committed in one recoverable job.
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
  namingSaveImpact: "all",
  namingSelected: new Map(),
  jobReportId: null,
  jobIssueOffset: 0,
  saveTab: "current",
  saveSearch: "",
  saveOffset: 0,
  saveMatchSearch: "",
  saveMatchStatus: "all",
  saveMatchOffset: 0,
  saveConflictSearch: "",
  saveConflictOffset: 0,
  saveSnapshotId: null,
  saveSnapshotOffset: 0,
  saveSnapshotSearch: "",
  artworkUrls: [],
  artworkId: null,
  artworkDetail: null,
  artworkObserver: null,
  artworkScope: "library",
  artworkAssetMode: "cover",
  artworkPlatforms: new Set(),
  artworkGames: new Map(),
  artworkSearch: "",
  artworkPlatform: "",
  renderVersion: 0,
  navigationCache: new Map(),
  prefetchStarted: false,
  navigationLoadingTimer: null,
  infinitePages: new Map(),
  infiniteObserver: null,
  uploadSelection: null,
  uploadSessions: [],
  uploadPlatform: "",
  uploadProgress: null,
  trashSelected: new Map(),
  principal: null,
  permissions: { admin: false, upload: false, download: false },
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
const logoutButton = document.querySelector("#logout-button");
const changePasswordButton = document.querySelector("#change-password-button");
const mobileMenuButton = document.querySelector("#mobile-menu-button");
const navBackdrop = document.querySelector("#nav-backdrop");
const sidebarCloseButton = document.querySelector("#sidebar-close-button");

function isAdmin() { return state.permissions.admin; }
function canUpload() { return state.permissions.upload; }

function allowedViews() {
  if (state.principal?.must_change_password) return new Set();
  if (isAdmin()) return new Set(Object.keys(VIEW_ROUTES));
  return new Set(["library", ...(canUpload() ? ["transfers"] : [])]);
}

function setMobileNavigation(open) {
  open = Boolean(open && window.matchMedia("(max-width: 720px)").matches);
  document.body.classList.toggle("nav-open", open);
  mobileMenuButton.setAttribute("aria-expanded", String(open));
  mobileMenuButton.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  navBackdrop.setAttribute("aria-hidden", String(!open));
  document.querySelector("#main-content").inert = open;
  if (open) window.requestAnimationFrame(() => sidebarCloseButton.focus());
}

function applyRoleNavigation() {
  const allowed = allowedViews();
  document.querySelectorAll("[data-view]").forEach((item) => {
    item.classList.toggle("hidden", !allowed.has(item.dataset.view));
  });
  scanButton.classList.toggle("hidden", !isAdmin());
  document.querySelector(".root-state")?.classList.toggle("hidden", !isAdmin());
  const account = document.querySelector("#account-state");
  account?.classList.remove("hidden");
  document.querySelector("#account-name").textContent = state.principal?.display_name || "ROMmates user";
  document.querySelector("#account-role").textContent = state.principal?.bootstrap ? "Bootstrap admin" : state.principal?.role || "";
  changePasswordButton.classList.toggle("hidden", Boolean(state.principal?.bootstrap));
}

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
  clearTimeout(state.navigationLoadingTimer);
  state.infiniteObserver?.disconnect();
  const snapshot = captureFocus();
  view.innerHTML = html;
  restoreFocus(snapshot);
}

function mergeInfinitePage(key, data, identity = (item) => item.id) {
  let page = state.infinitePages.get(key);
  if (!page || data.offset === 0) {
    const namespace = key.split("\u001f", 1)[0];
    for (const existingKey of state.infinitePages.keys()) {
      if (existingKey !== key && existingKey.split("\u001f", 1)[0] === namespace) {
        state.infinitePages.delete(existingKey);
      }
    }
    page = { items: [], total: data.total };
    state.infinitePages.set(key, page);
  }
  if (data.offset === 0) {
    page.items = [...data.items];
  } else {
    const replaceCount = Math.min(
      data.limit || data.items.length,
      Math.max(0, page.items.length - data.offset),
    );
    page.items.splice(data.offset, replaceCount, ...data.items);
    const seen = new Set();
    page.items = page.items.filter((item) => {
      const itemIdentity = identity(item);
      if (seen.has(itemIdentity)) return false;
      seen.add(itemIdentity);
      return true;
    });
    if (page.items.length > data.total) {
      page.items.length = data.total;
    }
  }
  page.total = data.total;
  return { ...data, items: page.items, offset: 0 };
}

function infiniteFooter(data, noun = "items") {
  const loaded = data.items.length;
  const more = loaded < data.total;
  return `<div class="infinite-footer" ${more ? "data-infinite-sentinel" : ""}>
    <span>${loaded.toLocaleString()} of ${data.total.toLocaleString()} ${escapeHtml(noun)}</span>
    <span class="infinite-state">${more ? "Loading more…" : data.total ? "All loaded" : ""}</span>
  </div>`;
}

function bindInfiniteScroll(data, callback, offsetSetter = (offset) => { state.offset = offset; }) {
  const sentinel = view.querySelector("[data-infinite-sentinel]");
  if (!sentinel || data.items.length >= data.total) return;
  let loading = false;
  const load = () => {
    if (loading) return;
    loading = true;
    state.infiniteObserver?.disconnect();
    offsetSetter(data.items.length);
    Promise.resolve(callback()).catch((error) => {
      loading = false;
      const status = sentinel.querySelector(".infinite-state");
      if (status) status.textContent = "Could not load more. Scroll away and back to retry.";
      toast(error.message, "error");
      if (sentinel.isConnected) state.infiniteObserver?.observe(sentinel);
    });
  };
  if (!("IntersectionObserver" in window)) {
    const fallback = document.createElement("button");
    fallback.className = "button secondary small";
    fallback.textContent = "Load more";
    fallback.addEventListener("click", load);
    sentinel.append(fallback);
    return;
  }
  state.infiniteObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) load();
  }, { rootMargin: "600px 0px" });
  state.infiniteObserver.observe(sentinel);
}

function beginPageRender() {
  state.renderVersion += 1;
  return state.renderVersion;
}

function pageRenderIsCurrent(renderVersion, expectedView) {
  return renderVersion === state.renderVersion && state.view === expectedView;
}

function scheduleNavigationLoading(renderVersion) {
  clearTimeout(state.navigationLoadingTimer);
  state.navigationLoadingTimer = setTimeout(() => {
    if (!pageRenderIsCurrent(renderVersion, state.view)) return;
    setViewHtml(`<div class="navigation-loading" role="status" aria-label="Loading page">
      <span></span><span></span><span></span><span></span>
    </div>`);
  }, 120);
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

const NAVIGATION_CACHE_TTL = 20_000;
const INFINITE_CHUNK_SIZE = 250;

function clearNavigationCache() {
  state.navigationCache.clear();
}

async function api(path, options = {}) {
  const { cacheTtl = 0, ...fetchOptions } = options;
  const token = storedAccessToken();
  const method = String(fetchOptions.method || "GET").toUpperCase();
  const cacheKey = method === "GET" && cacheTtl ? path : null;
  const cached = cacheKey ? state.navigationCache.get(cacheKey) : null;
  if (cached && cached.expiresAt > Date.now()) return cached.data;
  if (cached?.promise) return cached.promise;

  const request = (async () => {
    const response = await fetch(path, {
      ...fetchOptions,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(fetchOptions.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const error = new Error(body?.detail || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    if (method !== "GET") clearNavigationCache();
    return body;
  })();

  if (cacheKey) state.navigationCache.set(cacheKey, { promise: request, expiresAt: 0 });
  try {
    const data = await request;
    if (cacheKey) state.navigationCache.set(cacheKey, { data, expiresAt: Date.now() + cacheTtl });
    return data;
  } catch (error) {
    if (cacheKey && state.navigationCache.get(cacheKey)?.promise === request) {
      state.navigationCache.delete(cacheKey);
    }
    throw error;
  }
}

function navigationApi(path, options = {}) {
  return api(path, { cacheTtl: NAVIGATION_CACHE_TTL, ...options });
}

function prefetchNavigationData() {
  if (state.prefetchStarted) return;
  state.prefetchStarted = true;
  const paths = isAdmin() ? [
    `/api/games?${new URLSearchParams({ search: "", platform: "", duplicate: "all", limit: state.limit, offset: 0 })}`,
    `/api/duplicates?${new URLSearchParams({ kind: "exact", search: "", platform: "", limit: 30, offset: 0 })}`,
    `/api/naming/suggestions?${new URLSearchParams({ search: "", platform: "", confidence: "all", save_impact: "all", limit: state.limit, offset: 0 })}`,
    "/api/naming/catalogs",
    "/api/saves",
    `/api/saves/current?${new URLSearchParams({ search: "", limit: 250, offset: 0 })}`,
    "/api/jobs",
    "/api/activity",
    "/api/trash",
  ] : [
    `/api/games?${new URLSearchParams({ search: "", platform: "", duplicate: "all", limit: state.limit, offset: 0 })}`,
  ];
  // Device views intentionally are not prefetched. Reconciling actual device
  // files and storage relationships is useful when opened, but should never
  // compete with Library navigation for mergerfs I/O.
  const warm = async () => {
    for (const path of paths) {
      try { await navigationApi(path); } catch { /* Prefetch must never affect the active page. */ }
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  };
  if ("requestIdleCallback" in window) {
    window.requestIdleCallback(warm, { timeout: 1500 });
  } else {
    setTimeout(warm, 300);
  }
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
  bulk_delete: "Moving duplicate bundles to trash…",
  delete: "Moving to trash…",
  device_apply: "Applying device changes…",
  restore: "Restoring from trash…",
  purge: "Deleting permanently…",
  bulk_purge: "Deleting selected trash…",
  upload_finalize: "Adding uploaded ROM…",
  save_snapshot: "Snapshotting saves…",
  save_restore: "Restoring saves…",
  save_delete: "Deleting orphan saves…",
  artwork_scrape: "Scraping artwork…",
  artwork_bulk: "Downloading library artwork…",
};

const ACTIVE_JOB_STATUSES = ["queued", "running", "paused", "cancelling"];
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
  state.principal = state.status.user;
  state.permissions = {
    admin: state.principal?.role === "admin" && !state.principal?.must_change_password,
    upload: ["admin", "contributor"].includes(state.principal?.role) && !state.principal?.must_change_password,
    download: !state.principal?.must_change_password,
  };
  applyRoleNavigation();
  document.querySelector("#nav-games").textContent = state.status.games.toLocaleString();
  document.querySelector("#nav-duplicates").textContent = state.status.duplicates.toLocaleString();
  document.querySelector("#nav-devices").textContent = state.status.devices.toLocaleString();
  document.querySelector("#nav-trash").textContent = state.status.trash.toLocaleString();
  document.querySelector("#nav-saves").textContent = state.status.save_snapshots.toLocaleString();
  document.querySelector("#library-root").textContent = state.status.roots.library;
  const pill = document.querySelector("#job-pill");
  const job = state.status.job;
  const running = job && ACTIVE_JOB_STATUSES.includes(job.status);
  const scanning = running && job.kind === "scan";
  pill.classList.toggle("hidden", !running);
  pill.textContent = running
    ? job.status === "paused" ? job.detail : scanning ? `${job.progress}% · ${job.detail}` : JOB_LABELS[job.kind] || "Working…"
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
      const wasRunning = state.status?.job && ACTIVE_JOB_STATUSES.includes(state.status.job.status);
      await refreshStatus();
      const isRunning = state.status?.job && ACTIVE_JOB_STATUSES.includes(state.status.job.status);
      if (isRunning && state.view === "jobs") await renderJobs();
      if (isRunning && state.view === "artwork" && state.status.job.kind === "artwork_bulk") await renderArtwork();
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
  [state.platforms, state.devices] = await Promise.all([
    api("/api/platforms"),
    isAdmin() ? api("/api/devices") : Promise.resolve([]),
  ]);
  if (!state.deviceId && state.devices.length) state.deviceId = state.devices[0].id;
}

function setHeading(title, subtitle) {
  pageTitle.textContent = title;
  pageSubtitle.textContent = subtitle;
  document.title = title === "Overview" ? "ROMmates" : `${title} · ROMmates`;
}

function platformOptions(items = state.platforms, countSuffix = "", selectedValue = state.platform) {
  return `<option value="">All platforms</option>${items.map((item) => `<option value="${escapeHtml(item.platform)}" ${selectedValue === item.platform ? "selected" : ""}>${escapeHtml(item.platform)} (${Number(item.count).toLocaleString()}${escapeHtml(countSuffix)})</option>`).join("")}`;
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
    sort: state.sort,
    limit: state.offset ? INFINITE_CHUNK_SIZE : state.limit,
    offset: state.offset,
  });
  if (deviceId) params.set("device_id", deviceId);
  if (deviceId) params.set("device_scope", deviceScope);
  return navigationApi(`/api/games?${params}`, { signal: state.gamesController.signal });
}

function libraryToolbar(includeDuplicate = true, platformItems = state.platforms, countSuffix = "") {
  const duplicateOptions = state.view === "duplicates"
    ? `<option value="exact" ${state.duplicate === "exact" ? "selected" : ""}>Exact content</option><option value="possible" ${state.duplicate === "possible" ? "selected" : ""}>Similar filenames</option>`
    : `<option value="all" ${state.duplicate === "all" ? "selected" : ""}>All statuses</option><option value="exact" ${state.duplicate === "exact" ? "selected" : ""}>Exact duplicates</option><option value="possible" ${state.duplicate === "possible" ? "selected" : ""}>Possible duplicates</option><option value="unique" ${state.duplicate === "unique" ? "selected" : ""}>Unique</option>`;
  const selectedPlatform = state.platforms.find((item) => item.platform === state.platform);
  const missingRatings = selectedPlatform
    ? Math.max(0, Number(selectedPlatform.count) - Number(selectedPlatform.rated_count || 0))
    : 0;
  const ratingAction = state.view !== "duplicates" && isAdmin()
    ? `<button class="button secondary" type="button" data-fetch-ratings ${!selectedPlatform || missingRatings === 0 ? "disabled" : ""}>${selectedPlatform ? missingRatings ? `Fetch ${missingRatings.toLocaleString()} missing ratings` : "Ratings complete" : "Choose a platform for ratings"}</button>`
    : "";
  const rankingAction = state.view === "library"
    ? `<button class="button secondary ${state.rankingOpen ? "active" : ""}" type="button" data-toggle-ranking ${selectedPlatform ? "" : "disabled"}>Top 100 coverage</button>`
    : "";
  const auxiliaryTools = ratingAction || rankingAction
    ? `<details class="library-aux-tools"><summary>Ratings and rankings</summary><div>${ratingAction}${rankingAction}</div></details>`
    : "";
  return `
    <div class="toolbar library-toolbar">
      <label class="search-field">
        <span class="sr-only">Search ROMs</span>
        <input id="search-input" type="search" value="${escapeHtml(state.search)}" placeholder="Search ROMs" autocomplete="off">
      </label>
      <label>
        <span class="sr-only">Platform</span>
        <select id="platform-filter">${platformOptions(platformItems, countSuffix)}</select>
      </label>
      ${includeDuplicate ? `<label><span class="sr-only">Duplicate status</span><select id="duplicate-filter">${duplicateOptions}</select></label>` : ""}
      ${state.view !== "duplicates" ? `<label><span class="sr-only">Sort games</span><select id="sort-filter">
        <option value="name_asc" ${state.sort === "name_asc" ? "selected" : ""}>Title A–Z</option>
        <option value="name_desc" ${state.sort === "name_desc" ? "selected" : ""}>Title Z–A</option>
        <option value="rating_desc" ${state.sort === "rating_desc" ? "selected" : ""}>Best rated</option>
        <option value="rating_asc" ${state.sort === "rating_asc" ? "selected" : ""}>Lowest rated</option>
        <option value="size_desc" ${state.sort === "size_desc" ? "selected" : ""}>Largest size</option>
        <option value="size_asc" ${state.sort === "size_asc" ? "selected" : ""}>Smallest size</option>
      </select></label>` : ""}
      ${auxiliaryTools}
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
  document.querySelector("#sort-filter")?.addEventListener("change", (event) => {
    state.sort = event.target.value;
    state.offset = 0;
    callback();
  });
  document.querySelector("[data-fetch-ratings]")?.addEventListener("click", fetchMissingRatings);
  document.querySelector("[data-toggle-ranking]")?.addEventListener("click", () => {
    state.rankingOpen = !state.rankingOpen;
    callback();
  });
}

function uploadPanel() {
  const selection = state.uploadSelection;
  const sessions = state.uploadSessions;
  const fileSummary = selection
    ? `<div class="upload-selection"><strong>${escapeHtml(selection.bundleName || selection.files[0]?.name || "Selected ROM")}</strong><span>${selection.files.length.toLocaleString()} ${selection.files.length === 1 ? "file" : "files"} · ${formatBytes(selection.files.reduce((sum, file) => sum + file.size, 0))}</span></div>`
    : `<p class="meta">Choose one ROM file, a related multi-file set, or an entire game folder. Archives are stored as-is and are never extracted.</p>`;
  const progress = state.uploadProgress
    ? `<div class="upload-progress"><div><strong>${escapeHtml(state.uploadProgress.label)}</strong><span>${state.uploadProgress.percent}%</span></div><progress max="100" value="${state.uploadProgress.percent}"></progress></div>`
    : "";
  const active = sessions.length
    ? `<div class="upload-sessions"><strong>${isAdmin() ? "Uploads and review queue" : "Your uploads"}</strong>${sessions.map((session) => `<div><span><strong>${escapeHtml(session.bundle_name || session.files?.[0]?.relative_path || session.id)}</strong>${isAdmin() && session.owner_display_name ? ` · submitted by ${escapeHtml(session.owner_display_name)}` : ""} · ${formatBytes(session.received_size)} of ${formatBytes(session.total_size)} · <span class="badge ${session.status === "pending_review" ? "possible" : session.status === "rejected" ? "exact" : session.status === "finalizing" ? "naming-strong" : "cancelled"}">${escapeHtml(session.status.replaceAll("_", " "))}</span>${session.review_note ? `<small>${escapeHtml(session.review_note)}</small>` : ""}</span><span class="upload-review-actions">${isAdmin() && session.status === "pending_review" ? `<input class="input" data-review-note="${session.id}" maxlength="500" placeholder="Optional rejection reason" aria-label="Rejection reason"><span class="bulk-actions"><button class="button small" data-approve-upload="${session.id}">Approve</button><button class="button danger-subtle small" data-reject-upload="${session.id}">Reject</button></span>` : ""}${session.status === "uploading" ? `<button class="text-button" data-cancel-upload="${session.id}">Cancel</button>` : ""}</span></div>`).join("")}</div>`
    : "";
  return `<section class="upload-panel" aria-label="Upload ROMs">
    <div class="upload-head"><div><h2>${isAdmin() ? "Add ROMs to the library" : "Submit ROMs for review"}</h2><p>${isAdmin() ? "Uploads are staged, checked, then moved into the selected platform without overwriting existing files." : "Uploads remain isolated until an administrator approves them. You cannot overwrite or directly change the library."}</p></div></div>
    <form id="upload-form" class="upload-form">
      <label class="field"><span>Platform</span><select id="upload-platform" required><option value="">Choose platform</option>${state.platforms.map((item) => `<option value="${escapeHtml(item.platform)}" ${(state.uploadPlatform || state.platform) === item.platform ? "selected" : ""}>${escapeHtml(item.platform)}</option>`).join("")}</select></label>
      <label class="field"><span>Bundle name</span><input class="input" id="upload-bundle-name" maxlength="255" value="${escapeHtml(selection?.bundleName || "")}" placeholder="Filled from your selection"></label>
      <div class="upload-pickers"><label class="button secondary file-picker">Choose files<input id="upload-files" type="file" multiple></label><label class="button secondary file-picker">Choose game folder<input id="upload-folder" type="file" webkitdirectory multiple></label></div>
      ${fileSummary}${progress}
      <button class="button" type="submit" ${selection && !state.uploadProgress ? "" : "disabled"}>${isAdmin() ? "Upload to library" : "Upload and submit"}</button>
    </form>${active}
  </section>`;
}

function selectUploadFiles(fileList, folderMode) {
  const files = [...fileList];
  if (!files.length) return;
  const firstPath = files[0].webkitRelativePath || files[0].name;
  const rootName = folderMode && firstPath.includes("/") ? firstPath.split("/")[0] : "";
  const relativePath = (file) => {
    const path = file.webkitRelativePath || file.name;
    return rootName && path.startsWith(`${rootName}/`) ? path.slice(rootName.length + 1) : path;
  };
  const baseName = rootName || files[0].name.replace(/\.[^.]+$/, "");
  state.uploadSelection = {
    files,
    folderMode: folderMode || files.length > 1,
    bundleName: baseName,
    relativePath,
  };
  renderTransfers();
}

function setUploadProgress(label, uploaded, total) {
  const percent = total ? Math.min(100, Math.floor(uploaded * 100 / total)) : 100;
  state.uploadProgress = { label, percent };
  const panel = view.querySelector(".upload-progress");
  if (!panel) return;
  panel.querySelector("strong").textContent = label;
  panel.querySelector("span").textContent = `${percent}%`;
  panel.querySelector("progress").value = percent;
}

async function refreshTransfersIfActive() {
  if (state.view === "transfers") await renderTransfers();
}

async function runUpload(form) {
  const selection = state.uploadSelection;
  if (!selection) return;
  const platform = form.querySelector("#upload-platform").value;
  const bundleName = form.querySelector("#upload-bundle-name").value.trim() || selection.bundleName;
  state.uploadPlatform = platform;
  const manifest = selection.files.map((file) => ({
    relative_path: selection.relativePath(file),
    size: file.size,
  }));
  const total = selection.files.reduce((sum, file) => sum + file.size, 0);
  let uploaded = 0;
  state.uploadProgress = { label: "Preparing secure upload", percent: 0 };
  await refreshTransfersIfActive();
  const session = await api("/api/uploads", {
    method: "POST",
    body: JSON.stringify({
      platform,
      bundle_name: bundleName,
      folder_mode: selection.folderMode,
      files: manifest,
    }),
  });
  const chunkBytes = Number(session.chunk_bytes);
  for (let index = 0; index < selection.files.length; index += 1) {
    const file = selection.files[index];
    let offset = Number(session.files[index].received_size || 0);
    uploaded += offset;
    while (offset < file.size) {
      const end = Math.min(file.size, offset + chunkBytes);
      setUploadProgress(`Uploading ${file.name}`, uploaded, total);
      await api(`/api/uploads/${encodeURIComponent(session.id)}/files/${index}`, {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream", "Upload-Offset": String(offset) },
        body: file.slice(offset, end),
      });
      uploaded += end - offset;
      offset = end;
    }
  }
  setUploadProgress(isAdmin() ? "Adding ROM to the library" : "Submitting for review", total, total);
  const response = await api(`/api/uploads/${encodeURIComponent(session.id)}/finalize`, { method: "POST" });
  const result = response.submitted
    ? null
    : await (async () => { toast("Upload complete; indexing the new ROM"); await refreshStatus(); return waitForJob(response.job_id); })();
  state.uploadSelection = null;
  state.uploadProgress = null;
  state.uploadSessions = [];
  toast(response.submitted ? "Upload submitted for administrator review" : `Added ${result.files.toLocaleString()} ${result.files === 1 ? "file" : "files"} to ${platform}`);
  await refreshStatus();
  await loadReferenceData();
  await refreshTransfersIfActive();
}

function bindUploadEvents() {
  view.querySelector("#upload-files")?.addEventListener("change", (event) => selectUploadFiles(event.target.files, false));
  view.querySelector("#upload-folder")?.addEventListener("change", (event) => selectUploadFiles(event.target.files, true));
  view.querySelector("#upload-platform")?.addEventListener("change", (event) => { state.uploadPlatform = event.target.value; });
  view.querySelector("#upload-bundle-name")?.addEventListener("input", (event) => {
    if (state.uploadSelection) state.uploadSelection.bundleName = event.target.value;
  });
  view.querySelector("#upload-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await runUpload(event.currentTarget); }
    catch (error) {
      state.uploadProgress = null;
      toast(error.message, "error");
      await refreshTransfersIfActive();
    }
  });
  view.querySelectorAll("[data-cancel-upload]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api(`/api/uploads/${encodeURIComponent(button.dataset.cancelUpload)}`, { method: "DELETE" });
      toast("Upload cancelled and staged data removed");
      await refreshTransfersIfActive();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelectorAll("[data-approve-upload]").forEach((button) => button.addEventListener("click", async (event) => {
    try {
      await requestJob(`/api/uploads/${encodeURIComponent(event.currentTarget.dataset.approveUpload)}/approve`, { method: "POST" }, "Upload approved; adding it to the library");
      toast("Upload approved and indexed");
      await refreshTransfersIfActive();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelectorAll("[data-reject-upload]").forEach((button) => button.addEventListener("click", async (event) => {
    const sessionId = event.currentTarget.dataset.rejectUpload;
    const reason = view.querySelector(`[data-review-note="${CSS.escape(sessionId)}"]`)?.value || "Rejected by administrator";
    try {
      await api(`/api/uploads/${encodeURIComponent(sessionId)}/reject`, { method: "POST", body: JSON.stringify({ note: reason }) });
      toast("Upload rejected and staged files removed");
      await refreshTransfersIfActive();
    } catch (error) { toast(error.message, "error"); }
  }));
}

async function fetchMissingRatings() {
  const platform = state.platforms.find((item) => item.platform === state.platform);
  if (!platform) return;
  const missing = Math.max(0, Number(platform.count) - Number(platform.rated_count || 0));
  if (!missing) return;
  const confirmed = await confirmAction({
    title: `Fetch ratings for ${missing.toLocaleString()} ${platform.platform} games?`,
    content: `<p>ROMmates will match unrated games through ScreenScraper and cache their community score. This sends up to one metadata request per game, uses no artwork bandwidth, and remains subject to your ScreenScraper quota.</p>`,
    confirmLabel: "Fetch missing ratings",
    cancelLabel: "Not now",
    danger: false,
  });
  if (!confirmed) return;
  try {
    const result = await requestJob(
      "/api/ratings/scrape",
      { method: "POST", body: JSON.stringify({ platform: platform.platform, search: "" }) },
      `Rating job queued for ${platform.platform}`,
    );
    toast(`Matched ${result.matched || 0} ratings; ${result.skipped || 0} games skipped`);
    await refreshStatus();
    await loadReferenceData();
    await renderCurrentView();
  } catch (error) {
    toast(error.message, "error");
  }
}

function rankingPanel(data) {
  if (!data) return "";
  if (!data.configured) {
    return `<section class="ranking-panel"><div class="ranking-head"><div><h2>Top 100 coverage</h2><p>Add <code>ROMMATES_RAWG_API_KEY</code> to load a platform-wide Metacritic list.</p></div></div><p class="ranking-attribution">Rankings require a free personal API key from <a href="https://rawg.io/apidocs" target="_blank" rel="noopener noreferrer">RAWG</a>.</p></section>`;
  }
  const counts = data.counts || { owned: 0, possible: 0, missing: 0 };
  const rows = (data.items || []).map((item) => {
    const stateLabel = item.status === "owned" ? "Owned" : item.status === "possible" ? "Review match" : "Missing";
    const match = item.match ? `<span class="ranking-match" title="${escapeHtml(item.match.primary_relpath)}">${escapeHtml(item.match.display_name)}</span>` : "";
    return `<tr><td class="ranking-number">#${item.rank}</td><td class="name-cell"><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>${item.released ? `<span class="path-line">${escapeHtml(item.released)}</span>` : ""}</td><td class="meta">${item.score ?? "N/A"}</td><td><span class="badge ranking-${item.status}">${stateLabel}</span>${match}</td></tr>`;
  }).join("");
  const body = rows
    ? `<div class="ranking-table"><table><thead><tr><th>Rank</th><th>Game</th><th>Metacritic</th><th>Library match</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="ranking-empty"><strong>No ranking cached</strong><span>Fetch RAWG's Metacritic list for this platform.</span></div>`;
  return `<section class="ranking-panel"><div class="ranking-head"><div><h2>Top 100 coverage</h2><p><strong>${counts.owned}</strong> owned, <strong>${counts.possible}</strong> need review, <strong>${counts.missing}</strong> missing.</p></div>${isAdmin() ? `<button class="button secondary small" data-refresh-ranking>${rows ? "Refresh list" : "Fetch top 100"}</button>` : ""}</div>${body}<p class="ranking-attribution">Metacritic ranking data provided by <a href="https://rawg.io" target="_blank" rel="noopener noreferrer">RAWG</a>. Possible matches never count as owned until filenames match.</p></section>`;
}

async function refreshRanking() {
  if (!state.platform) return;
  try {
    await requestJob(
      `/api/rankings/${encodeURIComponent(state.platform)}/refresh`,
      { method: "POST" },
      `Fetching ${state.platform} top 100 from RAWG`,
    );
    await renderLibrary();
  } catch (error) {
    toast(error.message, "error");
  }
}

function gameRating(game) {
  if (game.rating === null || game.rating === undefined) {
    return '<span class="rating-empty">Not rated</span>';
  }
  const score = Number(game.rating);
  const rank = game.platform_rank ? `#${Number(game.platform_rank).toLocaleString()} on ${escapeHtml(game.platform)}` : "";
  return `<span class="rating-summary" title="ScreenScraper community rating"><strong>${score.toLocaleString(undefined, { maximumFractionDigits: 1 })}<small>/20</small></strong>${rank ? `<span>${rank}</span>` : ""}${game.top_staff ? '<span class="staff-pick">Staff pick</span>' : ""}</span>`;
}

function mobileGameMeta(game) {
  const parts = [
    formatBytes(game.size),
    game.rating === null || game.rating === undefined
      ? "Not rated"
      : `${Number(game.rating).toLocaleString(undefined, { maximumFractionDigits: 1 })}/20`,
  ];
  if (Number(game.file_count) > 1) parts.push(`${Number(game.file_count).toLocaleString()} files`);
  if (game.duplicate_status === "exact") parts.push("Exact duplicate");
  else if (game.duplicate_status === "possible") parts.push("Possible duplicate");
  return `<span class="mobile-game-meta"><span class="mobile-platform">${escapeHtml(game.platform)}</span>${parts.map((part) => `<span>${part}</span>`).join("")}</span>`;
}

function mobileDeviceAction(game) {
  const devices = game.devices || [];
  const pending = devices.some((device) => device.state === "pending_add" || device.state === "pending_remove");
  const label = devices.length ? `${devices.length} selected` : "None selected";
  return `<button class="button secondary small mobile-device-button ${devices.length ? "selected" : ""} ${pending ? "pending" : ""}" data-assign-devices="${game.id}" aria-label="Choose devices for ${escapeHtml(game.display_name)}"><span>Devices</span><span class="mobile-device-count">${devices.length}</span><span class="sr-only">, ${label}</span></button>`;
}

function mobileActionsMenu(game) {
  const devices = game.devices || [];
  const pending = devices.some((device) => device.state === "pending_add" || device.state === "pending_remove");
  const deviceAction = isAdmin() ? mobileDeviceAction(game) : "";
  const cleanupActions = isAdmin() ? `
    <button class="button secondary small" data-rename="${game.id}">Rename</button>
    <button class="button danger-subtle small" data-delete="${game.id}" data-name="${escapeHtml(game.display_name)}">Trash</button>` : "";
  return `<details class="mobile-actions-menu ${devices.length ? "has-devices" : ""} ${pending ? "pending" : ""}">
    <summary aria-label="Actions for ${escapeHtml(game.display_name)}"><span aria-hidden="true">•••</span>${isAdmin() && devices.length ? `<span class="mobile-menu-count">${devices.length}</span>` : ""}</summary>
    <div>
      ${deviceAction}
      <button class="button secondary small" data-download="${game.id}" data-name="${escapeHtml(game.display_name)}">Download</button>
      ${cleanupActions}
    </div>
  </details>`;
}

function gameRows(items, deviceMode = false) {
  return items.map((game) => {
    const checked = isAdmin() && (deviceMode ? game.selected : state.selectedRows.has(game.id));
    const editor = state.editingId === game.id ? `
      <tr class="inline-editor">
        <td colspan="${deviceMode ? 8 : 10}">
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
        <td colspan="${deviceMode ? 8 : 10}">
          <div class="assignment-panel">
            <div class="assignment-head">
              <div><h3>Include “${escapeHtml(game.display_name)}” on devices</h3><p>Selections update immediately. Apply each device when you are ready to copy or remove files.</p></div>
              <button class="button secondary small" data-close-assignment>Close</button>
            </div>
            ${state.assignmentDevices.length ? `<div class="device-choices">${state.assignmentDevices.map((device) => `<label class="device-choice"><input type="checkbox" data-assignment-checkbox data-device-id="${device.id}" data-game-id="${game.id}" ${device.selected ? "checked" : ""}><span>${escapeHtml(device.name)}</span></label>`).join("")}</div>` : `<p class="meta">No device folders have been discovered. Create a device/roms directory and scan again.</p>`}
          </div>
        </td>
      </tr>` : "";
    const artwork = !deviceMode && state.artworkId === game.id ? artworkPanel(game) : "";
    return `
      <tr class="game-row ${isAdmin() ? "" : "read-only-row"}">
        <td class="checkbox-cell">${isAdmin() ? `<input type="checkbox" aria-label="Select ${escapeHtml(game.display_name)}" data-${deviceMode ? "device" : "row"}-select="${game.id}" ${checked ? "checked" : ""}>` : ""}</td>
        <td class="artwork-cell">${artworkThumb(game, !deviceMode && (isAdmin() || Number(game.artwork_count) > 0))}</td>
        <td class="name-cell" title="${escapeHtml(game.primary_relpath)}"><strong>${escapeHtml(game.display_name)}</strong><span class="path-line">${escapeHtml(game.primary_relpath)}</span>${mobileGameMeta(game)}</td>
        <td class="platform-cell">${escapeHtml(game.platform)}</td>
        <td class="rating-cell">${gameRating(game)}</td>
        ${deviceMode ? "" : `<td class="duplicate-cell">${duplicateLabel(game.duplicate_status)}</td>`}
        <td class="meta size-cell">${formatBytes(game.size)}</td>
        <td class="meta optional-column">${game.file_count} ${game.file_count === 1 ? "file" : "files"}</td>
        <td class="meta optional-column">${deviceMode ? deviceTargetState(game) : deviceSummary(game, isAdmin())}</td>
        ${deviceMode ? "" : `<td class="nowrap actions-cell">
          <span class="desktop-row-actions"><button class="button secondary small download-action" data-download="${game.id}" data-name="${escapeHtml(game.display_name)}">Download</button>${isAdmin() ? `<button class="button secondary small" data-rename="${game.id}">Rename</button><button class="button danger-subtle small" data-delete="${game.id}" data-name="${escapeHtml(game.display_name)}">Trash</button>` : ""}</span>
          ${mobileActionsMenu(game)}
        </td>`}
      </tr>${editor}${assignment}${artwork}`;
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
  return `
    <div class="table-wrap library-table">
      <table>
        <thead><tr>
          <th class="checkbox-cell">${isAdmin() ? '<input type="checkbox" aria-label="Select visible ROMs" data-select-all>' : ""}</th>
          <th class="artwork-cell">Art</th><th>Filename</th><th>Platform</th><th>Rating</th>${deviceMode ? "" : "<th>Duplicate status</th>"}<th>Size</th><th class="optional-column">Bundle</th><th class="optional-column">${deviceMode ? "Target state" : "Devices"}</th>${deviceMode ? "" : "<th>Actions</th>"}
        </tr></thead>
        <tbody>${gameRows(data.items, deviceMode)}</tbody>
      </table>
    </div>
    ${infiniteFooter(data, data.total === 1 ? "game" : "games")}`;
}

function artworkThumb(game, interactive = true) {
  const hasArtwork = Number(game.artwork_count) > 0;
  const label = hasArtwork ? `View artwork for ${game.display_name}` : `Find artwork for ${game.display_name}`;
  const content = game.cover_asset_id ? `<img data-artwork-src="${game.cover_asset_id}" alt="" loading="lazy"><span class="sr-only">${game.artwork_count} assets</span>` : `<span aria-hidden="true">${hasArtwork ? "●" : "＋"}</span>`;
  return interactive
    ? `<button class="artwork-button ${hasArtwork ? "has-artwork" : ""}" data-artwork-view="${game.id}" data-artwork-name="${escapeHtml(game.display_name)}" data-artwork-existing="${game.artwork_count || 0}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${content}</button>`
    : `<span class="artwork-button passive ${hasArtwork ? "has-artwork" : ""}" title="${escapeHtml(hasArtwork ? `${game.artwork_count} cached assets` : "No cached artwork")}">${content}</span>`;
}

function artworkPanel(game) {
  const detail = state.artworkDetail;
  if (!detail) return `<tr class="inline-editor"><td colspan="10"><div class="artwork-panel"><p class="meta">Loading artwork…</p></div></td></tr>`;
  const metadata = detail.metadata;
  const cards = detail.assets.length
    ? detail.assets.map((asset) => `<figure class="asset-card"><img data-artwork-src="${asset.id}" alt="${escapeHtml(asset.kind)} for ${escapeHtml(game.display_name)}"><figcaption>${escapeHtml(asset.kind)} <span>${formatBytes(asset.size)}</span></figcaption></figure>`).join("")
    : '<div class="artwork-empty"><strong>No artwork cached</strong><p>Match this game with ScreenScraper to add a cover, screenshot, and logo.</p></div>';
  return `<tr class="inline-editor"><td colspan="10"><div class="artwork-panel">
    <div class="assignment-head"><div><h3>${escapeHtml(metadata?.title || game.display_name)}</h3><p>${metadata ? `Matched by ${escapeHtml(metadata.match_method)} · ScreenScraper game ${escapeHtml(metadata.source_game_id)}` : "No ScreenScraper match yet"}</p></div><div class="bulk-actions">${isAdmin() ? `<button class="button secondary small" data-manage-game-artwork="${game.id}" data-name="${escapeHtml(game.display_name)}" data-platform="${escapeHtml(game.platform)}">Manage artwork</button>` : ""}<button class="button secondary small" data-close-artwork>Close</button></div></div>
    ${metadata?.description ? `<p class="artwork-description">${escapeHtml(metadata.description)}</p>` : ""}
    <div class="asset-grid">${cards}</div>
  </div></td></tr>`;
}

async function loadArtworkImages() {
  state.artworkObserver?.disconnect();
  state.artworkUrls.forEach((url) => URL.revokeObjectURL(url));
  state.artworkUrls = [];
  const token = storedAccessToken();
  const load = async (image) => {
    try {
      const response = await fetch(`/api/artwork/assets/${image.dataset.artworkSrc}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!response.ok) return;
      const url = URL.createObjectURL(await response.blob());
      state.artworkUrls.push(url);
      if (image.isConnected) image.src = url;
    } catch { /* A missing thumbnail should not prevent the library from loading. */ }
  };
  if (!("IntersectionObserver" in window)) {
    await Promise.all([...view.querySelectorAll("[data-artwork-src]")].map(load));
    return;
  }
  state.artworkObserver = new IntersectionObserver((entries, observer) => {
    entries.filter((entry) => entry.isIntersecting).forEach((entry) => {
      observer.unobserve(entry.target);
      load(entry.target);
    });
  }, { rootMargin: "240px 0px" });
  view.querySelectorAll("[data-artwork-src]").forEach((image) => state.artworkObserver.observe(image));
}

function bulkBarHtml() {
  if (!isAdmin()) return "";
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

function dashboardDate(value) {
  if (!value) return "Never";
  const date = new Date(value.includes?.("T") ? value : `${value.replace(" ", "T")}Z`);
  if (!Number.isFinite(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(date);
}

function coverageBar(value, total, label) {
  const percent = total ? Math.round(value * 100 / total) : 0;
  return `<div class="coverage-row"><div><span>${escapeHtml(label)}</span><strong>${value.toLocaleString()} of ${total.toLocaleString()}</strong></div><div class="coverage-track" role="progressbar" aria-label="${escapeHtml(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div><small>${percent}%</small></div>`;
}

function dashboardJobBadge(job) {
  const badge = job.status === "failed" ? "exact" : job.status === "complete" ? "unique" : job.status === "cancelled" ? "cancelled" : "possible";
  return `<span class="badge ${badge}">${escapeHtml(job.status)}${["running", "paused", "cancelling"].includes(job.status) ? ` · ${job.progress}%` : ""}</span>`;
}

async function renderOverview() {
  const renderVersion = beginPageRender();
  setHeading("Overview", "Collection health and recent activity.");
  const data = await navigationApi("/api/dashboard");
  if (!pageRenderIsCurrent(renderVersion, "overview")) return;
  const collection = data.collection;
  const largestPlatform = Math.max(...data.platforms.map((item) => item.games), 1);
  const visiblePlatforms = data.platforms.slice(0, 10);
  const otherPlatforms = data.platforms.slice(10);
  const platformRows = visiblePlatforms.map((item) => `<button class="platform-row" data-dashboard-view="library" data-dashboard-platform="${escapeHtml(item.platform)}"><span class="platform-name">${escapeHtml(item.platform)}</span><span class="platform-bar"><i style="width:${Math.max(2, item.games * 100 / largestPlatform)}%"></i></span><strong>${item.games.toLocaleString()}</strong><small>${formatBytes(item.bytes)}</small></button>`).join("");
  const otherSummary = otherPlatforms.length
    ? `<div class="platform-rest"><span>${otherPlatforms.length} more platforms</span><strong>${otherPlatforms.reduce((sum, item) => sum + item.games, 0).toLocaleString()} games</strong></div>`
    : "";

  const pendingDeviceFiles = data.devices.reduce((sum, device) => sum + device.additions + device.removals, 0);
  const attention = [];
  if (data.last_scan && ["failed", "cancelled"].includes(data.last_scan.status)) attention.push({ view: "jobs", label: `Last scan ${data.last_scan.status}`, detail: data.last_scan.detail, tone: "danger", job: data.last_scan.id });
  if (data.cleanup.groups) attention.push({ view: "duplicates", label: `${data.cleanup.groups.toLocaleString()} exact duplicate groups`, detail: `${data.cleanup.extra_copies.toLocaleString()} extra copies · ${formatBytes(data.cleanup.reclaimable_bytes)} recoverable`, tone: "danger", duplicate: "exact" });
  if (data.cleanup.possible_groups) attention.push({ view: "duplicates", label: `${data.cleanup.possible_groups.toLocaleString()} possible duplicate groups`, detail: "Similar filenames with different content", tone: "warning", duplicate: "possible" });
  if (data.last_scan?.reported_issue_count) attention.push({ view: "jobs", label: `${data.last_scan.reported_issue_count.toLocaleString()} unreadable files`, detail: `From scan #${data.last_scan.id}`, tone: "warning", job: data.last_scan.id });
  const saveReview = (data.saves.matching?.orphan || 0) + (data.saves.matching?.possible || 0) + (data.saves.matching?.ambiguous || 0);
  if (saveReview) attention.push({ view: "saves", label: `${saveReview.toLocaleString()} save names need review`, detail: `${(data.saves.matching?.orphan || 0).toLocaleString()} have no ROM match`, tone: "warning", saveTab: "matches" });
  if (pendingDeviceFiles) attention.push({ view: "devices", label: `${pendingDeviceFiles.toLocaleString()} pending device file changes`, detail: "Selections have not been fully applied", tone: "accent" });
  if (data.cleanup.trash) attention.push({ view: "trash", label: `${data.cleanup.trash.toLocaleString()} bundles in trash`, detail: "Recoverable until permanently purged", tone: "neutral" });
  const missingArtwork = Math.max(0, collection.games - data.artwork.games);
  if (missingArtwork) attention.push({ view: "library", label: `${missingArtwork.toLocaleString()} games without artwork`, detail: data.screenscraper_configured ? "Select games to scrape missing assets" : "ScreenScraper credentials are not configured", tone: "neutral" });
  if (!data.cleanup.naming_catalogs) attention.push({ view: "naming", label: "No naming catalogs imported", detail: "Import a DAT for canonical filename matches", tone: "neutral" });
  if (data.syncthing.configured && !data.syncthing.available && !data.syncthing.checking) attention.push({ view: "overview", label: "Syncthing status unavailable", detail: data.syncthing.error || "ROMmates could not reach Syncthing", tone: "warning" });
  const attentionHtml = attention.length
    ? `<div class="attention-list">${attention.slice(0, 7).map((item) => `<button class="attention-row ${item.tone}" data-dashboard-view="${item.view}" ${item.duplicate ? `data-dashboard-duplicate="${item.duplicate}"` : ""} ${item.job ? `data-dashboard-job="${item.job}"` : ""} ${item.saveTab ? `data-dashboard-save-tab="${item.saveTab}"` : ""}><span class="attention-mark" aria-hidden="true"></span><span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.detail)}</small></span><span aria-hidden="true">→</span></button>`).join("")}</div>`
    : '<div class="dashboard-empty"><strong>Nothing needs immediate review</strong><span>Scans, devices, saves, and cleanup queues are current.</span></div>';

  const devicesHtml = data.devices.length
    ? `<div class="dashboard-table"><table><thead><tr><th>Device</th><th>Selected</th><th>Deployed</th><th>Pending files</th><th>Status</th></tr></thead><tbody>${data.devices.map((device) => {
        const pending = device.additions + device.removals;
        return `<tr><td><button class="text-button" data-dashboard-view="devices" data-dashboard-device="${device.id}">${escapeHtml(device.name)}</button></td><td>${device.selected_games.toLocaleString()}</td><td>${device.deployed_games.toLocaleString()}</td><td>${pending ? `<span class="dashboard-pending">+${device.additions} / −${device.removals}</span>` : "0"}</td><td>${pending ? '<span class="badge possible">Needs apply</span>' : '<span class="badge unique">Applied</span>'}</td></tr>`;
      }).join("")}</tbody></table></div>`
    : '<div class="dashboard-empty compact"><strong>No devices discovered</strong><span>Create a device ROM directory, then scan.</span></div>';

  const syncthingHtml = !data.syncthing.configured
    ? `<div class="dashboard-empty compact"><strong>Syncthing is not connected</strong><span>${escapeHtml(data.syncthing.error || "Configure the Syncthing API URL and key to see device presence.")}</span></div>`
    : data.syncthing.checking
      ? '<div class="syncthing-devices syncthing-loading" aria-label="Checking Syncthing device status"><div class="syncthing-device"><span class="skeleton-dot"></span><div><span class="skeleton-line wide"></span><span class="skeleton-line"></span></div></div><div class="syncthing-device"><span class="skeleton-dot"></span><div><span class="skeleton-line wide"></span><span class="skeleton-line"></span></div></div></div>'
    : !data.syncthing.available
      ? `<div class="dashboard-empty compact"><strong>Syncthing is unavailable</strong><span>${escapeHtml(data.syncthing.error || "Check the configured API URL and key.")}</span></div>`
      : data.syncthing.devices.length
        ? `<div class="syncthing-devices">${data.syncthing.devices.map((device) => {
            const detail = device.connected
              ? [device.connection_type, device.address, device.client_version].filter(Boolean).join(" · ")
              : device.paused ? "Paused" : device.last_seen ? `Last observed ${dashboardDate(device.last_seen)}` : "Not currently connected";
            return `<div class="syncthing-device"><span class="presence-dot ${device.connected ? "online" : "offline"}" aria-hidden="true"></span><div><strong>${escapeHtml(device.name)}</strong><small>${escapeHtml(detail)}</small></div><span class="badge ${device.connected ? "unique" : "cancelled"}">${device.connected ? "Online" : "Offline"}</span></div>`;
          }).join("")}</div>`
        : '<div class="dashboard-empty compact"><strong>No remote Syncthing devices</strong><span>Add a device to the Syncthing node, then refresh.</span></div>';

  const savesUpdated = data.saves.latest_mtime_ns
    ? dashboardDate(new Date(data.saves.latest_mtime_ns / 1e6).toISOString())
    : "No cloud files";
  const recentJobs = data.recent_jobs.length
    ? `<div class="dashboard-table recent-jobs"><table><thead><tr><th>Job</th><th>Status</th><th>Detail</th><th>Started</th></tr></thead><tbody>${data.recent_jobs.map((job) => `<tr><td><button class="text-button" data-dashboard-view="jobs" data-dashboard-job="${job.id}">${escapeHtml(job.kind)}</button></td><td>${dashboardJobBadge(job)}</td><td class="name-cell">${escapeHtml(job.detail)}</td><td class="meta">${dashboardDate(job.created_at)}</td></tr>`).join("")}</tbody></table></div>`
    : '<div class="dashboard-empty compact"><strong>No jobs yet</strong><span>Run a library scan to start collection history.</span></div>';

  setViewHtml(`<div class="overview-strip" aria-label="Collection summary">
    <div><span>Games</span><strong>${collection.games.toLocaleString()}</strong></div>
    <div><span>Platforms</span><strong>${collection.platforms.toLocaleString()}</strong></div>
    <div><span>Library size</span><strong>${formatBytes(collection.bytes)}</strong></div>
    <div><span>Indexed files</span><strong>${collection.files.toLocaleString()}</strong></div>
    <div class="scan-summary"><span>Last scan</span><strong>${data.last_scan ? dashboardDate(data.last_scan.completed_at || data.last_scan.created_at) : "Never"}</strong>${data.last_scan ? dashboardJobBadge(data.last_scan) : ""}</div>
  </div>
  <div class="dashboard-grid">
    <section class="dashboard-panel platform-panel"><div class="dashboard-panel-head"><div><h2>Collection by platform</h2><p>Largest indexed sets by game count.</p></div><button class="text-button" data-dashboard-view="library">Open library</button></div><div class="platform-list">${platformRows}${otherSummary}</div></section>
    <section class="dashboard-panel attention-panel"><div class="dashboard-panel-head"><div><h2>Needs attention</h2><p>Work that can change or improve the collection.</p></div></div>${attentionHtml}</section>
    <section class="dashboard-panel syncthing-panel"><div class="dashboard-panel-head"><div><h2>Syncthing devices</h2><p>${data.syncthing.available ? `${data.syncthing.online.toLocaleString()} of ${data.syncthing.total.toLocaleString()} online${data.syncthing.checked_at ? ` · checked ${dashboardDate(data.syncthing.checked_at)}` : ""}` : "Live connection status from the NUC."}</p></div><button class="text-button" data-refresh-syncthing ${data.syncthing.configured ? "" : "disabled"}>Refresh</button></div>${syncthingHtml}</section>
    <section class="dashboard-panel device-panel"><div class="dashboard-panel-head"><div><h2>Device sync</h2><p>Desired games compared with managed deployments.</p></div><button class="text-button" data-dashboard-view="devices">Manage devices</button></div>${devicesHtml}</section>
    <section class="dashboard-panel coverage-panel"><div class="dashboard-panel-head"><div><h2>Coverage</h2><p>Metadata and recovery readiness.</p></div></div>${coverageBar(data.artwork.games, collection.games, "Games with artwork")}${coverageBar(data.artwork.covers, collection.games, "Games with cover art")}<div class="coverage-facts"><div><span>Cached artwork</span><strong>${data.artwork.assets.toLocaleString()} assets · ${formatBytes(data.artwork.bytes)}</strong></div><div><span>Save snapshots</span><strong>${data.saves.snapshots.toLocaleString()}${data.saves.latest_snapshot ? ` · latest #${data.saves.latest_snapshot.id}` : ""}</strong></div></div></section>
    <section class="dashboard-panel saves-panel"><div class="dashboard-panel-head"><div><h2>Save vault</h2><p>${data.saves.error ? escapeHtml(data.saves.error) : "Syncthing-backed emulator saves visible to ROMmates."}</p></div><button class="text-button" data-dashboard-view="saves">Open saves</button></div><dl class="save-facts"><div><dt>Battery saves</dt><dd>${data.saves.save_files.toLocaleString()}</dd></div><div><dt>Save states</dt><dd>${data.saves.state_files.toLocaleString()}</dd></div><div><dt>All vault files</dt><dd>${data.saves.files.toLocaleString()} · ${formatBytes(data.saves.bytes)}</dd></div><div><dt>Last changed</dt><dd>${escapeHtml(savesUpdated)}</dd></div></dl></section>
    <section class="dashboard-panel activity-panel"><div class="dashboard-panel-head"><div><h2>Recent jobs</h2><p>Latest filesystem and metadata work.</p></div><button class="text-button" data-dashboard-view="jobs">All jobs</button></div>${recentJobs}</section>
  </div>`);

  view.querySelectorAll("[data-dashboard-view]").forEach((button) => button.addEventListener("click", () => {
    navigateTo(button.dataset.dashboardView, {
      platform: button.dataset.dashboardPlatform || "",
      duplicate: button.dataset.dashboardDuplicate,
      jobId: Number(button.dataset.dashboardJob) || null,
      deviceId: Number(button.dataset.dashboardDevice) || null,
      saveTab: button.dataset.dashboardSaveTab || null,
    });
  }));
  view.querySelector("[data-refresh-syncthing]")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      await api("/api/syncthing/status?refresh=true");
      await renderOverview();
    } catch (error) {
      toast(error.message, "error");
      event.currentTarget.disabled = false;
    }
  });
  if (data.syncthing.checking || data.syncthing.stale) {
    api("/api/syncthing/status?refresh=true")
      .then(() => { if (state.currentView === "overview") return renderOverview(); })
      .catch(() => { /* The refreshed dashboard will show the sanitized connection error. */ });
  }
}

async function renderLibrary() {
  const renderVersion = beginPageRender();
  setHeading("Library", "Browse, rename, and clean the canonical collection.");
  const response = await getGames();
  const ranking = state.rankingOpen && state.platform
    ? await api(`/api/rankings/${encodeURIComponent(state.platform)}`)
    : null;
  if (!pageRenderIsCurrent(renderVersion, "library")) return;
  const key = `library\u001f${state.search}\u001f${state.platform}\u001f${state.duplicate}\u001f${state.sort}`;
  const data = mergeInfinitePage(key, response);
  setViewHtml(`${libraryToolbar(true)}${rankingPanel(ranking)}${gamesTable(data)}<div id="bulk-bar-slot"></div>`);
  renderBulkBar();
  syncSelectAll();
  bindFilters(renderLibrary);
  view.querySelector("[data-refresh-ranking]")?.addEventListener("click", refreshRanking);
  bindGameEvents(data, false);
  bindInfiniteScroll(data, renderLibrary);
  loadArtworkImages();
}

function artworkCurrentRun(run) {
  if (!run || !ACTIVE_JOB_STATUSES.includes(run.status)) {
    return `<section class="artwork-current empty"><div><h2>No artwork scan running</h2><p>Start a full-library scan or choose a smaller scope below.</p></div></section>`;
  }
  const processed = Number(run.processed_games || 0);
  const total = Number(run.total_games || 0);
  const progress = total ? Math.min(100, Math.round(processed * 100 / total)) : 0;
  const status = ACTIVE_JOB_STATUSES.includes(run.job_status) ? run.job_status : run.status;
  return `<section class="artwork-current" aria-labelledby="artwork-current-title">
    <div class="artwork-current-head"><div><span class="badge ${status === "paused" ? "possible" : "unique"}">${escapeHtml(status)}</span><h2 id="artwork-current-title">${escapeHtml(run.scope_label)}</h2><p>${run.asset_mode === "cover" ? "Covers" : "Covers, screenshots, and logos"} · ${processed.toLocaleString()} of ${total.toLocaleString()} ROMs processed</p></div><div class="bulk-actions"><button class="button secondary" data-artwork-job="${run.job_id || ""}" ${run.job_id ? "" : "disabled"}>View report</button><button class="button danger-subtle" data-stop-artwork="${run.job_id || ""}" ${!run.job_id || status === "cancelling" ? "disabled" : ""}>${status === "cancelling" ? "Stopping…" : "Stop scan"}</button></div></div>
    <div class="coverage-track artwork-progress" role="progressbar" aria-label="Artwork scan progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><span style="width:${progress}%"></span></div>
    <div class="artwork-current-facts"><span>${progress}% complete</span><span>${Number(run.matched_games || 0).toLocaleString()} matched</span><span>${Number(run.downloaded_assets || 0).toLocaleString()} assets downloaded</span><span>${Number(run.skipped_games || 0).toLocaleString()} skipped</span></div>
    ${run.last_error ? `<p class="issue-warning">${escapeHtml(run.last_error)}</p>` : ""}
  </section>`;
}

function artworkScopePicker(gameData) {
  const scope = state.artworkScope;
  const scopeReady = scope === "library" || (scope === "platforms" ? state.artworkPlatforms.size > 0 : state.artworkGames.size > 0);
  const selectedGames = [...state.artworkGames.values()];
  const resultRows = gameData?.items?.filter((game) => !state.artworkGames.has(game.id)).slice(0, 50) || [];
  const platforms = state.platforms.map((item) => `<label class="artwork-platform-choice"><input type="checkbox" data-artwork-platform="${escapeHtml(item.platform)}" ${state.artworkPlatforms.has(item.platform) ? "checked" : ""}><span><strong>${escapeHtml(item.platform)}</strong><small>${Number(item.count).toLocaleString()} ROMs</small></span></label>`).join("");
  const selected = selectedGames.length
    ? `<div class="artwork-selected-games">${selectedGames.map((game) => `<span>${escapeHtml(game.display_name)} <small>${escapeHtml(game.platform)}</small><button type="button" data-remove-artwork-game="${game.id}" aria-label="Remove ${escapeHtml(game.display_name)}">×</button></span>`).join("")}</div>`
    : `<p class="meta">No ROMs selected yet.</p>`;
  const results = resultRows.length
    ? `<div class="artwork-game-results">${resultRows.map((game) => `<label><input type="checkbox" data-add-artwork-game="${game.id}"><span><strong>${escapeHtml(game.display_name)}</strong><small>${escapeHtml(game.platform)} · ${escapeHtml(game.primary_relpath)}</small></span></label>`).join("")}</div>`
    : state.artworkSearch || state.artworkPlatform ? `<p class="meta">No matching ROMs.</p>` : `<p class="meta">Search or filter to choose ROMs.</p>`;
  return `<section class="artwork-scope" aria-labelledby="artwork-scope-title">
    <div class="section-heading"><div><h2 id="artwork-scope-title">Start an artwork scan</h2><p>Only missing local assets are queued. ScreenScraper limits and automatic backoff still apply.</p></div></div>
    <div class="artwork-scope-tabs" role="radiogroup" aria-label="Scan scope">
      <label><input type="radio" name="artwork-scope" value="library" ${scope === "library" ? "checked" : ""}><span>Full library</span></label>
      <label><input type="radio" name="artwork-scope" value="platforms" ${scope === "platforms" ? "checked" : ""}><span>Platforms</span></label>
      <label><input type="radio" name="artwork-scope" value="games" ${scope === "games" ? "checked" : ""}><span>ROMs</span></label>
    </div>
    ${scope === "platforms" ? `<div class="artwork-platform-grid">${platforms}</div>` : ""}
    ${scope === "games" ? `<div class="artwork-game-picker">${selected}<div class="toolbar"><label class="search-field"><span class="sr-only">Search ROMs for artwork</span><input id="artwork-search" type="search" value="${escapeHtml(state.artworkSearch)}" placeholder="Search ROMs" autocomplete="off"></label><label><span class="sr-only">Filter ROMs by platform</span><select id="artwork-platform-filter">${platformOptions(state.platforms, "", state.artworkPlatform)}</select></label></div>${results}</div>` : ""}
    <div class="artwork-start-row"><label class="field"><span>Assets</span><select id="artwork-asset-mode"><option value="cover" ${state.artworkAssetMode === "cover" ? "selected" : ""}>Covers only</option><option value="full" ${state.artworkAssetMode === "full" ? "selected" : ""}>Covers, screenshots, and logos</option></select></label><button class="button" data-start-artwork-scan ${scopeReady ? "" : "disabled"}>Start ${scope === "library" ? "full" : "partial"} scan</button></div>
  </section>`;
}

function artworkHistory(runs) {
  if (!runs.length) return `<section class="artwork-history"><div class="section-heading"><div><h2>Past scans</h2><p>Completed artwork work will appear here.</p></div></div></section>`;
  return `<section class="artwork-history"><div class="section-heading"><div><h2>Past scans</h2><p>Scope, results, and completion status for previous runs.</p></div></div><div class="table-wrap"><table><thead><tr><th>Scope</th><th>Assets</th><th>Status</th><th>Processed</th><th>Matched</th><th>Downloaded</th><th>Started</th><th>Action</th></tr></thead><tbody>${runs.map((run) => `<tr><td class="name-cell"><strong>${escapeHtml(run.scope_label)}</strong><span>${escapeHtml(run.scope_type)}</span></td><td>${run.asset_mode === "cover" ? "Covers" : "Full set"}</td><td><span class="badge ${run.status === "complete" ? "unique" : run.status === "failed" ? "exact" : run.status === "cancelled" ? "cancelled" : "possible"}">${escapeHtml(run.status)}</span></td><td>${Number(run.processed_games).toLocaleString()} / ${Number(run.total_games).toLocaleString()}</td><td>${Number(run.matched_games).toLocaleString()}</td><td>${Number(run.downloaded_assets).toLocaleString()}</td><td class="meta">${escapeHtml(run.created_at)} UTC</td><td><button class="button secondary small" data-artwork-job="${run.job_id || ""}" ${run.job_id ? "" : "disabled"}>Report</button></td></tr>`).join("")}</tbody></table></div></section>`;
}

async function renderArtwork() {
  const renderVersion = beginPageRender();
  setHeading("Artwork", "Scan ScreenScraper by library, platform, or selected ROMs.");
  const gameParams = new URLSearchParams({ search: state.artworkSearch, platform: state.artworkPlatform, duplicate: "all", sort: "name_asc", limit: "100", offset: "0" });
  const requests = [api("/api/artwork/bulk"), api("/api/artwork/runs?limit=100")];
  if (state.artworkScope === "games") requests.push(api(`/api/games?${gameParams}`));
  const [summary, runs, gameData = null] = await Promise.all(requests);
  if (!pageRenderIsCurrent(renderVersion, "artwork")) return;
  const quotaUsed = Number(summary.quota?.requests_today || 0);
  const quotaLimit = Number(summary.quota?.max_requests_per_day || 0);
  const quota = quotaLimit ? `${quotaUsed.toLocaleString()} of ${quotaLimit.toLocaleString()} requests used today` : "Account limits are read from ScreenScraper";
  const pastRuns = runs.filter((run) => !ACTIVE_JOB_STATUSES.includes(run.status));
  setViewHtml(`<div class="artwork-summary"><div><span>Coverage</span><strong>${Number(summary.covers).toLocaleString()} / ${Number(summary.games).toLocaleString()} covers</strong></div><div><span>Complete sets</span><strong>${Number(summary.full).toLocaleString()} / ${Number(summary.games).toLocaleString()}</strong></div><div><span>Quota</span><strong>${escapeHtml(quota)}</strong></div></div>${artworkCurrentRun(summary.run)}${artworkScopePicker(gameData)}${artworkHistory(pastRuns)}`);
  bindArtworkPageEvents();
}

function bindArtworkPageEvents() {
  view.querySelectorAll('input[name="artwork-scope"]').forEach((input) => input.addEventListener("change", () => { state.artworkScope = input.value; renderArtwork(); }));
  view.querySelectorAll("[data-artwork-platform]").forEach((input) => input.addEventListener("change", () => {
    if (input.checked) state.artworkPlatforms.add(input.dataset.artworkPlatform);
    else state.artworkPlatforms.delete(input.dataset.artworkPlatform);
  }));
  view.querySelector("#artwork-asset-mode")?.addEventListener("change", (event) => { state.artworkAssetMode = event.target.value; });
  const search = view.querySelector("#artwork-search");
  let searchTimer;
  search?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.artworkSearch = search.value; renderArtwork(); }, 220);
  });
  view.querySelector("#artwork-platform-filter")?.addEventListener("change", (event) => { state.artworkPlatform = event.target.value; renderArtwork(); });
  view.querySelectorAll("[data-add-artwork-game]").forEach((input) => input.addEventListener("change", () => {
    const id = Number(input.dataset.addArtworkGame);
    const row = input.closest("label");
    state.artworkGames.set(id, { id, display_name: row.querySelector("strong").textContent, platform: row.querySelector("small").textContent.split(" · ")[0] });
    renderArtwork();
  }));
  view.querySelectorAll("[data-remove-artwork-game]").forEach((button) => button.addEventListener("click", () => { state.artworkGames.delete(Number(button.dataset.removeArtworkGame)); renderArtwork(); }));
  view.querySelector("[data-start-artwork-scan]")?.addEventListener("click", startArtworkScan);
  view.querySelectorAll("[data-artwork-job]").forEach((button) => button.addEventListener("click", () => navigateTo("jobs", { jobId: Number(button.dataset.artworkJob) || null })));
  view.querySelector("[data-stop-artwork]")?.addEventListener("click", (event) => cancelJob(Number(event.currentTarget.dataset.stopArtwork), event.currentTarget));
}

async function startArtworkScan(event) {
  const button = event.currentTarget;
  const platforms = state.artworkScope === "platforms" ? [...state.artworkPlatforms] : [];
  const gameIds = state.artworkScope === "games" ? [...state.artworkGames.keys()] : [];
  if (state.artworkScope === "platforms" && !platforms.length) return toast("Choose at least one platform", "error");
  if (state.artworkScope === "games" && !gameIds.length) return toast("Choose at least one ROM", "error");
  button.disabled = true;
  try {
    const response = await api("/api/artwork/scrape-all", { method: "POST", body: JSON.stringify({ asset_mode: state.artworkAssetMode, platforms, game_ids: gameIds }) });
    toast(response.already_complete ? "Nothing is missing in that scope" : response.already_running ? "An artwork scan is already running" : `Queued ${Number(response.requested).toLocaleString()} ROMs`);
    await refreshStatus();
    await renderArtwork();
  } catch (error) { toast(error.message, "error"); button.disabled = false; }
}

async function renderTransfers() {
  const renderVersion = beginPageRender();
  setHeading("Transfers", "Upload ROMs securely and resume interrupted transfers.");
  const response = await api("/api/uploads");
  if (!pageRenderIsCurrent(renderVersion, "transfers")) return;
  state.uploadSessions = response.items;
  setViewHtml(`${uploadPanel()}<section class="transfer-guidance"><h2>Downloads</h2><p>Open Library and use Download on any game. Multi-file games are streamed as a ZIP without building a temporary archive on the server.</p></section>`);
  bindUploadEvents();
}

async function renderDuplicates() {
  const renderVersion = beginPageRender();
  setHeading("Duplicates", "Exact hashes first, filename matches for manual review.");
  if (state.duplicate === "all" || state.duplicate === "unique") state.duplicate = "exact";
  const params = new URLSearchParams({
    kind: state.duplicate,
    search: state.search,
    platform: state.platform,
    limit: 30,
    offset: state.offset,
  });
  const response = await navigationApi(`/api/duplicates?${params}`);
  if (!pageRenderIsCurrent(renderVersion, "duplicates")) return;
  if (response.total > 0 && state.offset >= response.total) {
    state.offset = 0;
    return renderDuplicates();
  }
  const key = `duplicates\u001f${state.duplicate}\u001f${state.search}\u001f${state.platform}`;
  const data = mergeInfinitePage(key, response, (group) => `${group.kind}\u001f${group.key}`);
  const possible = state.duplicate === "possible";
  const content = data.items.length
    ? `<div class="duplicate-groups">${data.items.map((group, index) => duplicateGroupHtml(group, index)).join("")}</div>${duplicatePager(data)}`
    : `<div class="empty-state duplicate-empty"><div><h2>No ${possible ? "possible" : "exact"} duplicate groups found</h2><p>${state.search || state.platform ? "Try a different title or platform." : possible ? "No same-platform filenames need manual comparison." : "Every indexed bundle has unique content."}</p></div></div>`;
  setViewHtml(`${libraryToolbar(true)}<div class="section-heading duplicate-heading"><div><h2>${possible ? "Possible duplicate groups" : "Exact duplicate groups"}</h2><p>${possible ? "Names normalize to the same title. Inspect paths and sizes before choosing what to keep." : "Every section contains complete bundles with the same content hash. Choose keepers, then move every reviewed non-keeper to Trash in one job."}</p></div><span class="meta">${data.total.toLocaleString()} ${data.total === 1 ? "group" : "groups"}</span></div>${duplicateBatchBarHtml(data)}${content}`);
  bindFilters(renderDuplicates);
  bindDuplicateGroups(data);
  bindInfiniteScroll(data, renderDuplicates);
}

function duplicateReviewKey(group) {
  return `${group.kind}\u001f${group.key}`;
}

function duplicateReviews(kind = state.duplicate) {
  return [...state.duplicateKeepers.values()].filter((review) => review.kind === kind);
}

function duplicateBatchBarHtml(data) {
  const reviews = duplicateReviews();
  const removals = reviews.reduce((total, review) => total + review.group.items.length - 1, 0);
  const suggestions = data.items.filter((group) =>
    group.recommended_keeper_id && !group.device_conflict && !state.duplicateKeepers.has(duplicateReviewKey(group))
  ).length;
  return `<div class="duplicate-batch-bar" aria-label="Duplicate review progress">
    <div><strong id="duplicate-reviewed-count">${reviews.length.toLocaleString()} groups reviewed</strong><span id="duplicate-removal-count">${removals.toLocaleString()} non-keepers ready for Trash · ${data.total.toLocaleString()} groups match the current filters</span></div>
    <div class="bulk-actions"><button class="button secondary small" data-use-duplicate-suggestions ${suggestions ? "" : "disabled"}>Use ${suggestions.toLocaleString()} safe ${suggestions === 1 ? "suggestion" : "suggestions"} on this page</button><button class="button danger small" data-clean-duplicate-batch ${removals ? "" : "disabled"}>Trash ${removals.toLocaleString()} reviewed non-keepers</button></div>
  </div>`;
}

function duplicateGroupHtml(group, index) {
  const review = state.duplicateKeepers.get(duplicateReviewKey(group));
  const keeper = review?.keeperId;
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
      <td class="keeper-cell"><label class="keeper-choice"><input type="radio" name="keeper-${index}" value="${game.id}" data-duplicate-keeper="${index}" ${checked ? "checked" : ""} ${group.device_conflict ? "disabled" : ""}><span>Keep</span></label>${suggested ? '<span class="badge naming-exact keeper-suggestion">Suggested</span>' : ""}</td>
      <td class="name-cell" title="${escapeHtml(game.primary_relpath)}"><strong>${escapeHtml(game.display_name)}</strong><span class="path-line">${escapeHtml(game.primary_relpath)}</span></td>
      <td>${escapeHtml(game.platform)}</td>
      <td class="meta">${formatBytes(game.size)}</td>
      <td class="meta optional-column">${game.file_count} ${game.file_count === 1 ? "file" : "files"}</td>
      <td class="meta optional-column"><span class="duplicate-device-state">${deviceState}</span></td>
      <td>${saveImpactHtml(game.save_impact)}</td>
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
      <span class="badge ${keeper ? "unique" : group.device_conflict ? "possible" : "neutral"}" data-duplicate-review-state>${keeper ? "Keeper selected" : group.device_conflict ? "Resolve device usage" : "Choose a keeper"}</span>
    </div>
    <div class="duplicate-table-wrap"><table><thead><tr><th>Decision</th><th>Filename and path</th><th>Platform</th><th>Size</th><th class="optional-column">Bundle</th><th class="optional-column">Devices</th><th>Save impact</th></tr></thead><tbody>${rows}</tbody></table></div>
  </section>`;
}

function duplicatePager(data) {
  return infiniteFooter(data, data.total === 1 ? "group" : "groups");
}

function updateDuplicateReviewUi(data) {
  const reviews = duplicateReviews();
  const removals = reviews.reduce((total, review) => total + review.group.items.length - 1, 0);
  const suggestions = data.items.filter((group) =>
    group.recommended_keeper_id && !group.device_conflict && !state.duplicateKeepers.has(duplicateReviewKey(group))
  ).length;
  const reviewed = view.querySelector("#duplicate-reviewed-count");
  const removal = view.querySelector("#duplicate-removal-count");
  const batch = view.querySelector("[data-clean-duplicate-batch]");
  const suggest = view.querySelector("[data-use-duplicate-suggestions]");
  if (reviewed) reviewed.textContent = `${reviews.length.toLocaleString()} groups reviewed`;
  if (removal) removal.textContent = `${removals.toLocaleString()} non-keepers ready for Trash · ${data.total.toLocaleString()} groups match the current filters`;
  if (batch) {
    batch.disabled = removals === 0;
    batch.textContent = `Trash ${removals.toLocaleString()} reviewed non-keepers`;
  }
  if (suggest) {
    suggest.disabled = suggestions === 0;
    suggest.textContent = `Use ${suggestions.toLocaleString()} safe ${suggestions === 1 ? "suggestion" : "suggestions"} on this page`;
  }
}

function markDuplicateKeeper(group, index, keeperId) {
  const keeper = group.items.find((game) => game.id === keeperId);
  if (!keeper || group.device_conflict) return;
  state.duplicateKeepers.set(duplicateReviewKey(group), { kind: group.kind, key: group.key, keeperId, keeper, group });
  const section = view.querySelector(`[data-duplicate-group="${index}"]`);
  section?.querySelectorAll("[data-duplicate-row]").forEach((row) => row.classList.toggle("keeper-row", Number(row.dataset.duplicateRow) === keeperId));
  const radio = section?.querySelector(`[data-duplicate-keeper][value="${keeperId}"]`);
  if (radio) radio.checked = true;
  const status = section?.querySelector("[data-duplicate-review-state]");
  if (status) {
    status.className = "badge unique";
    status.textContent = "Keeper selected";
  }
}

function bindDuplicateGroups(data) {
  const groups = data.items;
  view.querySelectorAll("[data-duplicate-keeper]").forEach((radio) => radio.addEventListener("change", () => {
    const index = Number(radio.dataset.duplicateKeeper);
    const group = groups[index];
    markDuplicateKeeper(group, index, Number(radio.value));
    updateDuplicateReviewUi(data);
  }));
  view.querySelector("[data-use-duplicate-suggestions]")?.addEventListener("click", () => {
    groups.forEach((group, index) => {
      if (group.recommended_keeper_id && !group.device_conflict && !state.duplicateKeepers.has(duplicateReviewKey(group))) {
        markDuplicateKeeper(group, index, group.recommended_keeper_id);
      }
    });
    updateDuplicateReviewUi(data);
  });
  view.querySelector("[data-clean-duplicate-batch]")?.addEventListener("click", () => cleanDuplicateBatch());
}

async function cleanDuplicateBatch() {
  const reviews = duplicateReviews();
  if (!reviews.length) return;
  const removals = reviews.flatMap((review) => review.group.items.filter((game) => game.id !== review.keeperId));
  const affectedDevices = [...new Set(removals.flatMap((game) => [
    ...(game.present_devices || []),
    ...(game.selected_devices || []),
  ]))];
  const saveAffected = removals.filter((game) => game.save_impact?.status && game.save_impact.status !== "none");
  const saveImpactWarning = saveAffected.length
    ? `<p class="issue-warning"><strong>${saveAffected.length} ${saveAffected.length === 1 ? "copy has" : "copies have"} matching RetroArch save data.</strong> Removing these ROM filenames can orphan the saves below. ROMmates will not delete or rename the save files.</p><ul class="confirm-list">${saveAffected.flatMap((game) => (game.save_impact.paths || []).slice(0, 5).map((path) => `<li><code>${escapeHtml(path)}</code> matched ${escapeHtml(game.primary_relpath)}</li>`)).join("")}</ul>`
    : "";
  const shown = reviews.slice(0, 40);
  const reviewList = shown.map((review) => `<li><strong>${escapeHtml(review.group.label)}</strong>: keep <code>${escapeHtml(review.keeper.primary_relpath)}</code>, trash ${review.group.items.length - 1}</li>`).join("");
  const confirmed = await confirmAction({
    title: `Trash ${removals.length} non-keepers from ${reviews.length} reviewed ${reviews.length === 1 ? "group" : "groups"}?`,
    content: `<p class="warning-copy">Every non-keeper bundle below moves to recoverable Trash with its companion files and managed device copies.</p><ul class="confirm-list">${reviewList}${reviews.length > shown.length ? `<li>And ${(reviews.length - shown.length).toLocaleString()} more reviewed groups</li>` : ""}</ul>${saveImpactWarning}${affectedDevices.length ? `<p class="issue-warning"><strong>Device impact:</strong> A removed copy is present or selected on ${escapeHtml(affectedDevices.join(", "))}. Review those device assignments after cleanup.</p>` : ""}${state.duplicate === "possible" ? '<p class="issue-warning"><strong>Content is not identical.</strong> These bundles only have similar names.</p>' : ""}`,
    confirmLabel: `Trash ${removals.length} non-keepers`,
    cancelLabel: "Review again",
    danger: true,
  });
  if (!confirmed) return;
  try {
    const result = await requestJob("/api/duplicates/trash", {
      method: "POST",
      body: JSON.stringify({ items: reviews.map((review) => ({ kind: review.kind, group_key: review.key, keeper_id: review.keeperId })) }),
    }, `Moving ${removals.length} reviewed non-keepers to Trash`);
    for (const review of reviews) state.duplicateKeepers.delete(duplicateReviewKey(review.group));
    toast(`Kept one copy in ${result.groups} groups; moved ${result.trashed} bundles to Trash`);
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  await refreshStatus();
  await loadReferenceData();
  await renderDuplicates();
}

function bindGameEvents(data, deviceMode) {
  const byId = new Map(data.items.map((game) => [game.id, game]));
  const mobileMenus = [...view.querySelectorAll(".mobile-actions-menu")];
  mobileMenus.forEach((menu) => menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    mobileMenus.forEach((other) => { if (other !== menu) other.open = false; });
  }));
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
      const impact = detail.save_impact;
      const saveWarning = impact?.status && impact.status !== "none"
        ? `<p class="issue-warning"><strong>${escapeHtml(saveMatchLabel(impact.status))}: ${impact.save_files} ${impact.save_files === 1 ? "save" : "saves"} and ${impact.state_files} ${impact.state_files === 1 ? "state" : "states"}.</strong> This operation renames the ROM bundle only. RetroArch save filenames will not change.</p>${impact.paths?.length ? `<ul class="confirm-list save-impact-paths">${impact.paths.slice(0, 8).map((path) => `<li><code>${escapeHtml(path)}</code></li>`).join("")}${impact.paths.length > 8 ? `<li>and ${impact.paths.length - 8} more</li>` : ""}</ul>` : ""}`
        : "";
      const confirmed = await confirmAction({
        title: "Rename this bundle?",
        content: `<p class="warning-copy">ROMmates will rename the complete file or folder bundle. References inside CUE, GDI, and M3U descriptors will be updated.</p>${saveWarning}<p><strong>${escapeHtml(bundleNames + bundleOverflow)}</strong></p>`,
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
  view.querySelectorAll("[data-download]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const ticket = await api(`/api/games/${button.dataset.download}/download-ticket`, { method: "POST" });
      const anchor = document.createElement("a");
      anchor.href = ticket.url;
      anchor.download = ticket.filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      toast(ticket.files > 1 ? `Streaming ${ticket.files} files as ${ticket.filename}` : `Downloading ${ticket.filename}`);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  }));
  view.querySelectorAll("[data-artwork-view]").forEach((button) => button.addEventListener("click", async () => {
    const gameId = Number(button.dataset.artworkView);
    if (Number(button.dataset.artworkExisting) === 0) {
      state.artworkScope = "games";
      state.artworkGames = new Map([[gameId, {
        id: gameId,
        display_name: button.dataset.artworkName,
        platform: button.closest("tr")?.children?.[3]?.textContent?.trim() || "",
      }]]);
      navigateTo("artwork");
      return;
    }
    if (state.artworkId === gameId) {
      state.artworkId = null;
      state.artworkDetail = null;
      await renderCurrentView();
      return;
    }
    state.artworkId = gameId;
    state.artworkDetail = null;
    state.editingId = null;
    state.assigningId = null;
    await renderCurrentView();
    try {
      state.artworkDetail = await api(`/api/games/${gameId}/artwork`);
      await renderCurrentView();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelector("[data-close-artwork]")?.addEventListener("click", () => {
    state.artworkId = null;
    state.artworkDetail = null;
    renderCurrentView();
  });
  view.querySelector("[data-manage-game-artwork]")?.addEventListener("click", (event) => {
    state.artworkScope = "games";
    state.artworkGames = new Map([[Number(event.currentTarget.dataset.manageGameArtwork), {
      id: Number(event.currentTarget.dataset.manageGameArtwork),
      display_name: event.currentTarget.dataset.name,
      platform: event.currentTarget.dataset.platform,
    }]]);
    navigateTo("artwork");
  });
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
}

async function deleteOne(id, name) {
  try {
    const detail = await api(`/api/games/${id}`);
    const selectedDevices = detail.devices.filter((item) => item.selected).map((item) => item.name);
    const impact = detail.save_impact;
    const saveWarning = impact?.status && impact.status !== "none"
      ? `<p class="issue-warning"><strong>${escapeHtml(saveMatchLabel(impact.status))}: ${impact.files} RetroArch ${impact.files === 1 ? "file matches" : "files match"} this ROM filename.</strong> Moving the ROM to Trash will leave the saves in place, where they may become orphans.</p><ul class="confirm-list">${(impact.paths || []).slice(0, 8).map((path) => `<li><code>${escapeHtml(path)}</code></li>`).join("")}</ul>`
      : "";
    const confirmed = await confirmAction({
      title: `Move “${name}” to trash?`,
      content: `<p class="warning-copy">This moves all <strong>${detail.files.length} bundle ${detail.files.length === 1 ? "file" : "files"}</strong> out of the canonical library.${selectedDevices.length ? ` Deployed copies on <strong>${escapeHtml(selectedDevices.join(", "))}</strong> will be removed.` : ""} You can restore the bundle from Trash.</p>${saveWarning}`,
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
  const impactChunks = [];
  for (let index = 0; index < entries.length; index += 500) {
    impactChunks.push(entries.slice(index, index + 500).map(([id]) => id));
  }
  let impactResponses;
  try {
    impactResponses = await Promise.all(impactChunks.map((game_ids) => api("/api/saves/impacts", {
      method: "POST",
      body: JSON.stringify({ game_ids }),
    })));
  } catch (error) {
    toast(`Could not check save impact: ${error.message}`, "error");
    return;
  }
  const impacts = Object.values(Object.assign({}, ...impactResponses.map((response) => response.items)));
  const saveAffected = impacts.filter((impact) => impact.status !== "none");
  const saveWarning = saveAffected.length
    ? `<p class="issue-warning"><strong>${saveAffected.length} selected ${saveAffected.length === 1 ? "game has" : "games have"} matching save data.</strong> The saves will stay in place and may become orphans. Review Save matching after cleanup.</p>`
    : "";
  const confirmed = await confirmAction({
    title: `Move ${count} ${count === 1 ? "bundle" : "bundles"} to trash?`,
    content: `<p class="warning-copy">Each selected game and every file in its bundle will move to recoverable trash. Deployed copies managed by this app will be removed.</p>${saveWarning}<ul class="confirm-list">${preview}</ul>${overflow}`,
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

function deviceMetric(value, label, explanation) {
  const count = Number(value).toLocaleString();
  const accessibleLabel = `${count} ${label}. ${explanation}`;
  return `<span class="device-metric" tabindex="0" aria-label="${escapeHtml(accessibleLabel)}" data-tooltip="${escapeHtml(explanation)}"><strong>${count}</strong> ${escapeHtml(label)} <span class="metric-info" aria-hidden="true">ⓘ</span></span>`;
}

async function renderDevices() {
  const renderVersion = beginPageRender();
  setHeading("Devices", "See what is present now, then choose what changes next.");
  if (!state.devices.length) {
    setViewHtml(`<div class="empty-state"><div><h2>No device folders found</h2><p>Create a directory such as <code>/devices/retroid/roms</code>, then scan the library. Device folders are discovered automatically.</p><button class="button" data-scan>Scan again</button></div></div>`);
    view.querySelector("[data-scan]")?.addEventListener("click", () => startScan());
    return;
  }
  const device = state.devices.find((item) => item.id === Number(state.deviceId)) || state.devices[0];
  state.deviceId = device.id;
  const [response, preview] = await Promise.all([
    getGames(device.id, state.deviceScope),
    navigationApi(`/api/devices/${device.id}/preview`),
  ]);
  if (!pageRenderIsCurrent(renderVersion, "devices")) return;
  const key = `devices\u001f${device.id}\u001f${state.deviceScope}\u001f${state.search}\u001f${state.platform}\u001f${state.sort}`;
  const data = mergeInfinitePage(key, response);
  const inventory = data.device_inventory;
  const scopedPlatformCounts = new Map(
    (inventory.platforms || []).map((item) => [item.platform, Number(item.count)]),
  );
  const platformItems = state.platforms.map((item) => ({
    ...item,
    count: state.deviceScope === "all" ? item.count : scopedPlatformCounts.get(item.platform) || 0,
  }));
  const platformCountSuffix = state.deviceScope === "on_device"
    ? " on device"
    : state.deviceScope === "changes"
      ? " pending"
      : " in library";
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
      <label class="field"><span>Deployment storage</span><select id="deployment-mode"><option value="copy" ${device.deployment_mode === "copy" ? "selected" : ""}>Independent copies</option><option value="hardlink" ${device.deployment_mode === "hardlink" ? "selected" : ""}>Prefer hardlinks</option></select></label>
      <div class="device-summary">
        ${deviceMetric(preview.hardlinked, "hardlinked", "Individual device files that share storage with their canonical library files on the NUC.")}
        ${deviceMetric(preview.copied, "copied", "Individual managed device files stored as independent copies on the NUC.")}
        ${preview.missing ? deviceMetric(preview.missing, "managed files missing", "Files recorded as deployed by ROMmates that are no longer present in the device directory.") : ""}
        ${preview.unknown ? deviceMetric(preview.unknown, "storage states unknown", "Managed files whose source or device storage identity could not be inspected.") : ""}
        ${deviceMetric(inventory.present_games, "currently on device", "Library game bundles matched to files currently present in this device directory. Unmatched files are counted separately below.")}
        ${deviceMetric(preview.games, "desired", "Game bundles currently selected in ROMmates for this device.")}
        ${deviceMetric(preview.additions, "files to add/update", "Individual files ROMmates will create or replace the next time changes are applied.")}
        ${preview.conversions ? deviceMetric(preview.conversions, "copies to convert", "Existing managed copies eligible to be replaced with space-saving hardlinks.") : ""}
        ${deviceMetric(preview.removals, "files to remove", "Managed files ROMmates will remove because their games are no longer selected.")}
        <button class="button" id="apply-device" ${preview.additions === 0 && preview.removals === 0 && preview.conversions === 0 ? "disabled" : ""}>Review and apply</button>
      </div>
    </div>
    <div class="device-scope" role="group" aria-label="Device ROM view">
      <button class="scope-button ${state.deviceScope === "on_device" ? "active" : ""}" data-device-scope="on_device" aria-pressed="${state.deviceScope === "on_device"}">On device <span>${inventory.present_games.toLocaleString()}</span></button>
      <button class="scope-button ${state.deviceScope === "changes" ? "active" : ""}" data-device-scope="changes" aria-pressed="${state.deviceScope === "changes"}">Pending changes <span>${inventory.changes.toLocaleString()}</span></button>
      <button class="scope-button ${state.deviceScope === "all" ? "active" : ""}" data-device-scope="all" aria-pressed="${state.deviceScope === "all"}">Browse library</button>
    </div>
    ${inventory.unmatched_files ? `<p class="device-inventory-note">${deviceMetric(inventory.unmatched_files, inventory.unmatched_files === 1 ? "file does not match a library bundle" : "files do not match library bundles", "These physical files exist in the device directory, but ROMmates cannot associate their paths with games in the current library index.")}</p>` : ""}
    ${libraryToolbar(false, platformItems, platformCountSuffix)}${table}`);
  bindFilters(renderDevices);
  document.querySelector("#device-select").addEventListener("change", (event) => {
    state.deviceId = Number(event.target.value);
    state.deviceScope = "on_device";
    state.offset = 0;
    renderDevices();
  });
  document.querySelector("#deployment-mode").addEventListener("change", async (event) => {
    const select = event.target;
    select.disabled = true;
    try {
      await api(`/api/devices/${device.id}/deployment-mode`, {
        method: "PUT",
        body: JSON.stringify({ mode: select.value }),
      });
      toast(select.value === "hardlink" ? "Hardlinks preferred. Apply to convert eligible copies." : "New deployments will use independent copies.");
      await loadReferenceData();
      await renderDevices();
    } catch (error) { select.value = device.deployment_mode; select.disabled = false; toast(error.message, "error"); }
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
      content: `<p class="warning-copy"><strong>${preview.additions} ${preview.additions === 1 ? "file" : "files"}</strong> will be deployed${device.deployment_mode === "hardlink" ? " as hardlinks where supported" : " as independent copies"}, <strong>${preview.conversions} existing ${preview.conversions === 1 ? "copy" : "copies"}</strong> will be considered for conversion, and <strong>${preview.removals} managed ${preview.removals === 1 ? "file" : "files"}</strong> will be removed. If mergerfs cannot place a hardlink on the ROM's underlying filesystem, ROMmates keeps or creates a normal copy. AppleDouble and .DS_Store metadata will also be cleaned.</p>`,
      confirmLabel: "Apply device changes",
      cancelLabel: "Keep current device files",
      danger: preview.removals > 0,
    });
    if (!confirmed) return;
    try {
      const result = await requestJob(`/api/devices/${device.id}/apply`, { method: "POST" }, `Applying ${device.name}`);
      toast(`Applied ${device.name}: ${result.linked} linked, ${result.converted} converted, ${result.copied} copied, ${result.removed} removed${result.link_fallbacks ? `, ${result.link_fallbacks} link fallbacks` : ""}`);
      await loadReferenceData();
      await renderDevices();
    } catch (error) { toast(error.message, "error"); }
  });
  bindGameEvents(data, true);
  bindInfiniteScroll(data, renderDevices);
  loadArtworkImages();
}

async function renderTrash() {
  const renderVersion = beginPageRender();
  setHeading("Trash", "Restore bundles or permanently delete them.");
  const items = await navigationApi("/api/trash");
  if (!pageRenderIsCurrent(renderVersion, "trash")) return;
  if (!items.length) {
    state.trashSelected.clear();
    setViewHtml(`<div class="empty-state"><div><h2>Trash is empty</h2><p>Deleted ROM bundles remain recoverable here until you permanently delete them.</p></div></div>`);
    return;
  }
  const available = new Set(items.map((item) => item.id));
  for (const id of state.trashSelected.keys()) if (!available.has(id)) state.trashSelected.delete(id);
  setViewHtml(`<div class="table-wrap"><table><thead><tr><th class="checkbox-cell"><input type="checkbox" aria-label="Select all trash" data-trash-select-all></th><th>Game</th><th>Platform</th><th>Files</th><th>Deleted</th><th>Actions</th></tr></thead><tbody>${items.map((item) => `<tr><td class="checkbox-cell"><input type="checkbox" aria-label="Select ${escapeHtml(item.game_name)}" data-trash-select="${item.id}" ${state.trashSelected.has(item.id) ? "checked" : ""}></td><td class="name-cell"><strong>${escapeHtml(item.game_name)}</strong><span class="path-line">${escapeHtml(item.original_relpath)}</span></td><td>${escapeHtml(item.platform)}</td><td class="meta">${item.file_count}</td><td class="meta">${escapeHtml(item.deleted_at)} UTC</td><td><button class="button secondary small" data-restore="${item.id}">Restore</button> <button class="button danger-subtle small" data-purge="${item.id}" data-name="${escapeHtml(item.game_name)}">Delete permanently</button></td></tr>`).join("")}</tbody></table></div><div id="trash-bulk-slot"></div>`);
  const renderTrashBulk = () => {
    const slot = view.querySelector("#trash-bulk-slot");
    if (!slot) return;
    const count = state.trashSelected.size;
    slot.innerHTML = count ? `<div class="bulk-bar"><div><strong>${count.toLocaleString()} ${count === 1 ? "bundle" : "bundles"} selected</strong><span class="meta"> · permanent deletion cannot be undone</span></div><div class="bulk-actions"><button class="button secondary" data-clear-trash-selection>Clear selection</button><button class="button danger" data-purge-selected>Delete ${count.toLocaleString()} permanently</button></div></div>` : "";
    slot.querySelector("[data-clear-trash-selection]")?.addEventListener("click", () => {
      state.trashSelected.clear();
      view.querySelectorAll("[data-trash-select]").forEach((box) => { box.checked = false; });
      const all = view.querySelector("[data-trash-select-all]");
      if (all) all.checked = false;
      renderTrashBulk();
    });
    slot.querySelector("[data-purge-selected]")?.addEventListener("click", async () => {
      const selected = [...state.trashSelected.values()];
      const names = selected.slice(0, 12).map((item) => `<li>${escapeHtml(item.game_name)} <span class="meta">${escapeHtml(item.platform)}</span></li>`).join("");
      const confirmed = await confirmAction({
        title: `Permanently delete ${selected.length.toLocaleString()} trashed ${selected.length === 1 ? "bundle" : "bundles"}?`,
        content: `<p class="warning-copy">This erases every selected bundle in one job and cannot be undone.</p><ul class="confirm-list">${names}${selected.length > 12 ? `<li>and ${(selected.length - 12).toLocaleString()} more</li>` : ""}</ul>`,
        confirmLabel: `Delete ${selected.length.toLocaleString()} permanently`,
        cancelLabel: "Keep in trash",
        danger: true,
      });
      if (!confirmed) return;
      try {
        const result = await requestJob("/api/trash/purge", { method: "POST", body: JSON.stringify({ trash_ids: selected.map((item) => item.id) }) }, "Permanent deletion queued");
        state.trashSelected.clear();
        toast(`Permanently deleted ${result.purged.toLocaleString()} ${result.purged === 1 ? "bundle" : "bundles"}`);
        await refreshStatus();
        await renderTrash();
      } catch (error) { toast(error.message, "error"); }
    });
  };
  const syncTrashSelectAll = () => {
    const boxes = [...view.querySelectorAll("[data-trash-select]")];
    const checked = boxes.filter((box) => box.checked).length;
    const all = view.querySelector("[data-trash-select-all]");
    if (all) { all.checked = boxes.length > 0 && checked === boxes.length; all.indeterminate = checked > 0 && checked < boxes.length; }
  };
  view.querySelectorAll("[data-trash-select]").forEach((box) => box.addEventListener("change", () => {
    const id = Number(box.dataset.trashSelect);
    const item = items.find((candidate) => candidate.id === id);
    if (box.checked) state.trashSelected.set(id, item); else state.trashSelected.delete(id);
    syncTrashSelectAll();
    renderTrashBulk();
  }));
  view.querySelector("[data-trash-select-all]")?.addEventListener("change", (event) => {
    items.forEach((item) => { if (event.target.checked) state.trashSelected.set(item.id, item); else state.trashSelected.delete(item.id); });
    view.querySelectorAll("[data-trash-select]").forEach((box) => { box.checked = event.target.checked; });
    renderTrashBulk();
  });
  syncTrashSelectAll();
  renderTrashBulk();
  view.querySelectorAll("[data-restore]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const result = await requestJob(`/api/trash/${button.dataset.restore}/restore`, { method: "POST" }, "Restore queued");
      state.trashSelected.delete(Number(button.dataset.restore));
      toast(`Restored ${result.restored}`);
      await refreshStatus(); await loadReferenceData(); await renderTrash();
    } catch (error) { toast(error.message, "error"); }
  }));
  view.querySelectorAll("[data-purge]").forEach((button) => button.addEventListener("click", async () => {
    const confirmed = await confirmAction({ title: `Permanently delete “${button.dataset.name}”?`, content: `<p class="warning-copy">This erases the trashed bundle and cannot be undone.</p>`, confirmLabel: "Delete permanently", cancelLabel: "Keep in trash", danger: true });
    if (!confirmed) return;
    try {
      const result = await requestJob(`/api/trash/${button.dataset.purge}`, { method: "DELETE" }, "Permanent deletion queued");
      state.trashSelected.delete(Number(button.dataset.purge));
      toast(`Permanently deleted ${result.purged}`);
      await refreshStatus(); await renderTrash();
    } catch (error) { toast(error.message, "error"); }
  }));
}

function saveTabs(overview) {
  const matching = overview.matching || { orphan: 0, possible: 0, ambiguous: 0 };
  const reviewCount = matching.orphan + matching.possible + matching.ambiguous;
  return `<div class="device-scope save-tabs" role="tablist" aria-label="Save management view">
    <button class="scope-button ${state.saveTab === "current" ? "active" : ""}" data-save-tab="current" role="tab" aria-selected="${state.saveTab === "current"}">Current saves</button>
    <button class="scope-button ${state.saveTab === "matches" ? "active" : ""}" data-save-tab="matches" role="tab" aria-selected="${state.saveTab === "matches"}">Save matching <span>${reviewCount.toLocaleString()}</span></button>
    <button class="scope-button ${state.saveTab === "conflicts" ? "active" : ""}" data-save-tab="conflicts" role="tab" aria-selected="${state.saveTab === "conflicts"}">Conflicts <span>${(overview.conflicts?.total || 0).toLocaleString()}</span></button>
    <button class="scope-button ${state.saveTab === "snapshots" ? "active" : ""}" data-save-tab="snapshots" role="tab" aria-selected="${state.saveTab === "snapshots"}">Snapshots <span>${overview.snapshot_count.toLocaleString()}</span></button>
    <button class="scope-button ${state.saveTab === "settings" ? "active" : ""}" data-save-tab="settings" role="tab" aria-selected="${state.saveTab === "settings"}">Settings</button>
  </div>`;
}

function saveConflictsHtml(data) {
  const toolbar = `<div class="toolbar"><label class="search-field"><span class="sr-only">Search save conflicts</span><input id="save-conflict-search" type="search" value="${escapeHtml(state.saveConflictSearch)}" placeholder="Search game, emulator, or device" autocomplete="off"></label></div>`;
  if (!data.available) return `${toolbar}<div class="empty-state save-empty"><div><h2>Save vault is not mounted</h2><p>Mount Emulation/saves to inspect Syncthing conflicts.</p></div></div>`;
  if (!data.items.length) {
    const history = data.history?.length
      ? `<div class="table-wrap conflict-history"><table><thead><tr><th>Recent resolution</th><th>Decision</th><th>Safety snapshot</th><th>Resolved</th></tr></thead><tbody>${data.history.map((item) => `<tr><td class="name-cell"><code class="save-path">${escapeHtml(item.canonical_relpath)}</code><span class="path-line">${escapeHtml(item.device_name || item.device_id || "Unknown device")}</span></td><td>${item.decision === "conflict" ? "Used conflict version" : "Kept current version"}</td><td>#${item.safety_snapshot_id}</td><td class="meta">${escapeHtml(item.resolved_at)} UTC</td></tr>`).join("")}</tbody></table></div>`
      : "";
    return `${toolbar}<div class="empty-state save-empty compact"><div><h2>${state.saveConflictSearch ? "No conflicts match this search" : "No unresolved save conflicts"}</h2><p>${state.saveConflictSearch ? "Try a broader search." : "If two devices edit the same save concurrently, Syncthing's preserved branch will appear here."}</p></div></div>${history}`;
  }
  const rows = data.items.map((item, index) => {
    const source = item.device_name || item.device_id || "Unknown device";
    const current = item.canonical_exists
      ? `<div><strong>Current</strong><span>${formatBytes(item.canonical_size)} · ${new Date(Number(item.canonical_mtime_ns) / 1e6).toLocaleString()}</span><code>${escapeHtml(item.canonical_sha256.slice(0, 12))}</code></div>`
      : `<div><strong>Current missing</strong><span>The conflict is the only surviving version.</span></div>`;
    const conflict = `<div><strong>Conflict from ${escapeHtml(source)}</strong><span>${formatBytes(item.conflict_size)} · ${new Date(Number(item.conflict_mtime_ns) / 1e6).toLocaleString()}</span><code>${escapeHtml(item.conflict_sha256.slice(0, 12))}</code></div>`;
    return `<tr><td class="name-cell"><strong>${escapeHtml(item.content_name || item.canonical_relpath.split("/").pop())}</strong><code class="save-path">${escapeHtml(item.canonical_relpath)}</code><span class="path-line">${escapeHtml(item.emulator)}${item.core ? ` · ${escapeHtml(item.core)}` : ""} · conflict recorded ${escapeHtml(item.conflict_at)}</span></td><td><div class="conflict-versions">${current}${conflict}</div></td><td><span class="badge ${item.identical ? "unique" : "exact"}">${item.identical ? "Identical content" : "Different progress"}</span></td><td><div class="bulk-actions"><button class="button secondary small" data-resolve-conflict="${index}" data-decision="current" ${item.canonical_exists ? "" : "disabled"}>${item.identical ? "Remove duplicate" : "Keep current"}</button><button class="button small" data-resolve-conflict="${index}" data-decision="conflict">${item.canonical_exists ? "Use conflict" : "Restore conflict"}</button></div></td></tr>`;
  }).join("");
  return `${toolbar}<p class="save-match-note">ROMmates never resolves conflicts automatically. Both versions are captured in a safety snapshot before the selected branch replaces the live save.</p><div class="table-wrap"><table><thead><tr><th>Save</th><th>Preserved versions</th><th>Comparison</th><th>Resolution</th></tr></thead><tbody>${rows}</tbody></table></div>${infiniteFooter(data, data.total === 1 ? "save conflict" : "save conflicts")}`;
}

function saveMatchLabel(status) {
  return { exact: "Matched", possible: "Possible match", ambiguous: "Needs choice", orphan: "No ROM found", none: "No saves" }[status] || status;
}

function saveMatchClass(status) {
  return status === "exact" ? "unique" : status === "orphan" ? "exact" : "possible";
}

function saveMatchesHtml(data) {
  const summary = data.summary || { orphan: 0, possible: 0, ambiguous: 0, exact: 0 };
  const toolbar = `<div class="toolbar save-match-toolbar">
    <label class="search-field"><span class="sr-only">Search unmatched saves</span><input id="save-match-search" type="search" value="${escapeHtml(state.saveMatchSearch)}" placeholder="Search save name, core, or path" autocomplete="off"></label>
    <label><span class="sr-only">Match status</span><select id="save-match-status">
      <option value="all" ${state.saveMatchStatus === "all" ? "selected" : ""}>All to review</option>
      <option value="orphan" ${state.saveMatchStatus === "orphan" ? "selected" : ""}>No ROM found (${summary.orphan})</option>
      <option value="possible" ${state.saveMatchStatus === "possible" ? "selected" : ""}>Possible (${summary.possible})</option>
      <option value="ambiguous" ${state.saveMatchStatus === "ambiguous" ? "selected" : ""}>Needs choice (${summary.ambiguous})</option>
    </select></label>
  </div>`;
  if (!data.available) return `${toolbar}<div class="empty-state save-empty"><div><h2>Save vault is not mounted</h2><p>ROMmates needs the shared Emulation/saves directory to match filename-based saves against the ROM library.</p></div></div>`;
  if (!data.items.length) return `${toolbar}<div class="empty-state save-empty"><div><h2>No save matches need review</h2><p>${state.saveMatchSearch || state.saveMatchStatus !== "all" ? "Try broader filters." : "Every recognized save and state group has one exact ROM match."}</p></div></div>`;
  const rows = data.items.map((item, index) => {
    const candidates = item.games?.length
      ? item.games.map((game) => `${escapeHtml(game.name)} <span class="meta">(${escapeHtml(game.platform)})</span>`).join("<br>")
      : '<span class="meta">None</span>';
    const paths = item.files.map((file) => `<li><code class="save-path">${escapeHtml(file.relpath)}</code></li>`).join("");
    return `<tr><td class="name-cell"><strong>${escapeHtml(item.content_name)}</strong><span class="path-line">${escapeHtml(item.core || "No core directory")}</span><details class="save-paths"><summary>${item.files.length} ${item.files.length === 1 ? "file" : "files"}</summary><ul>${paths}</ul></details></td><td><span class="badge ${saveMatchClass(item.status)}">${saveMatchLabel(item.status)}</span></td><td>${candidates}</td><td class="meta">${item.save_files} saves · ${item.state_files} states<br>${formatBytes(item.bytes)}</td><td class="meta">${new Date(Number(item.latest_mtime_ns) / 1e6).toLocaleString()}</td><td>${item.status === "orphan" ? `<button class="button danger-subtle small" data-delete-save-group="${index}">Review delete</button>` : '<span class="meta">Resolve match first</span>'}</td></tr>`;
  }).join("");
  return `${toolbar}<p class="save-match-note">ROMmates matches filename-based saves from RetroArch and compatible standalone emulators. Identifier-based saves such as Dolphin and Ryujinx remain protected and are not labeled as orphans.</p><div class="table-wrap"><table><thead><tr><th>Save content name</th><th>Status</th><th>Candidate ROM</th><th>Files</th><th>Last changed</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table></div>${infiniteFooter(data, data.total === 1 ? "save group" : "save groups")}`;
}

function saveHeader(overview) {
  const settings = overview.settings;
  const latest = overview.latest_snapshot;
  return `<div class="save-strip">
    <div class="save-source"><div><span class="badge ${settings.available ? "unique" : "exact"}">${settings.available ? "Source available" : "Source unavailable"}</span><strong>Syncthing save vault</strong></div><code title="${escapeHtml(settings.source_root)}">${escapeHtml(settings.source_root)}</code><span class="meta">${latest ? `Last snapshot ${escapeHtml(latest.created_at)} UTC` : "No snapshots yet"}</span></div>
    <form class="snapshot-now" data-snapshot-form><label class="field"><span>Snapshot note (optional)</span><input class="input" name="note" maxlength="500" placeholder="Before a long trip, before testing a core…"></label><button class="button" ${settings.available ? "" : "disabled"}>Snapshot now</button></form>
  </div>`;
}

function currentSavesHtml(data, inventory) {
  const toolbar = `<div class="toolbar"><label class="search-field"><span class="sr-only">Search current saves</span><input id="save-search" type="search" value="${escapeHtml(state.saveSearch)}" placeholder="Search save paths" autocomplete="off"></label></div>`;
  if (!data.available) {
    return `${toolbar}<div class="empty-state save-empty"><div><h2>Save vault is not mounted</h2><p>Mount the Emulation directory and ensure it contains the top-level saves folder, then refresh ROMmates.</p></div></div>`;
  }
  const emulatorSummary = inventory?.emulators?.length
    ? `<div class="save-emulator-grid">${inventory.emulators.map((item) => `<div class="save-emulator-card"><strong>${escapeHtml(item.emulator)}</strong><span>${item.files.toLocaleString()} files · ${formatBytes(item.bytes)}</span><small>${item.save_files.toLocaleString()} saves · ${item.state_files.toLocaleString()} states</small></div>`).join("")}</div>`
    : "";
  if (!data.items.length) {
    return `${emulatorSummary}${toolbar}<div class="empty-state save-empty"><div><h2>${state.saveSearch ? "No saves match this search" : "No save files found"}</h2><p>${state.saveSearch ? "Try part of the filename, emulator, or relative directory." : "No emulator save data has arrived through Syncthing yet."}</p></div></div>`;
  }
  return `${emulatorSummary}${toolbar}<div class="table-wrap"><table><thead><tr><th>Save path</th><th>Emulator</th><th>Type</th><th>Size</th><th>Modified</th></tr></thead><tbody>${data.items.map((item) => `<tr><td class="name-cell"><code class="save-path">${escapeHtml(item.relpath)}</code>${item.core ? `<span class="path-line">${escapeHtml(item.core)}</span>` : ""}</td><td>${escapeHtml(item.emulator)}</td><td><span class="badge ${item.kind === "save" ? "unique" : item.kind === "state" ? "possible" : "cancelled"}">${escapeHtml(item.kind)}</span></td><td class="meta">${formatBytes(item.size)}</td><td class="meta">${new Date(Number(item.mtime_ns) / 1e6).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>${infiniteFooter(data, data.total === 1 ? "vault file" : "vault files")}`;
}

function snapshotChangeSummary(snapshot) {
  if (!snapshot.added_count && !snapshot.changed_count && !snapshot.removed_count) return "No content changes";
  return `+${snapshot.added_count} · ~${snapshot.changed_count} · −${snapshot.removed_count}`;
}

function snapshotDetailHtml(detail, comparison) {
  const snapshot = detail.snapshot;
  const sourceAvailable = Boolean(comparison && comparison.compatible !== false);
  const files = detail.files.length
    ? `<div class="table-wrap snapshot-files"><table><thead><tr><th>Historical file</th><th>Size</th><th>Action</th></tr></thead><tbody>${detail.files.map((item) => `<tr><td class="name-cell"><code class="save-path">${escapeHtml(item.relpath)}</code></td><td class="meta">${formatBytes(item.size)}</td><td><button class="button secondary small" data-download-save="${escapeHtml(item.relpath)}">Download</button></td></tr>`).join("")}</tbody></table></div>${infiniteFooter({ items: detail.files, total: detail.total }, detail.total === 1 ? "historical file" : "historical files")}`
    : `<p class="report-empty">This snapshot contains no files.</p>`;
  const changes = comparison ? [
    ...comparison.restore.map((path) => ["Restore missing", path]),
    ...comparison.overwrite.map((path) => ["Overwrite current", path]),
    ...comparison.delete.map((path) => ["Delete current", path]),
  ] : [];
  const changeReview = !sourceAvailable
    ? `<p class="issue-warning" role="note">${escapeHtml(comparison?.reason || "The live save source is unavailable.")} Historical files can still be inspected and downloaded, but comparison and restore are disabled.</p>`
    : changes.length
    ? `<details class="restore-changes"><summary>Review all ${changes.length.toLocaleString()} filesystem changes</summary><div class="table-wrap"><table><thead><tr><th>Action</th><th>Path</th></tr></thead><tbody>${changes.map(([action, path]) => `<tr><td>${escapeHtml(action)}</td><td><code class="save-path">${escapeHtml(path)}</code></td></tr>`).join("")}</tbody></table></div></details>`
    : `<p class="report-empty">The live cloud state already matches this snapshot.</p>`;
  const fileSearch = `<div class="toolbar snapshot-search"><label class="search-field"><span class="sr-only">Search snapshot files</span><input id="snapshot-search" type="search" value="${escapeHtml(state.saveSnapshotSearch)}" placeholder="Search files in this snapshot" autocomplete="off"></label></div>`;
  return `<section class="snapshot-detail" aria-labelledby="snapshot-detail-title"><div class="section-heading report-heading"><div><h2 id="snapshot-detail-title">Snapshot #${snapshot.id}</h2><p>${escapeHtml(snapshot.created_at)} UTC · ${escapeHtml(snapshot.trigger)}${snapshot.note ? ` · ${escapeHtml(snapshot.note)}` : ""}</p></div><button class="button secondary small" data-close-snapshot>Close</button></div><dl class="report-grid report-summary"><div><dt>Files</dt><dd>${snapshot.file_count.toLocaleString()}</dd></div><div><dt>Logical size</dt><dd>${formatBytes(snapshot.logical_bytes)}</dd></div><div><dt>New storage</dt><dd>${formatBytes(snapshot.new_bytes)}</dd></div><div><dt>Restore missing</dt><dd>${sourceAvailable ? comparison.restore.length.toLocaleString() : "Unavailable"}</dd></div><div><dt>Overwrite</dt><dd>${sourceAvailable ? comparison.overwrite.length.toLocaleString() : "Unavailable"}</dd></div><div><dt>Delete current</dt><dd>${sourceAvailable ? comparison.delete.length.toLocaleString() : "Unavailable"}</dd></div></dl>${changeReview}<div class="restore-panel"><div><h3>Restore this complete vault state</h3><p>ROMmates will first make a safety snapshot, then restore the entire shared save tree. If live files change after this comparison, the job will stop.</p><label class="restore-check"><input type="checkbox" data-retroarch-closed ${sourceAvailable ? "" : "disabled"}> <span>I closed every emulator on every device and let Syncthing finish.</span></label></div><button class="button danger" data-restore-snapshot ${sourceAvailable && changes.length ? "" : "disabled"}>Review restore</button></div><div class="section-heading snapshot-file-heading"><div><h3>Files in this snapshot</h3><p>Historical files can be downloaded without changing the live save vault.</p></div></div>${fileSearch}${files}</section>`;
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
  const renderVersion = beginPageRender();
  setHeading("Saves", "Inspect, version, and restore the shared emulator save vault.");
  const overview = await navigationApi("/api/saves");
  let content = "";
  let currentData = null;
  let snapshotData = null;
  let snapshotDetail = null;
  let comparison = null;
  let matchData = null;
  let conflictData = null;
  if (state.saveTab === "current") {
    const params = new URLSearchParams({ search: state.saveSearch, limit: 250, offset: state.saveOffset });
    const response = await navigationApi(`/api/saves/current?${params}`);
    currentData = mergeInfinitePage(`saves-current\u001f${state.saveSearch}`, response, (item) => item.relpath);
    content = currentSavesHtml(currentData, overview.inventory);
  } else if (state.saveTab === "snapshots") {
    snapshotData = await navigationApi("/api/saves/snapshots?limit=100");
    if (state.saveSnapshotId && snapshotData.items.some((item) => item.id === state.saveSnapshotId)) {
      const params = new URLSearchParams({ search: state.saveSnapshotSearch, limit: 250, offset: state.saveSnapshotOffset });
      if (overview.settings.available) {
        [snapshotDetail, comparison] = await Promise.all([
          navigationApi(`/api/saves/snapshots/${state.saveSnapshotId}?${params}`),
          navigationApi(`/api/saves/snapshots/${state.saveSnapshotId}/compare`),
        ]);
      } else {
        snapshotDetail = await navigationApi(`/api/saves/snapshots/${state.saveSnapshotId}?${params}`);
      }
      const files = mergeInfinitePage(
        `snapshot-files\u001f${state.saveSnapshotId}\u001f${state.saveSnapshotSearch}`,
        { items: snapshotDetail.files, total: snapshotDetail.total, offset: snapshotDetail.offset, limit: snapshotDetail.limit },
        (item) => item.relpath,
      );
      snapshotDetail = { ...snapshotDetail, files: files.items, offset: 0 };
    } else {
      state.saveSnapshotId = null;
    }
    content = snapshotsHtml(snapshotData, snapshotDetail, comparison);
  } else if (state.saveTab === "matches") {
    const params = new URLSearchParams({ search: state.saveMatchSearch, status: state.saveMatchStatus, limit: 200, offset: state.saveMatchOffset });
    const response = await navigationApi(`/api/saves/unmatched?${params}`);
    matchData = mergeInfinitePage(
      `save-matches\u001f${state.saveMatchSearch}\u001f${state.saveMatchStatus}`,
      response,
      (item) => item.key,
    );
    content = saveMatchesHtml(matchData);
  } else if (state.saveTab === "conflicts") {
    const params = new URLSearchParams({ search: state.saveConflictSearch, limit: 100, offset: state.saveConflictOffset });
    const response = await navigationApi(`/api/saves/conflicts?${params}`);
    conflictData = mergeInfinitePage(
      `save-conflicts\u001f${state.saveConflictSearch}`,
      response,
      (item) => item.conflict_relpath,
    );
    conflictData.history = response.history;
    conflictData.identical = response.identical;
    content = saveConflictsHtml(conflictData);
  } else {
    content = saveSettingsHtml(overview.settings);
  }
  if (!pageRenderIsCurrent(renderVersion, "saves")) return;
  setViewHtml(`${saveHeader(overview)}${saveTabs(overview)}${content}`);
  view.querySelectorAll("[data-save-tab]").forEach((button) => button.addEventListener("click", () => {
    state.saveTab = button.dataset.saveTab;
    state.saveOffset = 0;
    state.saveMatchOffset = 0;
    state.saveConflictOffset = 0;
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
  const saveMatchSearch = view.querySelector("#save-match-search");
  let saveMatchSearchTimer;
  saveMatchSearch?.addEventListener("input", (event) => {
    clearTimeout(saveMatchSearchTimer);
    saveMatchSearchTimer = setTimeout(() => {
      state.saveMatchSearch = event.target.value;
      state.saveMatchOffset = 0;
      renderSaves();
    }, 220);
  });
  view.querySelector("#save-match-status")?.addEventListener("change", (event) => {
    state.saveMatchStatus = event.target.value;
    state.saveMatchOffset = 0;
    renderSaves();
  });
  const conflictSearch = view.querySelector("#save-conflict-search");
  let conflictSearchTimer;
  conflictSearch?.addEventListener("input", (event) => {
    clearTimeout(conflictSearchTimer);
    conflictSearchTimer = setTimeout(() => {
      state.saveConflictSearch = event.target.value;
      state.saveConflictOffset = 0;
      renderSaves();
    }, 220);
  });
  view.querySelectorAll("[data-resolve-conflict]").forEach((button) => button.addEventListener("click", async () => {
    const item = conflictData?.items[Number(button.dataset.resolveConflict)];
    if (!item) return;
    const decision = button.dataset.decision;
    const chosen = decision === "conflict" ? `the conflict from ${item.device_name || item.device_id || "the other device"}` : "the current version";
    const warning = item.identical
      ? "The files have identical content, so this only removes Syncthing's extra conflict copy."
      : `The unselected branch will leave the live vault, but both versions remain recoverable in the safety snapshot.`;
    const confirmed = await confirmAction({
      title: `${decision === "conflict" ? "Use conflict version" : "Keep current version"}?`,
      content: `<p class="warning-copy">ROMmates will select <strong>${escapeHtml(chosen)}</strong> for <code>${escapeHtml(item.canonical_relpath)}</code>.</p><p>${escapeHtml(warning)}</p><p>Close the emulator on every device and let Syncthing finish before continuing.</p>`,
      confirmLabel: item.identical ? "Remove duplicate" : `Use ${decision === "conflict" ? "conflict" : "current"}`,
      cancelLabel: "Leave unresolved",
      danger: !item.identical,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await requestJob("/api/saves/conflicts/resolve", {
        method: "POST",
        body: JSON.stringify({
          conflict_relpath: item.conflict_relpath,
          decision,
          expected_canonical_sha256: item.canonical_sha256,
          expected_conflict_sha256: item.conflict_sha256,
          device_id: item.device_id,
          device_name: item.device_name,
        }),
      }, "Creating conflict safety snapshot");
      toast(`Conflict resolved; safety snapshot #${result.safety_snapshot_id}`);
      state.saveConflictOffset = 0;
      await refreshStatus();
      await renderSaves();
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  }));
  view.querySelectorAll("[data-delete-save-group]").forEach((button) => button.addEventListener("click", async () => {
    const group = matchData?.items[Number(button.dataset.deleteSaveGroup)];
    if (!group || group.status !== "orphan") return;
    const paths = group.files.map((file) => `<li><code>${escapeHtml(file.relpath)}</code> (${formatBytes(file.size)})</li>`).join("");
    const confirmed = await confirmAction({
      title: `Delete orphan saves for “${group.content_name}”?`,
      content: `<p class="warning-copy">ROMmates will create a full safety snapshot, then delete <strong>${group.files.length} ${group.files.length === 1 ? "file" : "files"}</strong> from the shared save vault. Restoring the safety snapshot restores the complete vault state, not only this group.</p><ul class="confirm-list">${paths}</ul>`,
      confirmLabel: `Snapshot and delete ${group.files.length}`,
      cancelLabel: "Keep saves",
      danger: true,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await requestJob(
        "/api/saves/orphans/delete",
        { method: "POST", body: JSON.stringify({ group_key: group.key }) },
        "Creating safety snapshot",
      );
      toast(`Deleted ${result.files} orphan save files; safety snapshot #${result.safety_snapshot_id} is available`);
      await refreshStatus();
      await renderSaves();
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
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
  view.querySelectorAll("[data-download-save]").forEach((button) => button.addEventListener("click", async () => {
    const relpath = button.dataset.downloadSave;
    const encoded = relpath.split("/").map(encodeURIComponent).join("/");
    try { await downloadApiFile(`/api/saves/snapshots/${state.saveSnapshotId}/files/${encoded}`, relpath.split("/").pop()); }
    catch (error) { toast(error.message, "error"); }
  }));
  view.querySelector("[data-restore-snapshot]")?.addEventListener("click", async () => {
    if (!comparison) return;
    if (!view.querySelector("[data-retroarch-closed]")?.checked) {
      toast("Confirm that every emulator is closed and Syncthing is finished first", "error");
      return;
    }
    const confirmed = await confirmAction({
      title: `Restore save snapshot #${state.saveSnapshotId}?`,
      content: `<p class="warning-copy">ROMmates will create a safety snapshot, then overwrite <strong>${comparison.overwrite.length}</strong>, restore <strong>${comparison.restore.length}</strong>, and remove <strong>${comparison.delete.length}</strong> current files from the shared vault.</p>`,
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
  if (state.saveTab === "current" && currentData) {
    bindInfiniteScroll(currentData, renderSaves, (offset) => { state.saveOffset = offset; });
  } else if (state.saveTab === "matches" && matchData) {
    bindInfiniteScroll(matchData, renderSaves, (offset) => { state.saveMatchOffset = offset; });
  } else if (state.saveTab === "conflicts" && conflictData) {
    bindInfiniteScroll(conflictData, renderSaves, (offset) => { state.saveConflictOffset = offset; });
  } else if (state.saveTab === "snapshots" && snapshotDetail) {
    bindInfiniteScroll(
      { items: snapshotDetail.files, total: snapshotDetail.total },
      renderSaves,
      (offset) => { state.saveSnapshotOffset = offset; },
    );
  }
}

function namingConfidenceLabel(value) {
  return { exact: "Exact DAT match", strong: "Strong name match", metadata: "ScreenScraper title", cleanup: "Cleanup only" }[value] || value;
}

function saveImpactHtml(impact) {
  if (!impact || impact.status === "none") return '<span class="meta">No matched saves</span>';
  const counts = `${impact.save_files} ${impact.save_files === 1 ? "save" : "saves"} · ${impact.state_files} ${impact.state_files === 1 ? "state" : "states"}`;
  const title = (impact.paths || []).join("\n");
  return `<span class="badge ${saveMatchClass(impact.status)}" title="${escapeHtml(title)}">${saveMatchLabel(impact.status)}</span><span class="path-line">${counts}</span>`;
}

async function renderNaming() {
  const renderVersion = beginPageRender();
  setHeading("Naming", "Review canonical filenames before changing bundles.");
  const params = new URLSearchParams({
    search: state.search,
    platform: state.platform,
    confidence: state.namingConfidence,
    save_impact: state.namingSaveImpact,
    limit: state.offset ? INFINITE_CHUNK_SIZE : state.limit,
    offset: state.offset,
  });
  const [response, catalogs] = await Promise.all([navigationApi(`/api/naming/suggestions?${params}`), navigationApi("/api/naming/catalogs")]);
  if (!pageRenderIsCurrent(renderVersion, "naming")) return;
  const key = `naming\u001f${state.search}\u001f${state.platform}\u001f${state.namingConfidence}\u001f${state.namingSaveImpact}`;
  const data = mergeInfinitePage(key, response, (item) => item.game_id);
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
      <option value="metadata" ${state.namingConfidence === "metadata" ? "selected" : ""}>ScreenScraper titles</option>
      <option value="cleanup" ${state.namingConfidence === "cleanup" ? "selected" : ""}>Cleanup only</option>
    </select></label>
    <label><span class="sr-only">Save impact</span><select id="naming-save-impact">
      <option value="all" ${state.namingSaveImpact === "all" ? "selected" : ""}>All save impacts</option>
      <option value="no_saves" ${state.namingSaveImpact === "no_saves" ? "selected" : ""}>No matched saves</option>
      <option value="has_saves" ${state.namingSaveImpact === "has_saves" ? "selected" : ""}>Has save data</option>
      <option value="review" ${state.namingSaveImpact === "review" ? "selected" : ""}>Save match needs review</option>
    </select></label>
  </div>`;
  let content;
  if (!data.items.length) {
    content = `<div class="empty-state naming-empty"><div><h2>No naming suggestions</h2><p>${state.search || state.platform || state.namingConfidence !== "all" || state.namingSaveImpact !== "all" ? "Try broader filters." : "Your current filenames do not need conservative cleanup. Import a DAT catalog to find canonical matches."}</p></div></div>`;
  } else {
    content = `<div class="table-wrap"><table><thead><tr><th class="checkbox-cell"><input type="checkbox" data-naming-select-all aria-label="Select visible suggestions without matched saves"></th><th>Current filename</th><th>Suggested filename</th><th>Confidence</th><th>Save impact</th><th>Source</th></tr></thead><tbody>${data.items.map((item) => {
      const checked = state.namingSelected.has(item.game_id);
      return `<tr class="${item.collision ? "collision-row" : ""}">
        <td class="checkbox-cell"><input type="checkbox" data-naming-select="${item.game_id}" ${checked ? "checked" : ""} ${item.collision ? "disabled" : ""} aria-label="Select suggestion for ${escapeHtml(item.current_name)}"></td>
        <td class="name-cell"><strong>${escapeHtml(item.current_name)}</strong><span class="path-line">${escapeHtml(item.primary_relpath)}</span></td>
        <td class="suggestion-cell"><input class="input suggestion-input" data-suggestion-name="${item.game_id}" value="${escapeHtml(state.namingSelected.get(item.game_id)?.name || item.suggested_name)}" maxlength="255" ${item.collision ? "disabled" : ""}>${item.collision ? `<span class="collision-note">${escapeHtml(item.collision_detail || "A file with this name already exists")}</span>` : ""}</td>
        <td><span class="badge naming-${item.confidence}">${escapeHtml(namingConfidenceLabel(item.confidence))}</span></td>
        <td>${saveImpactHtml(item.save_impact)}</td>
        <td class="meta">${escapeHtml(item.source)}</td>
      </tr>`;
    }).join("")}</tbody></table></div>${infiniteFooter(data, data.total === 1 ? "suggestion" : "suggestions")}`;
  }
  const selectedCount = state.namingSelected.size;
  const bulk = selectedCount ? `<div class="bulk-bar"><div><strong>${selectedCount} selected</strong><span class="meta"> · bundle-aware rename</span></div><div class="bulk-actions"><button class="button secondary" data-clear-naming>Clear</button><button class="button" data-apply-naming>Review and apply</button></div></div>` : "";
  setViewHtml(`${importer}${toolbar}${content}${bulk}`);

  bindFilters(renderNaming);
  view.querySelector("#naming-confidence")?.addEventListener("change", (event) => { state.namingConfidence = event.target.value; state.offset = 0; renderNaming(); });
  view.querySelector("#naming-save-impact")?.addEventListener("change", (event) => { state.namingSaveImpact = event.target.value; state.offset = 0; renderNaming(); });
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
    if (selected && item) state.namingSelected.set(id, { name: input?.value || item.suggested_name, current: item.current_name, saveImpact: item.save_impact });
    else state.namingSelected.delete(id);
  };
  view.querySelectorAll("[data-naming-select]").forEach((box) => box.addEventListener("change", () => { selectSuggestion(Number(box.dataset.namingSelect), box.checked); renderNaming(); }));
  view.querySelectorAll("[data-suggestion-name]").forEach((input) => input.addEventListener("input", () => {
    const id = Number(input.dataset.suggestionName);
    if (state.namingSelected.has(id)) state.namingSelected.get(id).name = input.value;
  }));
  view.querySelector("[data-naming-select-all]")?.addEventListener("change", (event) => {
    data.items.filter((item) => !item.collision && item.save_impact?.status === "none").forEach((item) => selectSuggestion(item.game_id, event.target.checked));
    renderNaming();
  });
  view.querySelector("[data-clear-naming]")?.addEventListener("click", () => { state.namingSelected.clear(); renderNaming(); });
  view.querySelector("[data-apply-naming]")?.addEventListener("click", async () => {
    const items = [...state.namingSelected.entries()].map(([game_id, item]) => ({ game_id, name: item.name }));
    const saveAffected = [...state.namingSelected.values()].filter((item) => item.saveImpact?.status && item.saveImpact.status !== "none");
    const preview = items.slice(0, 12).map((item) => `<li>${escapeHtml(state.namingSelected.get(item.game_id).current)} → <strong>${escapeHtml(item.name)}</strong></li>`).join("");
    const confirmed = await confirmAction({ title: `Apply ${items.length} naming ${items.length === 1 ? "suggestion" : "suggestions"}?`, content: `<p class="warning-copy">ROMmates will rename complete file or folder bundles and update CUE, GDI, and M3U references. Existing device selections stay attached.</p>${saveAffected.length ? `<p class="issue-warning"><strong>${saveAffected.length} selected ${saveAffected.length === 1 ? "game has" : "games have"} matching save data.</strong> This job renames ROMs only; RetroArch save and state filenames will not change.</p>` : ""}<ul class="confirm-list">${preview}${items.length > 12 ? `<li>and ${items.length - 12} more</li>` : ""}</ul>`, confirmLabel: "Apply renames", cancelLabel: "Keep reviewing", danger: false });
    if (!confirmed) return;
    try {
      const result = await requestJob("/api/naming/apply", { method: "POST", body: JSON.stringify({ items }) }, "Naming changes queued");
      state.namingSelected.clear();
      toast(`Renamed ${result.renamed} ${result.renamed === 1 ? "bundle" : "bundles"}`);
      await refreshStatus(); await loadReferenceData(); await renderNaming();
    } catch (error) { toast(error.message, "error"); }
  });
  bindInfiniteScroll(data, renderNaming);
}

async function renderJobs() {
  const renderVersion = beginPageRender();
  setHeading("Jobs", "Detailed reports for scans and filesystem activity.");
  const [jobs, activity] = await Promise.all([navigationApi("/api/jobs", { cacheTtl: 5_000 }), navigationApi("/api/activity", { cacheTtl: 5_000 })]);
  if (state.jobReportId && !jobs.some((job) => job.id === state.jobReportId)) state.jobReportId = null;
  let report = null;
  let issues = null;
  if (state.jobReportId) {
    const [jobReport, issueResponse] = await Promise.all([
      api(`/api/jobs/${state.jobReportId}`),
      api(`/api/jobs/${state.jobReportId}/issues?limit=250&offset=${state.jobIssueOffset}`),
    ]);
    report = jobReport;
    issues = mergeInfinitePage(
      `job-issues\u001f${state.jobReportId}`,
      issueResponse,
      (item) => item.id,
    );
  }
  if (!pageRenderIsCurrent(renderVersion, "jobs")) return;
  const jobsHtml = jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Status</th><th>Detail</th><th>Started</th><th>Finished</th><th>Action</th></tr></thead><tbody>${jobs.map((job) => `<tr${state.jobReportId === job.id ? ` class="selected-row"` : ""}><td>${escapeHtml(job.kind)}</td><td><span class="badge ${job.status === "failed" ? "exact" : job.status === "complete" ? "unique" : job.status === "cancelled" ? "cancelled" : "possible"}">${escapeHtml(job.status)}${["running", "paused", "cancelling"].includes(job.status) ? ` · ${job.progress}%` : ""}</span></td><td class="name-cell">${escapeHtml(job.detail)}</td><td class="meta">${escapeHtml(job.created_at)}</td><td class="meta">${escapeHtml(job.completed_at || "In progress")}</td><td><div class="bulk-actions"><button class="button secondary small" data-job-report="${job.id}" aria-expanded="${state.jobReportId === job.id}">${state.jobReportId === job.id ? "Close" : "Report"}${job.reported_issue_count ? ` · ${job.reported_issue_count} issues` : ""}</button>${job.cancellable ? `<button class="button danger-subtle small" data-cancel-job="${job.id}" ${job.status === "cancelling" ? "disabled" : ""}>${job.status === "cancelling" ? "Stopping…" : "Stop"}</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty-state"><div><h2>No jobs yet</h2><p>Library scans will appear here.</p></div></div>`;
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
  view.querySelector("[data-copy-issues]")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(issues.items.map((item) => item.detail).join("\n"));
      toast(`Copied ${issues.items.length} issue paths`);
    } catch {
      toast("The browser could not copy the issue list", "error");
    }
  });
  if (issues) {
    bindInfiniteScroll(issues, renderJobs, (offset) => { state.jobIssueOffset = offset; });
  }
}

async function renderNotifications() {
  const renderVersion = beginPageRender();
  setHeading("Notifications", "Choose what ROMmates sends to Discord.");
  const data = await api("/api/notifications");
  if (!pageRenderIsCurrent(renderVersion, "notifications")) return;
  const configured = data.configured;
  const eventChoices = data.events.map((event) => `<label class="notification-event"><input type="checkbox" name="${escapeHtml(event.key)}" ${event.enabled ? "checked" : ""}><span><strong>${escapeHtml(event.label)}</strong><small>${escapeHtml(event.description)}</small></span></label>`).join("");
  const setup = configured
    ? `<div class="notification-connection connected"><span class="status-dot"></span><div><strong>Discord webhook connected</strong><p>${escapeHtml(data.webhook_hint)}${data.public_url ? ` · Links open ${escapeHtml(data.public_url)}` : " · Set ROMMATES_PUBLIC_URL to include links back to ROMmates."}</p></div></div>`
    : `<div class="notification-connection"><span class="status-dot"></span><div><strong>Discord webhook not configured</strong><p>Create a channel webhook, set <code>ROMMATES_DISCORD_WEBHOOK_URL</code> in your <code>.env</code>, expose it in Compose, then recreate the container. Set <code>ROMMATES_PUBLIC_URL</code> to add links back to ROMmates.</p></div></div>`;
  const deliveries = data.deliveries.length
    ? `<div class="table-wrap"><table><thead><tr><th>Event</th><th>Notification</th><th>Status</th><th>Attempts</th><th>Created</th></tr></thead><tbody>${data.deliveries.map((item) => `<tr><td>${escapeHtml(item.event.replaceAll("_", " "))}</td><td class="name-cell"><strong>${escapeHtml(item.title)}</strong>${item.error ? `<small class="notification-error">${escapeHtml(item.error)}</small>` : ""}</td><td><span class="badge ${item.status === "sent" ? "unique" : item.status === "failed" ? "exact" : "possible"}">${escapeHtml(item.status)}</span></td><td>${Number(item.attempts).toLocaleString()}</td><td class="meta">${escapeHtml(item.created_at)} UTC</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty-state compact"><div><h2>No deliveries yet</h2><p>Send a test or wait for an enabled event.</p></div></div>`;
  setViewHtml(`${setup}<form class="notification-settings" data-notification-settings><div class="settings-section"><h2>Delivery</h2><p>Notifications are queued in the background. Discord outages never block a ROM, save, or device operation.</p><label class="device-choice"><input type="checkbox" name="enabled" ${data.enabled ? "checked" : ""}><span>Enable Discord notifications</span></label></div><div class="settings-section notification-events-section"><h2>Events</h2><p>Failures, uploads, and save conflicts are enabled by default. High-volume completion events are opt-in.</p><div class="notification-event-list">${eventChoices}</div></div><div class="notification-actions"><button class="button" type="submit">Save preferences</button><button class="button secondary" type="button" data-test-notification ${configured ? "" : "disabled"}>Send test</button></div></form><div class="section-heading"><div><h2>Recent deliveries</h2><p>The most recent 50 attempts, including delivery failures.</p></div></div>${deliveries}`);
  view.querySelector("[data-notification-settings]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const events = Object.fromEntries(data.events.map((item) => [item.key, form.elements[item.key].checked]));
    try {
      await api("/api/notifications/settings", { method: "PUT", body: JSON.stringify({ enabled: form.elements.enabled.checked, events }) });
      toast("Notification preferences saved");
      await renderNotifications();
    } catch (error) { toast(error.message, "error"); }
  });
  view.querySelector("[data-test-notification]")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      await api("/api/notifications/test", { method: "POST" });
      toast("Test notification queued");
      window.setTimeout(() => { if (state.view === "notifications") renderNotifications(); }, 1200);
    } catch (error) {
      event.currentTarget.disabled = false;
      toast(error.message, "error");
    }
  });
}

async function renderUsers() {
  const renderVersion = beginPageRender();
  setHeading("Users", "Control who can browse, transfer, or administer ROMmates.");
  const data = await api("/api/users");
  if (!pageRenderIsCurrent(renderVersion, "users")) return;
  const roleCopy = {
    viewer: "Browse and download ROMs",
    contributor: "Browse, download, and submit staged uploads",
    admin: "Full library, device, save, cleanup, and user access",
  };
  const rows = data.items.map((user) => `<tr><td class="name-cell"><strong>${escapeHtml(user.display_name)}</strong><span class="path-line">${escapeHtml(user.username)}${state.principal?.id === user.id ? " · You" : ""}</span></td><td><select data-user-role="${user.id}" aria-label="Role for ${escapeHtml(user.username)}">${data.roles.map((role) => `<option value="${role}" ${user.role === role ? "selected" : ""}>${escapeHtml(role)}</option>`).join("")}</select><span class="path-line">${escapeHtml(roleCopy[user.role] || "")}</span></td><td><label class="device-choice compact"><input type="checkbox" data-user-active="${user.id}" ${user.active ? "checked" : ""}><span>${user.active ? "Active" : "Disabled"}</span></label>${user.must_change_password ? '<span class="path-line credential-pending">Password change required</span>' : ""}</td><td class="meta">${escapeHtml(user.last_login_at || "Never")}</td><td><form class="inline-password" data-user-password-form="${user.id}"><input class="input" name="password" type="password" required minlength="12" autocomplete="new-password" placeholder="Temporary password" aria-label="Temporary password for ${escapeHtml(user.username)}"><button class="button secondary small">Reset</button></form></td></tr>`).join("");
  setViewHtml(`<section class="user-create"><div><h2>Add account</h2><p>Set a temporary password of at least 12 characters. The new user must replace it at first login.</p></div><form class="user-create-form" id="user-create-form"><label class="field"><span>Username</span><input class="input" name="username" required maxlength="64" autocomplete="off"></label><label class="field"><span>Display name</span><input class="input" name="display_name" maxlength="100" autocomplete="off"></label><label class="field"><span>Temporary password</span><input class="input" name="password" type="password" required minlength="12" autocomplete="new-password"></label><label class="field"><span>Role</span><select name="role"><option value="viewer">Viewer</option><option value="contributor">Contributor</option><option value="admin">Admin</option></select></label><button class="button" type="submit">Create account</button></form></section><div class="section-heading"><div><h2>Accounts</h2><p>Role and status changes take effect on the next request. Password resets require another change at login.</p></div><span class="meta">${data.items.length.toLocaleString()} total</span></div><div class="table-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last login</th><th>Credentials</th></tr></thead><tbody>${rows}</tbody></table></div>`);
  view.querySelector("#user-create-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/users", { method: "POST", body: JSON.stringify(Object.fromEntries(form)) });
      toast("Account created");
      await renderUsers();
    } catch (error) { toast(error.message, "error"); }
  });
  view.querySelectorAll("[data-user-role]").forEach((select) => select.addEventListener("change", async (event) => {
    try {
      await api(`/api/users/${event.currentTarget.dataset.userRole}`, { method: "PATCH", body: JSON.stringify({ role: event.currentTarget.value }) });
      toast("Role updated");
      await renderUsers();
    } catch (error) { toast(error.message, "error"); await renderUsers(); }
  }));
  view.querySelectorAll("[data-user-active]").forEach((input) => input.addEventListener("change", async (event) => {
    try {
      await api(`/api/users/${event.currentTarget.dataset.userActive}`, { method: "PATCH", body: JSON.stringify({ active: event.currentTarget.checked }) });
      toast(event.currentTarget.checked ? "Account enabled" : "Account disabled");
      await renderUsers();
    } catch (error) { toast(error.message, "error"); await renderUsers(); }
  }));
  view.querySelectorAll("[data-user-password-form]").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const password = new FormData(event.currentTarget).get("password");
    try {
      await api(`/api/users/${event.currentTarget.dataset.userPasswordForm}`, { method: "PATCH", body: JSON.stringify({ password }) });
      toast("Temporary password set; existing sessions were signed out");
      event.currentTarget.reset();
      await renderUsers();
    } catch (error) { toast(error.message, "error"); }
  }));
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

function formatElapsed(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes}m ${value % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function renderScanTelemetry(telemetry) {
  if (!telemetry || typeof telemetry !== "object") return "";
  const current = telemetry.current || {};
  const platforms = Object.entries(telemetry.platforms || {})
    .sort(([leftName, left], [rightName, right]) => {
      if (leftName === current.platform) return -1;
      if (rightName === current.platform) return 1;
      return (right.hash_bytes || 0) - (left.hash_bytes || 0) || leftName.localeCompare(rightName);
    });
  const hashPercent = telemetry.bytes_to_hash
    ? Math.min(100, (telemetry.bytes_read || 0) * 100 / telemetry.bytes_to_hash)
    : 100;
  const currentRate = current.mode === "hashing" && current.elapsed_seconds
    ? (current.bytes_read || 0) / current.elapsed_seconds
    : 0;
  const currentFile = current.relpath
    ? `<div class="scan-current"><span>${current.mode === "hashing" ? "Reading now" : current.mode === "cached" ? "Cache hit" : "Cataloging metadata"}</span><code title="${escapeHtml(current.relpath)}">${escapeHtml(current.relpath)}</code><small>${formatBytes(current.bytes_read || 0)} of ${formatBytes(current.size || 0)}${current.mode === "hashing" ? ` at ${formatBytes(currentRate)}/s` : ""}</small></div>`
    : "";
  const platformRows = platforms.map(([name, item]) => `<tr${name === current.platform ? ' class="selected-row"' : ""}><td><strong>${escapeHtml(name)}</strong></td><td class="meta">${(item.processed_files || 0).toLocaleString()} / ${(item.total_files || 0).toLocaleString()}</td><td class="meta">${(item.processed_hash_files || 0).toLocaleString()} / ${(item.hash_files || 0).toLocaleString()}</td><td class="meta">${(item.processed_cached_files || 0).toLocaleString()} / ${(item.cached_files || 0).toLocaleString()}</td><td class="meta">${(item.processed_metadata_files || 0).toLocaleString()} / ${(item.metadata_files || 0).toLocaleString()}</td><td class="meta">${formatBytes(item.read_bytes || 0)} / ${formatBytes(item.hash_bytes || 0)}</td></tr>`).join("");
  const slowRows = (telemetry.slow_files || []).map((item) => `<tr><td class="name-cell"><code title="${escapeHtml(item.relpath)}">${escapeHtml(item.relpath)}</code></td><td>${escapeHtml(item.platform)}</td><td class="meta">${formatBytes(item.size)}</td><td class="meta">${formatElapsed(item.seconds)}</td><td class="meta">${formatBytes(item.rate)}/s</td></tr>`).join("");
  return `<div class="report-section scan-diagnostics"><div class="report-section-head"><div><h3>Live scan diagnostics</h3><p>Metadata-only files are walked and inspected, but their contents are not read. Physical read shows the actual hashing I/O.</p></div></div><dl class="report-grid scan-facts"><div><dt>Physical read</dt><dd>${formatBytes(telemetry.bytes_read || 0)} / ${formatBytes(telemetry.bytes_to_hash || 0)}</dd></div><div><dt>Current throughput</dt><dd>${currentRate ? `${formatBytes(currentRate)}/s` : "Not reading"}</dd></div><div><dt>Fully hashed</dt><dd>${(telemetry.hashed_files || 0).toLocaleString()} files</dd></div><div><dt>Cache hits</dt><dd>${(telemetry.cached_files || 0).toLocaleString()} files, ${formatBytes(telemetry.cached_bytes || 0)}</dd></div><div><dt>Metadata-only</dt><dd>${(telemetry.metadata_files || 0).toLocaleString()} files, ${formatBytes(telemetry.metadata_bytes || 0)}</dd></div></dl><div class="scan-read-progress" aria-label="${Math.round(hashPercent)} percent of required file contents read"><i style="width:${hashPercent}%"></i></div>${currentFile}${platformRows ? `<div class="table-wrap scan-platforms"><table><thead><tr><th>Platform</th><th>Files processed</th><th>Full hash</th><th>Cache</th><th>Metadata-only</th><th>Physical read</th></tr></thead><tbody>${platformRows}</tbody></table></div>` : ""}${slowRows ? `<details class="scan-slowest"><summary>Slowest completed reads</summary><div class="table-wrap"><table><thead><tr><th>File</th><th>Platform</th><th>Size</th><th>Time</th><th>Rate</th></tr></thead><tbody>${slowRows}</tbody></table></div></details>` : ""}</div>`;
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
    ? `<div class="table-wrap issue-table"><table><thead><tr><th>Unreadable file and reason</th></tr></thead><tbody>${issues.items.map((item) => `<tr><td><code class="issue-path">${escapeHtml(item.detail)}</code></td></tr>`).join("")}</tbody></table></div><div class="issue-actions"><button class="button secondary small" data-copy-issues>Copy loaded issues</button></div>${infiniteFooter(issues, issues.total === 1 ? "captured issue" : "captured issues")}`
    : `<p class="report-empty">No unreadable files were recorded for this job.</p>`;
  const scanIssues = job.kind === "scan"
    ? `<div class="report-section"><div class="report-section-head"><div><h3>Unreadable files</h3><p>${issues.reported_total ? `${issues.reported_total.toLocaleString()} reported by this scan.` : "Paths and reasons captured during scanning."}</p></div></div>${captureWarning}${issueRows}</div>`
    : "";
  const scanTelemetry = job.kind === "scan" ? renderScanTelemetry(job.telemetry) : "";
  return `<section class="job-report" aria-labelledby="job-report-title"><div class="section-heading report-heading"><div><h2 id="job-report-title">Job #${job.id} report</h2><p>${escapeHtml(job.detail)}</p></div><span class="badge ${job.status === "failed" ? "exact" : job.status === "complete" ? "unique" : job.status === "cancelled" ? "cancelled" : "possible"}">${escapeHtml(job.status)}</span></div><dl class="report-grid report-summary"><div><dt>Job type</dt><dd>${escapeHtml(job.kind)}</dd></div><div><dt>Started</dt><dd>${escapeHtml(job.created_at)} UTC</dd></div><div><dt>Finished</dt><dd>${escapeHtml(job.completed_at || "In progress")}${job.completed_at ? " UTC" : ""}</dd></div><div><dt>Duration</dt><dd>${escapeHtml(jobDuration(job))}</dd></div><div><dt>Progress</dt><dd>${job.progress}%</dd></div></dl>${scanTelemetry}<div class="report-section"><h3>Result</h3>${results}</div>${scanIssues}</section>`;
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
  if (state.principal?.must_change_password) {
    renderPasswordChange(true);
    return;
  }
  view.setAttribute("aria-busy", "true");
  const requestedView = state.view;
  let renderVersion = state.renderVersion;
  try {
    const renderers = { overview: renderOverview, library: renderLibrary, artwork: renderArtwork, transfers: renderTransfers, duplicates: renderDuplicates, naming: renderNaming, devices: renderDevices, saves: renderSaves, jobs: renderJobs, notifications: renderNotifications, users: renderUsers, trash: renderTrash };
    const renderPromise = renderers[requestedView]();
    renderVersion = state.renderVersion;
    await renderPromise;
  } catch (error) {
    if (!pageRenderIsCurrent(renderVersion, requestedView)) return;
    if (error.name === "AbortError") return;
    if (error.status === 401) {
      renderAuthentication();
      return;
    }
    setViewHtml(`<div class="empty-state"><div><h2>This view could not load</h2><p>${escapeHtml(error.message)}</p><button class="button secondary" data-retry>Try again</button></div></div>`);
    view.querySelector("[data-retry]")?.addEventListener("click", renderCurrentView);
  } finally {
    if (pageRenderIsCurrent(renderVersion, requestedView)) view.removeAttribute("aria-busy");
  }
}

function renderAuthentication() {
  setHeading("Sign in", "Use your ROMmates account or the bootstrap administrator token.");
  setViewHtml(`<div class="auth-panel"><h2>ROMmates account</h2><p>Your permissions follow your assigned role.</p><form class="auth-form" id="account-login-form"><label class="field" for="login-username"><span>Username</span><input class="input" id="login-username" name="username" autocomplete="username" required maxlength="64"></label><label class="field" for="login-password"><span>Password</span><input class="input" id="login-password" name="password" type="password" autocomplete="current-password" required></label><button class="button">Sign in</button></form><details class="bootstrap-login"><summary>Use bootstrap administrator token</summary><form class="auth-form" id="auth-form"><label class="field" for="access-token"><span>Access token</span><input class="input" id="access-token" name="token" type="password" autocomplete="current-password" required minlength="16"></label><button class="button secondary">Unlock as administrator</button></form></details></div>`);
  document.querySelector("#account-login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      localStorage.removeItem("rommates-token");
      localStorage.removeItem("rom-manager-token");
      const result = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: form.get("username"), password: form.get("password") }) });
      await refreshStatus();
      if (result.user.must_change_password) {
        renderPasswordChange(true);
        return;
      }
      await loadReferenceData();
      navigateTo(isAdmin() ? "overview" : "library", {}, "replace");
    } catch (error) { toast(error.message, "error"); }
  });
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
  document.querySelector("#login-username").focus();
}

function renderPasswordChange(required = false) {
  setHeading(required ? "Choose your password" : "Change password", required
    ? "Replace the temporary password before using ROMmates."
    : "Update your account credentials.");
  setViewHtml(`<div class="auth-panel password-panel"><h2>${required ? "Temporary password" : "Account password"}</h2><p>${required ? "Your administrator can reset access, but only you should know the password you choose here." : "Changing your password signs out your other ROMmates sessions."}</p><form class="auth-form" id="password-change-form"><label class="field"><span>Current password</span><input class="input" name="current_password" type="password" autocomplete="current-password" required></label><label class="field"><span>New password</span><input class="input" name="new_password" type="password" autocomplete="new-password" minlength="12" required aria-describedby="password-requirement"></label><small class="field-help" id="password-requirement">At least 12 characters</small><label class="field"><span>Confirm new password</span><input class="input" name="confirm_password" type="password" autocomplete="new-password" minlength="12" required></label><div class="password-actions">${required ? "" : '<button class="button secondary" type="button" data-cancel-password>Cancel</button>'}<button class="button" type="submit">Change password</button></div></form></div>`);
  view.querySelector("[data-cancel-password]")?.addEventListener("click", renderCurrentView);
  view.querySelector("#password-change-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const newPassword = String(form.get("new_password") || "");
    if (newPassword !== form.get("confirm_password")) {
      toast("New passwords do not match", "error");
      return;
    }
    const submit = event.currentTarget.querySelector("button[type='submit']");
    submit.disabled = true;
    try {
      const result = await api("/api/auth/password", {
        method: "POST",
        body: JSON.stringify({ current_password: form.get("current_password"), new_password: newPassword }),
      });
      state.principal = result.user;
      await refreshStatus();
      await loadReferenceData();
      toast("Password changed");
      navigateTo(isAdmin() ? "overview" : "library", {}, "replace");
    } catch (error) {
      submit.disabled = false;
      toast(error.message, "error");
    }
  });
  view.querySelector("input[name='current_password']")?.focus();
}

function updateActiveNavigation(viewName) {
  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.view === viewName;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
}

function updateBrowserRoute(viewName, historyMode) {
  if (historyMode === "none") return;
  const route = VIEW_ROUTES[viewName] || VIEW_ROUTES.overview;
  if (normalizedRoute() === route) return;
  window.history[historyMode === "replace" ? "replaceState" : "pushState"](
    { view: viewName },
    "",
    route,
  );
}

function navigateTo(viewName, options = {}, historyMode = "push") {
  setMobileNavigation(false);
  if (!VIEW_ROUTES[viewName]) viewName = "overview";
  if (state.principal && !allowedViews().has(viewName)) viewName = "library";
  updateBrowserRoute(viewName, historyMode);
  state.view = viewName;
  state.offset = 0;
  state.search = "";
  state.platform = options.platform || "";
  state.duplicate = options.duplicate || (state.view === "duplicates" ? "exact" : "all");
  if (options.jobId) {
    state.jobReportId = options.jobId;
    state.jobIssueOffset = 0;
  }
  if (options.deviceId) state.deviceId = options.deviceId;
  if (options.saveTab) state.saveTab = options.saveTab;
  state.selectedRows.clear();
  state.editingId = null;
  state.assigningId = null;
  state.artworkId = null;
  state.artworkDetail = null;
  state.assignmentDevices = [];
  if (state.view !== "naming") state.namingSelected.clear();
  if (state.view !== "saves") state.saveSnapshotId = null;
  if (state.view !== "trash") state.trashSelected.clear();
  updateActiveNavigation(viewName);
  renderCurrentView();
  scheduleNavigationLoading(state.renderVersion);
}

document.querySelector("#navigation").addEventListener("click", (event) => {
  const link = event.target.closest("[data-view]");
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigateTo(link.dataset.view);
});

mobileMenuButton.addEventListener("click", () => {
  setMobileNavigation(!document.body.classList.contains("nav-open"));
});
navBackdrop.addEventListener("click", () => setMobileNavigation(false));
sidebarCloseButton.addEventListener("click", () => {
  setMobileNavigation(false);
  mobileMenuButton.focus();
});

window.addEventListener("popstate", () => navigateTo(viewFromLocation(), {}, "none"));
window.addEventListener("pageshow", () => setMobileNavigation(false));
window.matchMedia("(min-width: 721px)").addEventListener("change", (event) => {
  if (event.matches) setMobileNavigation(false);
});

scanButton.addEventListener("click", () => startScan());
stopJobButton.addEventListener("click", () => cancelJob(Number(stopJobButton.dataset.jobId), stopJobButton));
refreshButton.addEventListener("click", async () => {
  try { clearNavigationCache(); await refreshStatus(); await loadReferenceData(); await renderCurrentView(); }
  catch (error) { toast(error.message, "error"); }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
    setMobileNavigation(false);
    mobileMenuButton.focus();
    return;
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    document.querySelector("#search-input")?.focus();
  }
});

async function initialize() {
  updateActiveNavigation(state.view);
  try {
    await refreshStatus();
    if (state.principal?.must_change_password) {
      renderPasswordChange(true);
      return;
    }
    await loadReferenceData();
    if (!allowedViews().has(state.view)) {
      updateBrowserRoute("library", "replace");
      state.view = "library";
      updateActiveNavigation("library");
    }
    await renderCurrentView();
    prefetchNavigationData();
  } catch (error) {
    if (error.status === 401) {
      renderAuthentication();
      return;
    }
    setViewHtml(`<div class="empty-state"><div><h2>ROMmates could not start</h2><p>${escapeHtml(error.message)}</p></div></div>`);
  }
}

logoutButton.addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* clear locally regardless */ }
  localStorage.removeItem("rommates-token");
  localStorage.removeItem("rom-manager-token");
  state.principal = null;
  state.permissions = { admin: false, upload: false, download: false };
  document.querySelector("#account-state")?.classList.add("hidden");
  renderAuthentication();
});

changePasswordButton.addEventListener("click", () => renderPasswordChange(false));

initialize();
