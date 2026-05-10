const form = document.getElementById("process-form");
const fileInput = document.getElementById("files");
const selectedFilesEl = document.getElementById("selected-files");
const statusEl = document.getElementById("status");
const submitBtn = document.getElementById("submit-btn");
const resultPanel = document.getElementById("result-panel");
const summaryEl = document.getElementById("summary");

const progressWrapEl = document.getElementById("progress-wrap");
const progressStageEl = document.getElementById("progress-stage");
const progressPercentEl = document.getElementById("progress-percent");
const progressTrackEl = document.querySelector(".progress-track");
const progressFillEl = document.getElementById("progress-fill");
const artifactSummaryEl = document.getElementById("artifact-summary");
const artifactListEl = document.getElementById("artifact-list");
const recentJobsListEl = document.getElementById("recent-jobs-list");
const refreshJobsBtn = document.getElementById("refresh-jobs");

const downloadExcel = document.getElementById("download-excel");
const downloadMarathi = document.getElementById("download-marathi");
const downloadEnglish = document.getElementById("download-english");
const liveExportGroup = document.getElementById("live-export-group");

const tabs = Array.from(document.querySelectorAll(".tab"));
const tableHead = document.querySelector("#preview-table thead");
const tableBody = document.querySelector("#preview-table tbody");
const prevBtn = document.getElementById("prev-page");
const nextBtn = document.getElementById("next-page");
const pageLabel = document.getElementById("page-label");

const correctionModal = document.getElementById("correction-modal");
const correctionForm = document.getElementById("correction-form");
const correctionError = document.getElementById("correction-error");
const modalClose = document.getElementById("modal-close");
const modalCancel = document.getElementById("modal-cancel");

const formControls = Array.from(form.querySelectorAll("input, select, button"));

const LIVE_SHEETS = new Set(["marathi-live", "english-live"]);

const state = {
  jobId: null,
  activeSheet: "marathi",
  page: 1,
  totalPages: 1,
  progressTimer: null,
  progressValue: 0,
  progressStartAt: 0,
  hasLiveData: false,
  liveColumns: [],
};

updatePagerButtons();
loadRecentJobs();

// ── API helpers ────────────────────────────────────────────────────────────

async function parseApiResponse(response) {
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  if (contentType.includes("application/json")) {
    try { return { data: await response.json(), rawText: "" }; } catch (_) {}
  }
  let rawText = "";
  try { rawText = await response.text(); } catch (_) {}
  if (rawText) {
    try { return { data: JSON.parse(rawText), rawText }; } catch (_) {}
  }
  return { data: null, rawText };
}

function getErrorMessage(response, data, rawText, fallbackMessage) {
  if (data && typeof data === "object") {
    if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
    if (typeof data.message === "string" && data.message.trim()) return data.message;
  }
  if (response.status === 401) return "Session expired. Please login again.";
  if (rawText && rawText.trim()) return `${fallbackMessage} (${response.status}): ${rawText.trim()}`;
  return `${fallbackMessage} (${response.status})`;
}

async function readApiOrThrow(response, fallbackMessage) {
  const { data, rawText } = await parseApiResponse(response);
  if (!response.ok) throw new Error(getErrorMessage(response, data, rawText, fallbackMessage));
  if (!data || typeof data !== "object") throw new Error(`Unexpected server response (${response.status}).`);
  return data;
}

// ── Progress ───────────────────────────────────────────────────────────────

function clampProgress(value) { return Math.max(0, Math.min(100, value)); }

function progressStageByValue(progress) {
  if (progress < 12) return "Uploading PDF files...";
  if (progress < 32) return "Rendering PDF pages...";
  if (progress < 65) return "Running OCR extraction...";
  if (progress < 85) return "Parsing voter rows...";
  return "Preparing Excel and CSV files...";
}

function setProgress(progress, stage) {
  const value = Math.round(clampProgress(progress));
  state.progressValue = value;
  progressWrapEl.classList.remove("hidden");
  progressFillEl.style.width = `${value}%`;
  progressTrackEl.setAttribute("aria-valuenow", String(value));
  progressPercentEl.textContent = `${value}%`;
  progressStageEl.textContent = stage || progressStageByValue(value);
}

