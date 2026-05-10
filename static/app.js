const form = document.getElementById("process-form");
const fileInput = document.getElementById("files");
const selectedFilesEl = document.getElementById("selected-files");
const statusEl = document.getElementById("status");
const submitBtn = document.getElementById("submit-btn");
const resultPanel = document.getElementById("result-panel");
const summaryEl = document.getElementById("summary");

const downloadExcel = document.getElementById("download-excel");
const downloadMarathi = document.getElementById("download-marathi");
const downloadEnglish = document.getElementById("download-english");

const tabs = Array.from(document.querySelectorAll(".tab"));
const tableHead = document.querySelector("#preview-table thead");
const tableBody = document.querySelector("#preview-table tbody");
const prevBtn = document.getElementById("prev-page");
const nextBtn = document.getElementById("next-page");
const pageLabel = document.getElementById("page-label");

const state = {
  jobId: null,
  activeSheet: "marathi",
  page: 1,
  totalPages: 1,
};

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
  setStatus("Processing PDFs. This can take a few minutes for large files.");

  try {
    const response = await fetch("/api/process", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Failed to process PDFs.");
    }

    state.jobId = data.job_id;
    state.activeSheet = "marathi";
    state.page = 1;
    state.totalPages = 1;

    summaryEl.textContent = `Files: ${data.input.total_files} | Parsed records: ${data.output.total_records}`;
    downloadExcel.href = data.output.download_excel_url;
    downloadMarathi.href = data.output.download_marathi_csv_url;
    downloadEnglish.href = data.output.download_english_csv_url;
    resultPanel.classList.remove("hidden");

    activateTab("marathi");
    await loadPreview(1);

    setStatus("Completed.");
  } catch (error) {
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

async function loadPreview(page) {
  if (!state.jobId) return;
  setStatus(`Loading ${state.activeSheet} preview...`);
  try {
    const url = `/api/jobs/${state.jobId}/preview?sheet=${state.activeSheet}&page=${page}&page_size=25`;
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Failed to load preview.");
    }
    state.page = data.page;
    state.totalPages = data.total_pages;
    renderTable(data.columns, data.rows);
    pageLabel.textContent = `Page ${state.page} / ${state.totalPages}`;
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
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? "#b42318" : "";
}
