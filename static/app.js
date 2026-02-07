const form = document.getElementById("extract-form");
const statusBox = document.getElementById("status");
const resultBox = document.getElementById("result");
const previewTable = document.getElementById("preview-table");
const downloadLink = document.getElementById("download-link");
const liveSummary = document.getElementById("live-summary");
const ocrMode = document.getElementById("ocr_mode");
const geminiFields = document.getElementById("gemini-fields");
const uploadModeInputs = document.querySelectorAll("input[name='upload_mode']");
const folderUploadGroup = document.getElementById("folder-upload-group");
const singleUploadGroup = document.getElementById("single-upload-group");
const folderInput = document.getElementById("folder");
const singleImageInput = document.getElementById("single_image");
const progressFill = document.getElementById("progress-fill");
const progressCount = document.getElementById("progress-count");
const sourcePreview = document.getElementById("source-preview");
const currentFile = document.getElementById("current-file");
const stopBtn = document.getElementById("stop-btn");
const tableSearch = document.getElementById("table-search");
const statusFilter = document.getElementById("status-filter");
const liveCount = document.getElementById("live-count");
const thumbRail = document.getElementById("thumb-rail");
const tabProcessing = document.getElementById("tab-processing");
const tabReview = document.getElementById("tab-review");
const viewProcessing = document.getElementById("view-processing");
const viewReview = document.getElementById("view-review");
const openReviewBtn = document.getElementById("open-review-btn");
const processingOverview = document.getElementById("processing-overview");
const processingCurrentFile = document.getElementById("processing-current-file");
const followLatestInput = document.getElementById("follow-latest");
const reviewLayout = document.getElementById("review-layout");
const toggleImagePaneBtn = document.getElementById("toggle-image-pane");

let tableColumns = [];
let tableBody = null;
let activeRowId = null;
let allRows = [];
let currentController = null;
let isProcessing = false;
let localPreviewUrls = new Map();

function clearLocalPreviewUrls() {
  const urls = new Set(localPreviewUrls.values());
  urls.forEach((url) => URL.revokeObjectURL(url));
  localPreviewUrls = new Map();
}

function getLocalPreviewUrl(fileName) {
  if (!fileName) return "";
  if (localPreviewUrls.has(fileName)) return localPreviewUrls.get(fileName) || "";
  const parts = String(fileName).split(/[\\/]/);
  const baseName = parts[parts.length - 1] || "";
  return baseName ? localPreviewUrls.get(baseName) || "" : "";
}

function resolvePreviewUrl(item) {
  if (!item) return "";
  return item.imageDataUrl || getLocalPreviewUrl(item.row?.file_name || "");
}

function switchWorkspace(view) {
  const showProcessing = view === "processing";
  if (!tabProcessing || !tabReview || !viewProcessing || !viewReview) return;

  tabProcessing.classList.toggle("active", showProcessing);
  tabReview.classList.toggle("active", !showProcessing);
  tabProcessing.setAttribute("aria-selected", String(showProcessing));
  tabReview.setAttribute("aria-selected", String(!showProcessing));
  viewProcessing.classList.toggle("hidden", !showProcessing);
  viewReview.classList.toggle("hidden", showProcessing);
}

function setImagePaneCollapsed(collapsed) {
  if (!reviewLayout || !toggleImagePaneBtn) return;
  reviewLayout.classList.toggle("image-collapsed", collapsed);
  toggleImagePaneBtn.textContent = collapsed ? "Expand Image" : "Collapse Image";
}

function setStatus(message, tone = "") {
  statusBox.className = "status";
  if (tone) statusBox.classList.add(tone);
  statusBox.textContent = message;
}

function setProcessingState(processing) {
  isProcessing = processing;
  stopBtn.classList.toggle("hidden", !processing);
  stopBtn.disabled = !processing;
}

function getUploadMode() {
  const selected = document.querySelector("input[name='upload_mode']:checked");
  return selected ? selected.value : "folder";
}

function syncUploadMode() {
  const mode = getUploadMode();
  const folderSelected = mode === "folder";
  folderUploadGroup.classList.toggle("hidden", !folderSelected);
  singleUploadGroup.classList.toggle("hidden", folderSelected);
  folderInput.required = folderSelected;
  singleImageInput.required = !folderSelected;
}

function setProgress(processed, total) {
  const safeTotal = total > 0 ? total : 0;
  const pct = safeTotal ? Math.min(100, Math.round((processed / safeTotal) * 100)) : 0;
  progressFill.style.width = `${pct}%`;
  progressCount.textContent = `${processed} / ${safeTotal} processed`;
  if (processingOverview) {
    processingOverview.textContent = safeTotal
      ? `${processed} of ${safeTotal} files processed with live indexing.`
      : "Preparing files for extraction.";
  }
}