function startProgress() {
  if (state.progressTimer) { clearInterval(state.progressTimer); state.progressTimer = null; }
  state.progressStartAt = Date.now();
  progressWrapEl.classList.remove("error", "complete");
  setProgress(3, "Uploading PDF files...");
  state.progressTimer = setInterval(() => {
    const elapsed = (Date.now() - state.progressStartAt) / 1000;
    const target = Math.min(92, 8 + elapsed * 1.1 + Math.log1p(elapsed) * 11);
    if (state.progressValue >= target) return;
    const step = state.progressValue < 50 ? 2 : state.progressValue < 75 ? 1 : 0.6;
    setProgress(Math.min(target, state.progressValue + step));
  }, 900);
}

function finishProgress(success, message) {
  if (state.progressTimer) { clearInterval(state.progressTimer); state.progressTimer = null; }
  if (success) {
    progressWrapEl.classList.remove("error");
    progressWrapEl.classList.add("complete");
    setProgress(100, message || "Completed.");
  } else {
    progressWrapEl.classList.remove("complete");
    progressWrapEl.classList.add("error");
    setProgress(Math.max(state.progressValue, 12), message || "Processing failed.");
  }
}

// ── Formatting ─────────────────────────────────────────────────────────────

function formatBytes(sizeBytes) {
  if (typeof sizeBytes !== "number" || Number.isNaN(sizeBytes) || sizeBytes < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let val = sizeBytes, unit = units[0];
  for (let i = 0; i < units.length; i++) {
    unit = units[i];
    if (val < 1024 || i === units.length - 1) break;
    val /= 1024;
  }
  return `${val >= 100 ? Math.round(val) : val.toFixed(val >= 10 ? 1 : 2)} ${unit}`;
}

function formatDateTime(isoValue) {
  if (!isoValue) return "-";
  const date = new Date(isoValue);
  if (Number.isNaN(date.getTime())) return String(isoValue);
  return date.toLocaleString();
}

// ── Download helpers ───────────────────────────────────────────────────────

function setDownloadLink(anchorEl, artifact, fallbackUrl) {
  if (!anchorEl) return;
  const available = artifact ? Boolean(artifact.available) : Boolean(fallbackUrl);
  const url = (artifact && artifact.download_url) || fallbackUrl || "#";
  if (available) {
    anchorEl.href = url;
    anchorEl.classList.remove("disabled");
    anchorEl.removeAttribute("aria-disabled");
  } else {
    anchorEl.href = "#";
    anchorEl.classList.add("disabled");
    anchorEl.setAttribute("aria-disabled", "true");
  }
}

function triggerExport(jobId, sheet, fmt) {
  window.open(`/api/jobs/${jobId}/export/${sheet}/${fmt}`, "_blank");
}

function setupLiveExports(jobId) {
  document.getElementById("export-marathi-csv").onclick = () => triggerExport(jobId, "marathi", "csv");
  document.getElementById("export-english-csv").onclick = () => triggerExport(jobId, "english", "csv");
  document.getElementById("export-marathi-excel").onclick = () => triggerExport(jobId, "marathi", "excel");
  document.getElementById("export-english-excel").onclick = () => triggerExport(jobId, "english", "excel");
  liveExportGroup.style.display = "";
}

// ── Artifact summary ───────────────────────────────────────────────────────

function renderArtifactSummary(artifacts, warnings) {
  artifactListEl.innerHTML = "";
  const entries = artifacts && typeof artifacts === "object" ? Object.values(artifacts) : [];
  if (entries.length === 0 && (!warnings || warnings.length === 0)) {
    artifactSummaryEl.classList.add("hidden");
    return;
  }

  entries.forEach((item) => {
    const row = document.createElement("div");
    row.className = "artifact-item";

    const main = document.createElement("div");
    main.className = "artifact-main";

    const label = document.createElement("div");
    label.className = "artifact-label";
    label.textContent = item.label || item.file_key || "Output";
    main.appendChild(label);

    const details = [];
    if (item.storage) details.push(`Storage: ${item.storage}`);
    if (item.size_bytes !== null && item.size_bytes !== undefined) {
      const sizeText = formatBytes(item.size_bytes);
      if (sizeText) details.push(`Size: ${sizeText}`);
    }
    if (!item.available && item.error) details.push(item.error);

    const meta = document.createElement("div");
    meta.className = `artifact-meta${!item.available && item.error ? " error" : ""}`;
    meta.textContent = details.join(" | ") || (item.available ? "Ready to download." : "Not available.");
    main.appendChild(meta);

    const side = document.createElement("div");
    side.style.cssText = "display:flex;gap:8px;align-items:center";

    const pill = document.createElement("span");
    pill.className = `artifact-pill ${item.available ? "ok" : "fail"}`;
    pill.textContent = item.available ? "Ready" : "Failed";
    side.appendChild(pill);

    const action = document.createElement(item.available ? "a" : "span");
    action.className = `artifact-link${item.available ? "" : " disabled"}`;
    action.textContent = item.available ? "Download" : "Unavailable";
    if (item.available && item.download_url) {
      action.href = item.download_url;
      action.target = "_blank";
      action.rel = "noopener";
    }
    side.appendChild(action);

    row.appendChild(main);
    row.appendChild(side);
    artifactListEl.appendChild(row);
  });

  if (warnings && warnings.length) {
    const warnRow = document.createElement("div");
    warnRow.className = "artifact-item";
    const warnMain = document.createElement("div");
    warnMain.className = "artifact-main";
    const warnLabel = document.createElement("div");
    warnLabel.className = "artifact-label";
    warnLabel.textContent = "Warnings";
    const warnMeta = document.createElement("div");
    warnMeta.className = "artifact-meta error";
    warnMeta.textContent = warnings.join(" | ");
    warnMain.appendChild(warnLabel);
    warnMain.appendChild(warnMeta);
    warnRow.appendChild(warnMain);
    artifactListEl.appendChild(warnRow);
  }

  artifactSummaryEl.classList.remove("hidden");
}

// ── Recent jobs ────────────────────────────────────────────────────────────

function findArtifact(artifacts, key) {
  const entries = Array.isArray(artifacts) ? artifacts : [];
  return entries.find((item) => item && item.file_key === key) || null;
}

function statusBadgeClass(status) {
  return (status === "completed" || status === "completed_with_warnings") ? "ok" : "fail";
}

function renderRecentJobs(jobs) {
  if (!recentJobsListEl) return;
  recentJobsListEl.innerHTML = "";
  if (!Array.isArray(jobs) || jobs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "recent-job-meta";
    empty.textContent = "No jobs yet.";
    recentJobsListEl.appendChild(empty);
    return;
  }

  jobs.forEach((job) => {
    const row = document.createElement("div");
    row.className = "recent-job-item";

    const main = document.createElement("div");
    main.className = "recent-job-main";

    const title = document.createElement("div");
    title.className = "recent-job-title";
    title.textContent = `Job ${job.job_id || "-"}`;
    main.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "recent-job-meta";
    meta.textContent = `Created: ${formatDateTime(job.created_at)} | Files: ${job.total_files || 0} | Records: ${job.total_records || 0}`;
    main.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "recent-job-actions";

    const status = document.createElement("span");
    status.className = `artifact-pill ${statusBadgeClass(job.status)}`;
    status.textContent = job.status || "unknown";
    actions.appendChild(status);

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "refresh-btn";
    openBtn.textContent = "Open";
    openBtn.disabled = !job.job_id;
    openBtn.addEventListener("click", async () => { if (job.job_id) await openJob(job.job_id); });
    actions.appendChild(openBtn);

    const excelArtifact = findArtifact(job.artifacts, "excel");
    const excelLink = document.createElement("a");
    excelLink.className = `recent-job-link${excelArtifact && excelArtifact.available ? "" : " disabled"}`;
    excelLink.textContent = "Excel";
    excelLink.href = excelArtifact && excelArtifact.available ? excelArtifact.download_url : "#";
    excelLink.target = "_blank";
    excelLink.rel = "noopener";
    actions.appendChild(excelLink);

    row.appendChild(main);
    row.appendChild(actions);
    recentJobsListEl.appendChild(row);
  });
}

async function loadRecentJobs() {
  if (!recentJobsListEl) return;
  try {
    const response = await fetch("/api/jobs/recent?limit=8");
    const data = await readApiOrThrow(response, "Failed to load recent jobs.");
    renderRecentJobs(data.jobs || []);
  } catch (_) {}
}

// ── Show job result ────────────────────────────────────────────────────────

async function showJobResult(data) {
  const output = (data.output && typeof data.output === "object") ? data.output : {};
  const input = (data.input && typeof data.input === "object") ? data.input : {};
  const artifacts = (output.artifacts && typeof output.artifacts === "object") ? output.artifacts : {};
  const warnings = Array.isArray(output.warnings) ? output.warnings : [];

  state.jobId = data.job_id;
  state.activeSheet = "marathi";
  state.page = 1;
  state.totalPages = 1;
  state.hasLiveData = false;
  updatePagerButtons();

  summaryEl.textContent = `Files: ${input.total_files || 0} | Parsed records: ${output.total_records || 0}`;
  setDownloadLink(downloadExcel, artifacts.excel, output.download_excel_url);
  setDownloadLink(downloadMarathi, artifacts.csv_marathi, output.download_marathi_csv_url);
  setDownloadLink(downloadEnglish, artifacts.csv_english, output.download_english_csv_url);
  renderArtifactSummary(artifacts, warnings);
  resultPanel.classList.remove("hidden");

  // Check if live data (MongoDB records) available
  await checkLiveData(data.job_id);

  const hasArtifactMap = Object.keys(artifacts).length > 0;
  const marathiAvailable = !hasArtifactMap || (artifacts.csv_marathi && artifacts.csv_marathi.available);
  const englishAvailable = !hasArtifactMap || (artifacts.csv_english && artifacts.csv_english.available);

  if (state.hasLiveData) {
    activateTab("marathi-live");
    await loadPreview(1);
  } else if (marathiAvailable) {
    activateTab("marathi");
    await loadPreview(1);
  } else if (englishAvailable) {
    activateTab("english");
    await loadPreview(1);
  } else {
    tableHead.innerHTML = "";
    tableBody.innerHTML = "";
    pageLabel.textContent = "Page 1 / 1";
    updatePagerButtons();
    setStatus("No preview available because CSV generation failed.", true);
  }

  const artifactEntries = Object.values(artifacts);
  const anyArtifactReady = artifactEntries.length === 0 || artifactEntries.some((item) => item && item.available);
  if (!anyArtifactReady || data.status === "failed_outputs") {
    finishProgress(false, "Output generation failed.");
    setStatus(warnings[0] || "Output generation failed. Please check server logs.", true);
  } else if (warnings.length || data.status === "completed_with_warnings") {
    finishProgress(true, "Completed with warnings.");
    setStatus(`Completed with warnings. ${warnings[0] || ""}`.trim(), false);
  } else {
    finishProgress(true, "Completed.");
    setStatus("Completed.");
  }
}

async function checkLiveData(jobId) {
  try {
    const r = await fetch(`/api/jobs/${jobId}/records?sheet=marathi&page=1&page_size=1`);
    if (r.ok) {
      state.hasLiveData = true;
      const tabLiveM = document.getElementById("tab-live-marathi");
      const tabLiveE = document.getElementById("tab-live-english");
      if (tabLiveM) tabLiveM.style.display = "";
      if (tabLiveE) tabLiveE.style.display = "";
      setupLiveExports(jobId);
    }
  } catch (_) {}
}

async function openJob(jobId) {
  setStatus(`Loading job ${jobId} ...`);
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    const data = await readApiOrThrow(response, "Failed to load job.");
    startProgress();
    await showJobResult(data);
  } catch (error) {
    setStatus(error.message || "Could not load job.", true);
  } finally {
    setLoading(false);
  }
}

