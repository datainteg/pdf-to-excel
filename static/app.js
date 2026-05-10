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

const tabs = Array.from(document.querySelectorAll(".tab"));
const tableHead = document.querySelector("#preview-table thead");
const tableBody = document.querySelector("#preview-table tbody");
const prevBtn = document.getElementById("prev-page");
const nextBtn = document.getElementById("next-page");
const pageLabel = document.getElementById("page-label");

const formControls = Array.from(form.querySelectorAll("input, select, button"));

const state = {
  jobId: null,
  activeSheet: "marathi",
  page: 1,
  totalPages: 1,
  progressTimer: null,
  progressValue: 0,
  progressStartAt: 0,
};

updatePagerButtons();
loadRecentJobs();

async function parseApiResponse(response) {
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  const isJson = contentType.includes("application/json");

  if (isJson) {
    try {
      return { data: await response.json(), rawText: "" };
    } catch (_) {
      // fall through to text parsing
    }
  }

  let rawText = "";
  try {
    rawText = await response.text();
  } catch (_) {
    rawText = "";
  }

  if (rawText) {
    try {
      return { data: JSON.parse(rawText), rawText };
    } catch (_) {
      // non-JSON text body
    }
  }

  return { data: null, rawText };
}

function getErrorMessage(response, data, rawText, fallbackMessage) {
  if (data && typeof data === "object") {
    if (typeof data.detail === "string" && data.detail.trim()) {
      return data.detail;
    }
    if (typeof data.message === "string" && data.message.trim()) {
      return data.message;
    }
  }

  if (response.status === 401) {
    return "Session expired. Please login again.";
  }

  if (response.status === 500 && rawText.trim() === "Internal Server Error") {
    return "Server processing failed. Please retry with lower DPI or fast/balanced mode, and check server logs.";
  }

  if (rawText && rawText.trim()) {
    return `${fallbackMessage} (${response.status}): ${rawText.trim()}`;
  }

  return `${fallbackMessage} (${response.status})`;
}

async function readApiOrThrow(response, fallbackMessage) {
  const { data, rawText } = await parseApiResponse(response);
  if (!response.ok) {
    throw new Error(getErrorMessage(response, data, rawText, fallbackMessage));
  }

  if (!data || typeof data !== "object") {
    throw new Error(`Unexpected server response (${response.status}).`);
  }

  return data;
}

function clampProgress(value) {
  return Math.max(0, Math.min(100, value));
}

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
  if (state.progressTimer) {
    clearInterval(state.progressTimer);
    state.progressTimer = null;
  }

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
  if (state.progressTimer) {
    clearInterval(state.progressTimer);
    state.progressTimer = null;
  }

  if (success) {
    progressWrapEl.classList.remove("error");
    progressWrapEl.classList.add("complete");
    setProgress(100, message || "Completed.");
    return;
  }

  progressWrapEl.classList.remove("complete");
  progressWrapEl.classList.add("error");
  setProgress(Math.max(state.progressValue, 12), message || "Processing failed.");
}

function formatBytes(sizeBytes) {
  if (typeof sizeBytes !== "number" || Number.isNaN(sizeBytes) || sizeBytes < 0) {
    return "";
  }
  const units = ["B", "KB", "MB", "GB"];
  let val = sizeBytes;
  let unit = units[0];
  for (let i = 0; i < units.length; i += 1) {
    unit = units[i];
    if (val < 1024 || i === units.length - 1) break;
    val /= 1024;
  }
  return `${val >= 100 ? Math.round(val) : val.toFixed(val >= 10 ? 1 : 2)} ${unit}`;
}

function setDownloadLink(anchorEl, artifact, fallbackUrl) {
  if (!anchorEl) return;
  const available = artifact ? Boolean(artifact.available) : Boolean(fallbackUrl);
  const url = (artifact && artifact.download_url) || fallbackUrl || "#";
  if (available) {
    anchorEl.href = url;
    anchorEl.classList.remove("disabled");
    anchorEl.removeAttribute("aria-disabled");
    return;
  }
  anchorEl.href = "#";
  anchorEl.classList.add("disabled");
  anchorEl.setAttribute("aria-disabled", "true");
}

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
    side.style.display = "flex";
    side.style.gap = "8px";
    side.style.alignItems = "center";

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

function formatDateTime(isoValue) {
  if (!isoValue) return "-";
  const date = new Date(isoValue);
  if (Number.isNaN(date.getTime())) return String(isoValue);
  return date.toLocaleString();
}

function findArtifact(artifacts, key) {
  const entries = Array.isArray(artifacts) ? artifacts : [];
  return entries.find((item) => item && item.file_key === key) || null;
}

function statusBadgeClass(status) {
  if (status === "completed" || status === "completed_with_warnings") return "ok";
  return "fail";
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
    openBtn.addEventListener("click", async () => {
      if (!job.job_id) return;
      await openJob(job.job_id);
    });
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
  } catch (_) {
    // ignore on initial load or unauthenticated response
  }
}

