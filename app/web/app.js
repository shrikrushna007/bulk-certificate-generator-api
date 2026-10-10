"use strict";

const state = { inputMode: "json", jobId: null, job: null, offset: 0, limit: 100, pollTimer: null };
const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  collectElements();
  bindEvents();
  elements.issueDate.value = new Date().toISOString().slice(0, 10);
  addRecipientRow();
  checkHealth();
  const savedJobId = localStorage.getItem("certflow.currentJobId");
  if (savedJobId) {
    elements.jobId.value = savedJobId;
    loadJob(savedJobId, true);
  }
});

function collectElements() {
  const ids = [
    "health-dot", "health-label", "create-view", "monitor-view", "json-mode", "csv-mode",
    "job-form", "certificate-title", "issuing-organization", "issue-date", "idempotency-key",
    "recipient-list-panel", "csv-panel", "recipient-rows", "add-recipient", "csv-file",
    "selected-file", "form-message", "submit-job", "job-lookup", "job-id", "empty-state",
    "job-dashboard", "job-title", "job-status", "job-metadata", "display-job-id", "copy-job-id",
    "refresh-job", "request-archive", "progress-percentage", "progress-bar", "total-count",
    "success-count", "failure-count", "pending-count", "archive-message", "status-filter",
    "recipient-results", "page-summary", "previous-page", "next-page"
  ];
  ids.forEach((id) => { elements[toCamelCase(id)] = document.getElementById(id); });
  elements.tabs = [...document.querySelectorAll(".tab")];
}

function bindEvents() {
  elements.tabs.forEach((tab) => tab.addEventListener("click", () => showView(tab.dataset.view)));
  elements.jsonMode.addEventListener("click", () => setInputMode("json"));
  elements.csvMode.addEventListener("click", () => setInputMode("csv"));
  elements.addRecipient.addEventListener("click", () => addRecipientRow());
  elements.csvFile.addEventListener("change", updateSelectedFile);
  elements.jobForm.addEventListener("submit", submitJob);
  elements.jobLookup.addEventListener("submit", (event) => {
    event.preventDefault();
    const jobId = elements.jobId.value.trim();
    if (jobId) loadJob(jobId, true);
  });
  elements.refreshJob.addEventListener("click", () => loadJob(state.jobId, false));
  elements.requestArchive.addEventListener("click", requestArchive);
  elements.copyJobId.addEventListener("click", copyJobId);
  elements.previousPage.addEventListener("click", () => changePage(-1));
  elements.nextPage.addEventListener("click", () => changePage(1));
  elements.statusFilter.addEventListener("change", renderRecipients);
}

function showView(view) {
  elements.tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.view === view));
  elements.createView.classList.toggle("active", view === "create");
  elements.monitorView.classList.toggle("active", view === "monitor");
}

function setInputMode(mode) {
  state.inputMode = mode;
  elements.jsonMode.classList.toggle("active", mode === "json");
  elements.csvMode.classList.toggle("active", mode === "csv");
  elements.recipientListPanel.classList.toggle("hidden", mode !== "json");
  elements.csvPanel.classList.toggle("hidden", mode !== "csv");
  hideMessage(elements.formMessage);
}

function addRecipientRow(name = "", email = "") {
  const row = document.createElement("div");
  row.className = "recipient-row";
  const number = document.createElement("span");
  number.className = "recipient-number";
  const nameLabel = labeledInput("Name", "recipient-name", "Recipient name", name, "text", 220);
  const emailLabel = labeledInput("Email (optional)", "recipient-email", "recipient@example.com", email, "email", 340);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "icon-button";
  remove.setAttribute("aria-label", "Remove recipient");
  remove.textContent = "×";
  remove.addEventListener("click", () => { row.remove(); renumberRows(); });
  row.append(number, nameLabel, emailLabel, remove);
  elements.recipientRows.appendChild(row);
  renumberRows();
}