// ── Form submit ────────────────────────────────────────────────────────────

fileInput.addEventListener("change", () => {
  if (!fileInput.files || fileInput.files.length === 0) {
    selectedFilesEl.textContent = "No files selected";
    return;
  }
  const names = Array.from(fileInput.files).map((f) => f.name);
  if (names.length <= 3) {
    selectedFilesEl.textContent = names.join(", ");
  } else {
    selectedFilesEl.textContent = `${names.slice(0, 3).join(", ")} +${names.length - 3} more`;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!fileInput.files || fileInput.files.length === 0) {
    setStatus("Please select at least one PDF file.", true);
    return;
  }

  const formData = new FormData();
  Array.from(fileInput.files).forEach((file) => formData.append("files", file));
  formData.append("lang", document.getElementById("lang").value);
  formData.append("accuracy_mode", document.getElementById("accuracy_mode").value);
  formData.append("dpi", document.getElementById("dpi").value || "300");
  const maxPages = document.getElementById("max_pages").value;
  if (maxPages) formData.append("max_pages", maxPages);

  setLoading(true);
  startProgress();
  artifactSummaryEl.classList.add("hidden");
  artifactListEl.innerHTML = "";
  liveExportGroup.style.display = "none";
  const tabLiveM = document.getElementById("tab-live-marathi");
  const tabLiveE = document.getElementById("tab-live-english");
  if (tabLiveM) tabLiveM.style.display = "none";
  if (tabLiveE) tabLiveE.style.display = "none";
  setStatus("Processing PDFs. This can take a few minutes for large files.");

  try {
    const response = await fetch("/api/process", { method: "POST", body: formData });
    const data = await readApiOrThrow(response, "Failed to process PDFs.");
    await showJobResult(data);
    await loadRecentJobs();
  } catch (error) {
    finishProgress(false, "Processing failed.");
    setStatus(error.message || "Request failed.", true);
  } finally {
    setLoading(false);
  }
});