async function showJobResult(data) {
  const output = data.output && typeof data.output === "object" ? data.output : {};
  const input = data.input && typeof data.input === "object" ? data.input : {};
  const artifacts = output.artifacts && typeof output.artifacts === "object" ? output.artifacts : {};
  const warnings = Array.isArray(output.warnings) ? output.warnings : [];

  state.jobId = data.job_id;
  state.activeSheet = "marathi";
  state.page = 1;
  state.totalPages = 1;
  updatePagerButtons();

  summaryEl.textContent = `Files: ${input.total_files || 0} | Parsed records: ${output.total_records || 0}`;
  setDownloadLink(downloadExcel, artifacts.excel, output.download_excel_url);
  setDownloadLink(downloadMarathi, artifacts.csv_marathi, output.download_marathi_csv_url);
  setDownloadLink(downloadEnglish, artifacts.csv_english, output.download_english_csv_url);
  renderArtifactSummary(artifacts, warnings);
  resultPanel.classList.remove("hidden");

  const hasArtifactMap = Object.keys(artifacts).length > 0;
  const marathiAvailable = !hasArtifactMap || (artifacts.csv_marathi && artifacts.csv_marathi.available);
  const englishAvailable = !hasArtifactMap || (artifacts.csv_english && artifacts.csv_english.available);

  if (marathiAvailable) {
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
  const anyArtifactReady =
    artifactEntries.length === 0 || artifactEntries.some((item) => item && item.available);
  if (!anyArtifactReady || data.status === "failed_outputs") {
    finishProgress(false, "Output generation failed.");
    const msg = warnings[0] || "Output generation failed. Please check server logs.";
    setStatus(msg, true);
  } else if (warnings.length || data.status === "completed_with_warnings") {
    finishProgress(true, "Completed with warnings.");
    setStatus(`Completed with warnings. ${warnings[0] || ""}`.trim(), false);
  } else {
    finishProgress(true, "Completed.");
    setStatus("Completed.");
  }
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
  if (maxPages) {
    formData.append("max_pages", maxPages);
  }

  setLoading(true);
  startProgress();
  artifactSummaryEl.classList.add("hidden");
  artifactListEl.innerHTML = "";
  setStatus("Processing PDFs. This can take a few minutes for large files.");

  try {
    const response = await fetch("/api/process", {
      method: "POST",
      body: formData,
    });
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

tabs.forEach((tab) => {
  tab.addEventListener("click", async () => {
    if (!state.jobId) return;
    const sheet = tab.dataset.sheet;
    activateTab(sheet);
    state.page = 1;
    await loadPreview(1);
  });
});

if (refreshJobsBtn) {
  refreshJobsBtn.addEventListener("click", async () => {
    refreshJobsBtn.disabled = true;
    try {
      await loadRecentJobs();
    } finally {
      refreshJobsBtn.disabled = false;
    }
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
  tabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.sheet === sheet);
  });
}

function updatePagerButtons() {
  prevBtn.disabled = !state.jobId || state.page <= 1;
  nextBtn.disabled = !state.jobId || state.page >= state.totalPages;
}

async function loadPreview(page) {
  if (!state.jobId) return;
  setStatus(`Loading ${state.activeSheet} preview...`);
  try {
    const url = `/api/jobs/${state.jobId}/preview?sheet=${state.activeSheet}&page=${page}&page_size=25`;
    const response = await fetch(url);
    const data = await readApiOrThrow(response, "Failed to load preview.");
    state.page = data.page;
    state.totalPages = data.total_pages;
    renderTable(data.columns, data.rows);
    pageLabel.textContent = `Page ${state.page} / ${state.totalPages}`;
    updatePagerButtons();
    setStatus("Preview ready.");
  } catch (error) {
    setStatus(error.message || "Could not load preview.", true);
  }
}

function renderTable(columns, rows) {
  tableHead.innerHTML = "";
  tableBody.innerHTML = "";

  const headerRow = document.createElement("tr");
  columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column;
    headerRow.appendChild(th);
  });
  tableHead.appendChild(headerRow);

  if (!rows || rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = Math.max(columns.length, 1);
    td.textContent = "No rows available.";
    tr.appendChild(td);
    tableBody.appendChild(tr);
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((column) => {
      const td = document.createElement("td");
      const value = row[column];
      td.textContent = value === null || value === undefined ? "" : String(value);
      tr.appendChild(td);
    });
    tableBody.appendChild(tr);
  });
}

function setLoading(isLoading) {
  submitBtn.disabled = isLoading;
  submitBtn.textContent = isLoading ? "Converting..." : "Convert PDFs";
  formControls.forEach((el) => {
    if (el.id !== "submit-btn") {
      el.disabled = isLoading;
    }
  });
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("status-error", isError);
}