function labeledInput(labelText, className, placeholder, value, type, maxLength) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.className = className;
  input.placeholder = placeholder;
  input.value = value;
  input.type = type;
  input.maxLength = maxLength;
  label.appendChild(input);
  return label;
}

function renumberRows() {
  const rows = [...elements.recipientRows.querySelectorAll(".recipient-row")];
  rows.forEach((row, index) => {
    row.querySelector(".recipient-number").textContent = String(index + 1).padStart(2, "0");
    row.querySelector(".icon-button").disabled = rows.length === 1;
  });
}

function updateSelectedFile() {
  const file = elements.csvFile.files[0];
  elements.selectedFile.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "No file selected";
}

async function submitJob(event) {
  event.preventDefault();
  hideMessage(elements.formMessage);
  const common = readCommonFields();
  if (!common) return;
  setSubmitting(true);
  try {
    const response = state.inputMode === "csv" ? await submitCsvJob(common) : await submitJsonJob(common);
    const created = await parseResponse(response);
    state.offset = 0;
    elements.jobId.value = created.job_id;
    localStorage.setItem("certflow.currentJobId", created.job_id);
    showMessage(elements.formMessage, `Job ${created.job_id} was accepted.`, true);
    await loadJob(created.job_id, true);
  } catch (error) {
    showMessage(elements.formMessage, error.message || "The job could not be created.");
  } finally {
    setSubmitting(false);
  }
}

function readCommonFields() {
  const common = {
    title: elements.certificateTitle.value.trim(),
    organization: elements.issuingOrganization.value.trim(),
    issueDate: elements.issueDate.value,
    idempotencyKey: elements.idempotencyKey.value.trim()
  };
  if (!common.title || !common.organization || !common.issueDate) {
    showMessage(elements.formMessage, "Complete the certificate title, organization, and issue date.");
    return null;
  }
  return common;
}

function submitJsonJob(common) {
  const recipients = [...elements.recipientRows.querySelectorAll(".recipient-row")].map((row) => ({
    name: row.querySelector(".recipient-name").value,
    email: row.querySelector(".recipient-email").value || null
  }));
  if (!recipients.length) throw new Error("Add at least one recipient.");
  return fetch("/api/v1/jobs", {
    method: "POST",
    headers: requestHeaders(common.idempotencyKey, true),
    body: JSON.stringify({
      certificate: { title: common.title, issuing_organization: common.organization, issue_date: common.issueDate },
      recipients
    })
  });
}

function submitCsvJob(common) {
  const file = elements.csvFile.files[0];
  if (!file) throw new Error("Choose a CSV file before creating the job.");
  const form = new FormData();
  form.append("title", common.title);
  form.append("issuing_organization", common.organization);
  form.append("issue_date", common.issueDate);
  form.append("recipients", file);
  return fetch("/api/v1/jobs/import", { method: "POST", headers: requestHeaders(common.idempotencyKey, false), body: form });
}

function requestHeaders(idempotencyKey, includeJson) {
  const headers = {};
  if (includeJson) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return headers;
}