// ── Tabs & pager ───────────────────────────────────────────────────────────

tabs.forEach((tab) => {
  tab.addEventListener("click", async () => {
    if (!state.jobId) return;
    activateTab(tab.dataset.sheet);
    state.page = 1;
    await loadPreview(1);
  });
});

if (refreshJobsBtn) {
  refreshJobsBtn.addEventListener("click", async () => {
    refreshJobsBtn.disabled = true;
    try { await loadRecentJobs(); } finally { refreshJobsBtn.disabled = false; }
  });
}

prevBtn.addEventListener("click", async () => {
  if (!state.jobId || state.page <= 1) return;
  await loadPreview(state.page - 1);
});

nextBtn.addEventListener("click", async () => {
  if (!state.jobId || state.page >= state.totalPages) return;
  await loadPreview(state.page + 1);
});

function activateTab(sheet) {
  state.activeSheet = sheet;
  tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.sheet === sheet));
}

function updatePagerButtons() {
  prevBtn.disabled = !state.jobId || state.page <= 1;
  nextBtn.disabled = !state.jobId || state.page >= state.totalPages;
}

// ── Preview loading ────────────────────────────────────────────────────────

async function loadPreview(page) {
  if (!state.jobId) return;

  const isLive = LIVE_SHEETS.has(state.activeSheet);
  const baseSheet = state.activeSheet.replace("-live", "");

  const url = isLive
    ? `/api/jobs/${state.jobId}/records?sheet=${baseSheet}&page=${page}&page_size=25`
    : `/api/jobs/${state.jobId}/preview?sheet=${baseSheet}&page=${page}&page_size=25`;

  setStatus(`Loading ${state.activeSheet} preview...`);
  try {
    const response = await fetch(url);
    const data = await readApiOrThrow(response, "Failed to load preview.");
    state.page = data.page;
    state.totalPages = data.total_pages;
    state.liveColumns = data.columns || [];
    renderTable(data.columns, data.rows, isLive);
    pageLabel.textContent = `Page ${state.page} / ${state.totalPages}`;
    updatePagerButtons();
    setStatus(isLive ? "Live data preview (editable). Click a row to correct." : "Preview ready.");
  } catch (error) {
    setStatus(error.message || "Could not load preview.", true);
  }
}