function updateImagePreview(dataUrl, fileName, fallbackUrl = "") {
  currentFile.textContent = fileName || "No image selected";
  sourcePreview.onerror = null;

  if (!dataUrl) {
    if (!fallbackUrl) {
      sourcePreview.removeAttribute("src");
      sourcePreview.classList.remove("visible");
      return;
    }
    dataUrl = fallbackUrl;
  }

  if (fallbackUrl && fallbackUrl !== dataUrl) {
    sourcePreview.onerror = () => {
      sourcePreview.onerror = null;
      sourcePreview.src = fallbackUrl;
      sourcePreview.classList.add("visible");
    };
  }

  sourcePreview.src = dataUrl;
  sourcePreview.classList.add("visible");
}

function resetLiveView() {
  clearLocalPreviewUrls();
  previewTable.innerHTML = "";
  thumbRail.innerHTML = "";
  tableColumns = [];
  tableBody = null;
  activeRowId = null;
  allRows = [];
  resultBox.classList.add("hidden");
  liveSummary.textContent = "Live session";
  liveCount.textContent = "0 records visible";
  updateImagePreview("", "No image selected");
  setProgress(0, 0);
  if (processingCurrentFile) processingCurrentFile.textContent = "Waiting for first processed image.";
  if (followLatestInput) followLatestInput.checked = true;
  setImagePaneCollapsed(false);
  switchWorkspace("processing");
}

function initializeTable(columns) {
  tableColumns = columns;
  previewTable.innerHTML = "";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach((col) => {
    const th = document.createElement("th");
    th.textContent = col;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  previewTable.appendChild(thead);

  tableBody = document.createElement("tbody");
  previewTable.appendChild(tableBody);
}

function matchesFilters(item) {
  const query = tableSearch.value.trim().toLowerCase();
  const filter = statusFilter.value;

  if (filter !== "all" && item.status !== filter) return false;

  if (!query) return true;
  return Object.values(item.row).some((value) => String(value || "").toLowerCase().includes(query));
}

function setActiveSelection(id) {
  activeRowId = id;

  const rows = tableBody ? Array.from(tableBody.querySelectorAll("tr")) : [];
  rows.forEach((tr) => tr.classList.toggle("active", tr.dataset.rowId === id));

  const thumbs = Array.from(thumbRail.querySelectorAll(".thumb-item"));
  thumbs.forEach((btn) => btn.classList.toggle("active", btn.dataset.rowId === id));

  const selected = allRows.find((item) => item.id === id);
  if (selected) {
    const localFallback = getLocalPreviewUrl(selected.row.file_name || "");
    const primary = selected.imageDataUrl || localFallback;
    const secondary = selected.imageDataUrl && localFallback ? localFallback : "";
    updateImagePreview(primary, selected.row.file_name || "Selected image", secondary);
  }
}

function renderTable() {
  if (!tableBody) return;
  tableBody.innerHTML = "";

  const visibleRows = allRows.filter(matchesFilters);
  liveCount.textContent = `${visibleRows.length} records visible`;

  visibleRows.forEach((item) => {
    const tr = document.createElement("tr");
    tr.dataset.rowId = item.id;
    if (item.status === "error") tr.classList.add("row-error");

    tableColumns.forEach((col) => {
      const td = document.createElement("td");
      td.textContent = item.row[col] ?? "";
      tr.appendChild(td);
    });

    tr.addEventListener("click", () => setActiveSelection(item.id));
    tableBody.appendChild(tr);
  });

  if (activeRowId && visibleRows.some((item) => item.id === activeRowId)) {
    setActiveSelection(activeRowId);
  } else if (visibleRows.length) {
    const shouldFollowLatest = !followLatestInput || followLatestInput.checked;
    const fallback = shouldFollowLatest ? visibleRows[visibleRows.length - 1] : visibleRows[0];
    setActiveSelection(fallback.id);
  }
}

function addThumbnail(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "thumb-item";
  button.dataset.rowId = item.id;
  button.title = item.row.file_name || "Processed image";

  const previewUrl = resolvePreviewUrl(item);
  if (previewUrl) {
    const img = document.createElement("img");
    img.src = previewUrl;
    img.alt = item.row.file_name || "Processed image";
    button.appendChild(img);
  }

  button.addEventListener("click", () => setActiveSelection(item.id));
  thumbRail.appendChild(button);
}

function appendLiveRow(row, imageDataUrl, status = "ok") {
  const rowId = `row_${Date.now()}_${allRows.length + 1}`;
  const item = {
    id: rowId,
    row,
    imageDataUrl,
    status,
  };
  allRows.push(item);

  addThumbnail(item);
  renderTable();
  if (!followLatestInput || followLatestInput.checked) {
    setActiveSelection(rowId);
  }
}

function handleStreamEvent(event) {
  if (event.type === "start") {
    initializeTable(event.columns || []);
    resultBox.classList.remove("hidden");
    switchWorkspace("processing");
    setProgress(0, event.total_files || 0);
    setStatus("Starting extraction...", "info");
    liveSummary.textContent = "Live session in progress";
    if (processingCurrentFile) processingCurrentFile.textContent = "Waiting for first processed image.";
    return;
  }

  if (event.type === "progress") {
    setProgress(event.processed || 0, event.total_files || 0);
    setStatus(`Processing ${event.processed}/${event.total_files}: ${event.file_name}`, "info");
    if (processingCurrentFile) processingCurrentFile.textContent = event.file_name || "Processing image...";
    if (event.row) appendLiveRow(event.row, event.image_data_url || "", event.status || "ok");
    return;
  }

  if (event.type === "complete") {
    setStatus(`${event.rows} images processed successfully.`, "success");
    downloadLink.href = event.download_url;
    downloadLink.textContent = `Download ${event.output_file}`;
    liveSummary.textContent = "Output ready";
    switchWorkspace("review");
    setProcessingState(false);
    return;
  }

  if (event.type === "fatal") {
    throw new Error(event.detail || "Processing failed.");
  }
}

async function processStream(formData, signal) {
  const res = await fetch("/process-stream", { method: "POST", body: formData, signal });
  if (!res.ok) {
    let message = "Failed to process files.";
    try {
      const body = await res.json();
      message = body.detail || message;
    } catch (_) {
      // Keep fallback message.
    }
    throw new Error(message);
  }

  if (!res.body) throw new Error("Streaming is not supported in this browser.");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      handleStreamEvent(JSON.parse(trimmed));
    }
  }

  if (buffer.trim()) handleStreamEvent(JSON.parse(buffer.trim()));
}

