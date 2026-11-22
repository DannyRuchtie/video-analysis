import { byId, json, renderAnalysis } from "./gallery.js";

const form = byId("import-form");
const fileInput = byId("video-file");
const urlInput = byId("youtube-url");
const thresholdInput = byId("sensitivity");
const dropZone = byId("drop-zone");
const progress = byId("progress");
const error = byId("error");
const accessCode = byId("access-code");
const tabs = Array.from(document.querySelectorAll('[role="tablist"] [role="tab"]'));
const defaultButton = byId("show-default");
const lastAnalysis = "shotline-last-analysis";
const activeJob = "shotline-active-job";

let source = tabs[0].id.slice(4);
let selectedFile = null;
let busy = false;
let maxUpload = 16 * 1024 ** 3;
let protectedImports = false;
let hasDefault = false;

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // The site works without persistent browser storage.
  }
}

function changeSource(next) {
  source = next;
  tabs.forEach((tab) => {
    const selected = tab.id === "tab-" + source;
    tab.setAttribute("aria-selected", selected);
    tab.tabIndex = selected ? 0 : -1;
    byId(tab.getAttribute("aria-controls")).hidden = !selected;
  });
  urlInput.disabled = busy || source !== "youtube";
  showError("");
}

function setFile(file) {
  selectedFile = file || null;
  byId("file-name").textContent = file ? file.name : "Choose or drop a video file";
  showError("");
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function status(message) {
  progress.textContent = message;
  progress.hidden = !message;
}

function setBusy(value) {
  busy = value;
  fileInput.disabled = value;
  urlInput.disabled = value || source !== "youtube";
  thresholdInput.disabled = value;
  accessCode.disabled = value;
  byId("analyze").disabled = value;
  tabs.forEach((tab) => { tab.disabled = value; });
}

function upload(video, threshold, code) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/upload?threshold=" + threshold);
    request.responseType = "json";
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.setRequestHeader("X-Shotline", "1");
    if (code) request.setRequestHeader("X-Access-Code", code);
    request.setRequestHeader("X-Filename", encodeURIComponent(video.name));
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) status("Receiving video… " + Math.floor(event.loaded / event.total * 100) + "%");
    });
    request.addEventListener("load", () => {
      if (request.status === 202) resolve(request.response);
      else reject(new Error(request.response?.error || "The upload failed."));
    });
    request.addEventListener("error", () => reject(new Error("Could not connect to the analyzer.")));
    request.send(video);
  });
}

async function waitForJob(id, onStatus = status) {
  const messages = {
    queued: "Waiting for the analyzer…",
    downloading: "Downloading the YouTube video…",
    detecting: "Detecting shot boundaries and measuring colors…",
    capturing: "Capturing screenshots…",
  };
  while (true) {
    const job = await json("/api/jobs/" + id);
    if (job.status === "ready") return job.manifest;
    if (job.status === "error") throw new Error(job.error);
    onStatus(messages[job.status] || "Analyzing video…");
    await new Promise((resolve) => setTimeout(resolve, 650));
  }
}

async function finishJob(id) {
  storage(activeJob, id);
  try {
    const manifest = await waitForJob(id);
    await showLocalAnalysis(manifest);
    status("Analysis complete.");
  } finally {
    storage(activeJob, null);
  }
}

async function showLocalAnalysis(manifest, isDefault = false) {
  await renderAnalysis(manifest);
  defaultButton.hidden = isDefault || !hasDefault;
  storage(lastAnalysis, isDefault ? null : manifest);
}

async function showDefault() {
  const manifest = await waitForJob("default", (message) => {
    if (!byId("loading").hidden) byId("loading").textContent = message;
  });
  await showLocalAnalysis(manifest, true);
}

tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => changeSource(tab.id.slice(4)));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[tabs.length - 1]
      : tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    changeSource(next.id.slice(4));
    next.focus();
  });
});
fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
urlInput.addEventListener("input", () => showError(""));
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!busy) dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragleave", (event) => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dragging");
});
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  if (!busy && event.dataTransfer.files.length > 0) {
    fileInput.value = "";
    setFile(event.dataTransfer.files[0]);
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  showError("");
  if (source === "upload" && !selectedFile) {
    showError("Choose a video file to analyze.");
    fileInput.focus();
    return;
  }
  if (source === "upload" && (selectedFile.size === 0 || selectedFile.size > maxUpload)) {
    showError("Choose a video file up to " + (protectedImports ? "250 MB." : "16 GB."));
    fileInput.focus();
    return;
  }

  const url = urlInput.value.trim();
  if (source === "youtube" && !url) {
    showError("Paste a YouTube video URL.");
    urlInput.focus();
    return;
  }
  const code = protectedImports ? accessCode.value.trim() : "";
  if (code && !/^[\x21-\x7e]{24,128}$/.test(code)) {
    showError("Check the access code and try again.");
    accessCode.focus();
    return;
  }

  setBusy(true);
  try {
    status(source === "upload" ? "Receiving video…" : "Starting YouTube import…");
    const threshold = thresholdInput.value;
    const job = source === "upload"
      ? await upload(selectedFile, threshold, code)
      : await json("/api/youtube?threshold=" + threshold, {
          method: "POST",
          headers: {
            "Content-Type": "application/json", "X-Shotline": "1",
            ...(code ? { "X-Access-Code": code } : {}),
          },
          body: JSON.stringify({ url }),
        });
    await finishJob(job.id);
  } catch (failure) {
    status("");
    showError(failure.message);
  } finally {
    setBusy(false);
  }
});

defaultButton.addEventListener("click", async () => {
  try {
    await showDefault();
    if (!busy) status("");
  } catch (failure) {
    showError(failure.message);
  }
});

async function start() {
  changeSource(source);
  try {
    const settings = await json("/api/settings");
    maxUpload = settings.max_upload;
    protectedImports = settings.protected;
    hasDefault = settings.has_default;
    if (protectedImports) {
      byId("quota-hint").textContent = settings.free_imports + " free analyses per 24 hours";
      byId("import-limits").hidden = false;
    }
  } catch {
    // The import form can still show a connection error on submission.
  }
  const saved = storage(lastAnalysis);
  const pending = storage(activeJob);
  try {
    if (saved) await showLocalAnalysis(saved).catch(showDefault);
    else if (hasDefault) await showDefault();
    else byId("loading").textContent = "Paste a YouTube link or choose a video to begin.";
  } catch (failure) {
    byId("loading").textContent = "Could not open this analysis. You can still import a video.";
  }

  if (pending) {
    setBusy(true);
    try {
      await finishJob(pending);
    } catch (failure) {
      status("");
      showError(failure.message);
    } finally {
      setBusy(false);
    }
  }
}

start();