// ── Table rendering ────────────────────────────────────────────────────────

function renderTable(columns, rows, editable = false) {
  tableHead.innerHTML = "";
  tableBody.innerHTML = "";

  const headerRow = document.createElement("tr");
  columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column;
    headerRow.appendChild(th);
  });
  if (editable) {
    const th = document.createElement("th");
    th.textContent = "Edit";
    th.style.width = "60px";
    headerRow.appendChild(th);
  }
  tableHead.appendChild(headerRow);

  if (!rows || rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = columns.length + (editable ? 1 : 0);
    td.textContent = "No rows available.";
    tr.appendChild(td);
    tableBody.appendChild(tr);
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    if (editable) tr.style.cursor = "default";

    columns.forEach((column) => {
      const td = document.createElement("td");
      const value = row[column];
      td.textContent = value === null || value === undefined ? "" : String(value);
      tr.appendChild(td);
    });

    if (editable) {
      const td = document.createElement("td");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "edit-row-btn";
      btn.textContent = "Edit";
      btn.addEventListener("click", () => openCorrectionModal(row));
      td.appendChild(btn);
      tr.appendChild(td);
    }

    tableBody.appendChild(tr);
  });
}

// ── Correction modal ───────────────────────────────────────────────────────

function openCorrectionModal(row) {
  const serialNo = row["serial_no"];
  document.getElementById("edit-serial-no").value = serialNo ?? "";
  document.getElementById("edit-serial-display").value = serialNo ?? "";
  document.getElementById("edit-house-no").value = row["house_no"] ?? "";
  document.getElementById("edit-name").value = row["name"] ?? "";
  document.getElementById("edit-relative-name").value = row["relative_name"] ?? "";
  document.getElementById("edit-relation-code").value = row["relation_code"] ?? "";
  document.getElementById("edit-gender").value = row["gender"] ?? "";
  document.getElementById("edit-age").value = row["age"] ?? "";
  document.getElementById("edit-voter-id").value = row["voter_id"] ?? "";
  correctionError.classList.add("hidden");
  correctionError.textContent = "";
  correctionModal.classList.remove("hidden");
  document.getElementById("edit-house-no").focus();
}

