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
  setStatus("Processing PDFs. This can take a few minutes for large files.");

  try {
    const response = await fetch("/api/process", {
      method: "POST",
      body: formData,
    });
    const data = await readApiOrThrow(response, "Failed to process PDFs.");

    state.jobId = data.job_id;
    state.activeSheet = "marathi";
    state.page = 1;
    state.totalPages = 1;
    updatePagerButtons();

    summaryEl.textContent = `Files: ${data.input.total_files} | Parsed records: ${data.output.total_records}`;
    downloadExcel.href = data.output.download_excel_url;
    downloadMarathi.href = data.output.download_marathi_csv_url;
    downloadEnglish.href = data.output.download_english_csv_url;
    resultPanel.classList.remove("hidden");

    activateTab("marathi");
    await loadPreview(1);

    finishProgress(true, "Completed.");
    setStatus("Completed.");
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