ocrMode.addEventListener("change", () => {
  geminiFields.classList.toggle("hidden", ocrMode.value !== "gemini");
});

uploadModeInputs.forEach((input) => input.addEventListener("change", syncUploadMode));
tableSearch.addEventListener("input", renderTable);
statusFilter.addEventListener("change", renderTable);
if (tabProcessing) tabProcessing.addEventListener("click", () => switchWorkspace("processing"));
if (tabReview) tabReview.addEventListener("click", () => switchWorkspace("review"));
if (openReviewBtn) openReviewBtn.addEventListener("click", () => switchWorkspace("review"));
if (followLatestInput) {
  followLatestInput.addEventListener("change", () => {
    if (followLatestInput.checked) {
      renderTable();
    }
  });
}
if (toggleImagePaneBtn) {
  toggleImagePaneBtn.addEventListener("click", () => {
    const willCollapse = !reviewLayout || !reviewLayout.classList.contains("image-collapsed");
    setImagePaneCollapsed(willCollapse);
  });
}

stopBtn.addEventListener("click", () => {
  if (!currentController) return;
  currentController.abort();
  setStatus("Processing stopped by user.", "error");
  liveSummary.textContent = "Session stopped";
  setProcessingState(false);
});

syncUploadMode();
setProcessingState(false);
switchWorkspace("processing");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isProcessing) return;

  resetLiveView();
  setStatus("Preparing files...", "info");

  const data = new FormData(form);
  const mode = getUploadMode();
  const selectedFiles = mode === "folder" ? Array.from(folderInput.files || []) : Array.from(singleImageInput.files || []);

  if (!selectedFiles.length) {
    setStatus(mode === "folder" ? "Please select a folder with images." : "Please select one image file.", "error");
    return;
  }

  data.delete("files");
  for (const file of selectedFiles) {
    const ext = file.name.toLowerCase();
    if (ext.endsWith(".jpg") || ext.endsWith(".jpeg") || ext.endsWith(".png")) {
      const relative = file.webkitRelativePath || file.name;
      const objectUrl = URL.createObjectURL(file);
      data.append("files", file, relative);
      localPreviewUrls.set(relative, objectUrl);
      localPreviewUrls.set(file.name, objectUrl);
    }
  }

  if (!data.getAll("files").length) {
    setStatus("No supported image files found. Use .jpg, .jpeg, or .png.", "error");
    return;
  }

  if (ocrMode.value === "gemini") {
    const key = document.getElementById("gemini_api_key").value.trim();
    if (!key) {
      setStatus("Please provide Gemini API key.", "error");
      return;
    }
  }

  currentController = new AbortController();
  setProcessingState(true);

  try {
    await processStream(data, currentController.signal);
  } catch (error) {
    if (error.name === "AbortError") {
      setStatus("Processing stopped by user.", "error");
    } else {
      setStatus(`Request failed: ${error.message}`, "error");
    }
    liveSummary.textContent = "Session ended with interruption";
  } finally {
    currentController = null;
    setProcessingState(false);
  }
});