function closeCorrectionModal() {
  correctionModal.classList.add("hidden");
}

modalClose.addEventListener("click", closeCorrectionModal);
modalCancel.addEventListener("click", closeCorrectionModal);
correctionModal.addEventListener("click", (e) => {
  if (e.target === correctionModal) closeCorrectionModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCorrectionModal();
});

correctionForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.jobId) return;

  const serialNo = parseInt(document.getElementById("edit-serial-no").value, 10);
  if (!serialNo) return;

  const updates = {};
  const fields = ["house_no", "name", "relative_name", "relation_code", "gender", "voter_id"];
  fields.forEach((f) => {
    const el = document.getElementById(`edit-${f.replace("_", "-")}`);
    if (el) updates[f] = el.value.trim() || null;
  });
  const age = document.getElementById("edit-age").value;
  updates.age = age ? parseInt(age, 10) : null;

  const saveBtn = document.getElementById("modal-save");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving...";
  correctionError.classList.add("hidden");

  try {
    const response = await fetch(`/api/jobs/${state.jobId}/records/${serialNo}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    await readApiOrThrow(response, "Failed to save correction.");
    closeCorrectionModal();
    await loadPreview(state.page);
    setStatus(`Record #${serialNo} corrected.`);
  } catch (err) {
    correctionError.textContent = err.message || "Save failed.";
    correctionError.classList.remove("hidden");
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "Save Correction";
  }
});

// ── Misc ───────────────────────────────────────────────────────────────────

function setLoading(isLoading) {
  submitBtn.disabled = isLoading;
  submitBtn.textContent = isLoading ? "Converting..." : "Convert PDFs";
  formControls.forEach((el) => {
    if (el.id !== "submit-btn") el.disabled = isLoading;
  });
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("status-error", isError);
}