async function loadJob(jobId, switchView) {
  if (!jobId) return;
  clearTimeout(state.pollTimer);
  try {
    const params = new URLSearchParams({ recipient_offset: state.offset, recipient_limit: state.limit });
    const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}?${params}`);
    const job = await parseResponse(response);
    state.jobId = job.job_id;
    state.job = job;
    localStorage.setItem("certflow.currentJobId", job.job_id);
    elements.jobId.value = job.job_id;
    renderJob();
    if (switchView) showView("monitor");
    if (["PENDING", "PROCESSING"].includes(job.status) || ["PENDING", "PROCESSING"].includes(job.archive_status)) {
      state.pollTimer = setTimeout(() => loadJob(job.job_id, false), 1500);
    }
  } catch (error) {
    elements.emptyState.classList.remove("hidden");
    elements.jobDashboard.classList.add("hidden");
    elements.emptyState.querySelector("h3").textContent = "Job unavailable";
    elements.emptyState.querySelector("p").textContent = error.message || "The job could not be loaded.";
    if (switchView) showView("monitor");
  }
}

function renderJob() {
  const job = state.job;
  elements.emptyState.classList.add("hidden");
  elements.jobDashboard.classList.remove("hidden");
  elements.jobTitle.textContent = job.certificate_title;
  elements.jobStatus.textContent = job.status.replaceAll("_", " ");
  elements.jobStatus.className = `status-badge ${job.status.toLowerCase().replaceAll("_", "-")}`;
  elements.jobMetadata.textContent = `${job.issuing_organization} · Issued ${formatDate(job.issue_date)}`;
  elements.displayJobId.textContent = job.job_id;
  elements.progressPercentage.textContent = `${job.progress_percentage.toFixed(2)}%`;
  elements.progressBar.style.width = `${job.progress_percentage}%`;
  elements.progressBar.parentElement.setAttribute("aria-valuenow", job.progress_percentage);
  elements.totalCount.textContent = job.total_recipients.toLocaleString();
  elements.successCount.textContent = job.successful_generations.toLocaleString();
  elements.failureCount.textContent = job.failed_generations.toLocaleString();
  elements.pendingCount.textContent = job.pending_generations.toLocaleString();
  elements.requestArchive.disabled = ["PENDING", "PROCESSING"].includes(job.status) || ["PENDING", "PROCESSING"].includes(job.archive_status);
  elements.requestArchive.textContent = archiveButtonLabel(job.archive_status, job.archive_progress_percentage ?? 0);
  renderArchive(job);
  renderRecipients();
  renderPagination();
}

function renderArchive(job) {
  if (job.archive_status === "NOT_REQUESTED") { hideMessage(elements.archiveMessage); return; }
  if (job.archive_status === "COMPLETED" && job.archive_download_url) {
    elements.archiveMessage.replaceChildren();
    elements.archiveMessage.className = "archive-message success";
    const link = document.createElement("a");
    link.href = job.archive_download_url;
    link.textContent = "Download ZIP";
    elements.archiveMessage.append(document.createTextNode("The job archive is ready. "), link);
    return;
  }
  let message = "The ZIP archive is queued.";
  if (job.archive_status === "PROCESSING") {
    const processedCount = job.archive_processed_files ?? 0;
    const totalCount = job.archive_total_files ?? job.successful_generations;
    const progress = job.archive_progress_percentage ?? 0;
    const processed = processedCount.toLocaleString();
    const total = totalCount.toLocaleString();
    const percentage = progress.toFixed(1);
    const details = [`${processed} of ${total} files (${percentage}%)`];
    if (job.archive_elapsed_seconds !== null) {
      details.push(`elapsed ${formatDuration(job.archive_elapsed_seconds)}`);
    }
    if (job.archive_estimated_remaining_seconds !== null) {
      details.push(`about ${formatDuration(job.archive_estimated_remaining_seconds)} remaining`);
    }
    message = `Preparing ZIP: ${details.join(" · ")}.`;
  } else if (job.archive_status === "FAILED") {
    message = "Archive generation failed. You can request it again.";
  }
  showMessage(elements.archiveMessage, message, job.archive_status !== "FAILED");
}

function renderRecipients() {
  const filter = elements.statusFilter.value;
  const recipients = (state.job?.recipients || []).filter((recipient) => filter === "ALL" || recipient.status === filter);
  elements.recipientResults.replaceChildren();
  if (!recipients.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No recipient outcomes match this view.";
    row.appendChild(cell);
    elements.recipientResults.appendChild(row);
    return;
  }
  recipients.forEach((recipient) => elements.recipientResults.appendChild(recipientRow(recipient)));
}

function recipientRow(recipient) {
  const row = document.createElement("tr");
  const identity = document.createElement("td");
  const name = document.createElement("span");
  name.className = "recipient-primary";
  name.textContent = recipient.recipient_name || "Invalid recipient";
  const index = document.createElement("span");
  index.className = "recipient-secondary";
  index.textContent = `Position ${recipient.recipient_index + 1}`;
  identity.append(name, index);
  const email = document.createElement("td");
  email.textContent = recipient.recipient_email || "—";
  const status = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `status-badge ${recipient.status.toLowerCase()}`;
  badge.textContent = recipient.status;
  status.appendChild(badge);
  const result = document.createElement("td");
  result.className = recipient.error_message ? "outcome-error" : "";
  result.textContent = recipient.error_message || (recipient.sha256 ? `SHA-256 ${recipient.sha256.slice(0, 12)}…` : "—");
  const action = document.createElement("td");
  action.className = "action-column";
  if (recipient.download_url) {
    const link = document.createElement("a");
    link.href = recipient.download_url;
    link.textContent = "Download PDF";
    action.appendChild(link);
  } else action.textContent = "—";
  row.append(identity, email, status, result, action);
  return row;
}

function renderPagination() {
  const page = state.job.recipient_page;
  if (!page) return;
  const start = page.total ? page.offset + 1 : 0;
  const end = page.offset + page.returned;
  elements.pageSummary.textContent = `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${page.total.toLocaleString()}`;
  elements.previousPage.disabled = page.offset === 0;
  elements.nextPage.disabled = end >= page.total;
}

function changePage(direction) {
  state.offset = Math.max(0, state.offset + direction * state.limit);
  loadJob(state.jobId, false);
}

async function requestArchive() {
  if (!state.jobId) return;
  elements.requestArchive.disabled = true;
  try {
    const response = await fetch(`/api/v1/jobs/${encodeURIComponent(state.jobId)}/archive`, { method: "POST" });
    await parseResponse(response);
    showMessage(elements.archiveMessage, "The ZIP archive has been requested.", true);
    await loadJob(state.jobId, false);
  } catch (error) {
    showMessage(elements.archiveMessage, error.message || "The archive could not be requested.");
    elements.requestArchive.disabled = false;
  }
}

async function copyJobId() {
  if (!state.jobId) return;
  try {
    await navigator.clipboard.writeText(state.jobId);
    elements.copyJobId.textContent = "Copied";
    setTimeout(() => { elements.copyJobId.textContent = "Copy"; }, 1200);
  } catch { elements.copyJobId.textContent = "Copy failed"; }
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error();
    elements.healthDot.className = "health-dot online";
    elements.healthLabel.textContent = "Service online";
  } catch {
    elements.healthDot.className = "health-dot offline";
    elements.healthLabel.textContent = "Service unavailable";
  }
}

async function parseResponse(response) {
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const error = body?.error;
    const details = error?.details?.map((item) => item.message).join("; ");
    throw new Error(details || error?.message || `Request failed with HTTP ${response.status}.`);
  }
  return body;
}

function setSubmitting(submitting) {
  elements.submitJob.disabled = submitting;
  elements.submitJob.querySelector(".button-label").textContent = submitting ? "Creating job" : "Create generation job";
  elements.submitJob.querySelector(".spinner").classList.toggle("hidden", !submitting);
}

function showMessage(element, message, success = false) {
  element.textContent = message;
  const base = element.id === "form-message" ? "message" : "archive-message";
  element.className = success ? `${base} success` : base;
}

function hideMessage(element) { element.classList.add("hidden"); }
function archiveButtonLabel(status, progress = 0) {
  if (status === "COMPLETED") return "ZIP ready";
  if (status === "PENDING") return "ZIP queued";
  if (status === "PROCESSING") return `Preparing ZIP ${progress.toFixed(0)}%`;
  if (status === "FAILED") return "Retry ZIP";
  return "Prepare ZIP";
}
function formatDate(value) { return new Date(`${value}T00:00:00`).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
function formatBytes(bytes) { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function formatDuration(seconds) {
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) return `${rounded}s`;
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  if (minutes < 60) return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}
function toCamelCase(value) { return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()); }
