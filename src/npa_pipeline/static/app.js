const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setBusy(busy) {
  const btn = $("#btn-lookup");
  if (btn) btn.disabled = busy;
}

async function loadHealth() {
  const box = $("#health");
  const text = $("#health-text");
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    const n = data.authorities_cached ?? 0;
    const its = data.its_cached ?? 0;
    text.textContent = `НПА-органы: ${n} · ИТС в кэше: ${its}`;
    box.className = `status-box ${n || its ? "ok" : "warn"}`;
    return data;
  } catch {
    text.textContent = "сервер недоступен";
    box.className = "status-box err";
    return null;
  }
}

function fileActions(file) {
  const label = file.label ? `${esc(file.label)}: ` : "";
  return `
    <div class="file-row">
      <span class="file-label">${label}</span>
      <a class="btn primary" href="${esc(file.open_url)}" target="_blank" rel="noopener">Открыть</a>
      <a class="btn" href="${esc(file.download_url)}" download>Скачать</a>
    </div>
  `;
}

let activeOcrJob = null;
let ocrTimer = null;
let ocrStatusSelector = "#ocr-status";
let ocrStarting = false;
let uploadFile = null;

function updateOcrButtons() {
  const busy = ocrStarting || Boolean(activeOcrJob);
  const upload = $("#btn-upload");
  if (upload) upload.disabled = busy || !uploadFile;
  const lookup = $("#btn-ocr");
  if (lookup) lookup.disabled = busy;
}

function fmtElapsed(sec) {
  const m = Math.floor(sec / 60);
  const s = String(sec % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function ocrStatusHtml(job) {
  const id = esc(job.job_id);
  if (job.status === "running") {
    const pct = job.percent == null ? "—" : `${job.percent.toFixed(1)}%`;
    const page = job.page != null ? `страница ${esc(job.page)} из ${esc(job.total_pages)}` : "подготовка";
    const steps = (job.steps || []).map((x) => `<li>${esc(x)}</li>`).join("");
    return `
      <p class="ocr-line">Распознаётся: ${page} · ${esc(pct)} · прошло ${esc(fmtElapsed(job.elapsed_seconds))}</p>
      ${steps ? `<ul class="ocr-steps">${steps}</ul>` : ""}
      <button type="button" class="btn" data-ocr-cancel="${id}">Остановить</button>`;
  }
  if (job.status === "done") {
    return `
      <p class="ocr-line">Готово за ${esc(fmtElapsed(job.elapsed_seconds))}.</p>
      <a class="btn primary" href="/api/ocr/jobs/${id}/docx" download>Скачать DOCX</a>`;
  }
  if (job.status === "cancelled") {
    return `<p class="ocr-line">Распознавание остановлено.</p>`;
  }
  return `<p class="ocr-line">Ошибка: ${esc(job.error || "распознавание не завершено")}</p>`;
}

function renderOcrStatus(job) {
  const el = $(ocrStatusSelector);
  if (el) el.innerHTML = ocrStatusHtml(job);
}

async function pollOcr() {
  clearTimeout(ocrTimer);
  if (!activeOcrJob) return;
  try {
    const res = await fetch(`/api/ocr/jobs/${encodeURIComponent(activeOcrJob)}`);
    const job = await res.json();
    if (!res.ok) {
      activeOcrJob = null;
      updateOcrButtons();
      const el = $(ocrStatusSelector);
      if (el) el.textContent = typeof job.detail === "string" ? job.detail : "Задание не найдено";
      return;
    }
    renderOcrStatus(job);
    if (job.status === "running") {
      ocrTimer = setTimeout(pollOcr, 5000);
    } else {
      activeOcrJob = null;
      updateOcrButtons();
    }
  } catch (err) {
    ocrTimer = setTimeout(pollOcr, 5000);
  }
}

async function startOcr(eoNumber, authorityName) {
  if (ocrStarting || activeOcrJob) return;
  ocrStatusSelector = "#ocr-status";
  ocrStarting = true;
  updateOcrButtons();
  const el = $("#ocr-status");
  if (el) el.textContent = "Запуск…";
  try {
    const res = await fetch("/api/ocr", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eo_number: eoNumber, authority_name: authorityName || null }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (el) el.textContent = typeof data.detail === "string" ? data.detail : "Ошибка запуска";
      return;
    }
    activeOcrJob = data.job_id;
    renderOcrStatus(data);
    pollOcr();
  } catch (err) {
    if (el) el.textContent = String(err);
  } finally {
    ocrStarting = false;
    updateOcrButtons();
  }
}

async function cancelOcr(jobId) {
  try {
    await fetch(`/api/ocr/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  } finally {
    pollOcr();
  }
}

function renderResult(data) {
  const root = $("#result");
  if (!data) {
    root.className = "result empty";
    root.textContent = "Введите название и нажмите «Найти».";
    return;
  }

  const status = data.status || "unknown";
  const kind = data.kind === "its" ? "ИТС/НДТ" : data.kind === "npa" ? "НПА" : "";

  let body = `
    <div class="result-head">
      <span class="badge ${esc(status)}">${esc(status)}</span>
      ${kind ? `<span class="kind-tag">${esc(kind)}</span>` : ""}
    </div>
  `;

  if (data.description) {
    body += `<p class="doc-description">${esc(data.description)}</p>`;
  } else if (data.title) {
    body += `<p class="doc-description">${esc(data.title)}</p>`;
  }

  if (data.files?.length) {
    body += `<div class="file-actions">${data.files.map(fileActions).join("")}</div>`;
  }

  if (data.ocr?.scan_pages) {
    body += `
      <div class="ocr-box">
        <p class="hint">Страниц без текстового слоя: ${esc(data.ocr.scan_pages)} из ${esc(data.ocr.total_pages)}</p>
        <button type="button" class="btn primary" id="btn-ocr"
          data-eo="${esc(data.ocr.eo_number)}" data-authority="${esc(data.ocr.authority_name || "")}">Распознать скан</button>
        <div id="ocr-status" class="ocr-status"></div>
      </div>`;
  }

  if (data.candidates?.length) {
    body += `<p class="hint">Найдено несколько вариантов — выберите нужный:</p><ul class="candidate-list">`;
    for (const c of data.candidates) {
      const id = c.url_id != null ? `data-url-id="${esc(c.url_id)}"` : "";
      const disabled = c.url_id == null ? "disabled" : "";
      body += `
        <li>
          <span>${esc(c.description || c.title || "—")}</span>
          <button type="button" class="btn" ${id} ${disabled}>Выбрать</button>
        </li>`;
    }
    body += "</ul>";
  }

  if (data.message) {
    body += `<div class="msg">${esc(data.message)}</div>`;
  }

  if (status === "not_found" && data.kind === "its") {
    body += `<div class="msg">Не найдено в локальном кэше ИТС. Обновите кэш через CLI: <code>npa refresh-its</code>.</div>`;
  }

  root.className = "result";
  root.innerHTML = body;
  updateOcrButtons();
  if (activeOcrJob) pollOcr();
}

async function lookup(payload) {
  setBusy(true);
  $("#result").textContent = "Ищем…";
  $("#result").className = "result empty";
  try {
    const res = await fetch("/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      renderResult({
        status: "invalid_input",
        message: typeof data.detail === "string" ? data.detail : "Ошибка запроса",
      });
      return;
    }
    renderResult(data);
  } catch (err) {
    renderResult({ status: "network_error", message: String(err) });
  } finally {
    setBusy(false);
  }
}

$("#lookup-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#query").value.trim();
  if (!q) {
    renderResult({ status: "invalid_input", message: "Укажите название документа" });
    return;
  }
  lookup({ q });
});

$("#result").addEventListener("click", (e) => {
  const ocrBtn = e.target.closest("#btn-ocr");
  if (ocrBtn) {
    startOcr(ocrBtn.dataset.eo, ocrBtn.dataset.authority);
    return;
  }
  const cancelBtn = e.target.closest("button[data-ocr-cancel]");
  if (cancelBtn) {
    cancelOcr(cancelBtn.dataset.ocrCancel);
    return;
  }
  const btn = e.target.closest("button[data-url-id]");
  if (!btn) return;
  const urlId = Number(btn.dataset.urlId);
  const q = $("#query").value.trim() || null;
  lookup({ q, url_id: urlId });
});

function selectPdf(files) {
  const list = Array.from(files || []);
  const status = $("#upload-status");
  if (ocrStarting || activeOcrJob) return;
  uploadFile = null;
  $("#pdf-selected").textContent = "Файл не выбран · до 100 МБ";
  updateOcrButtons();
  if (list.length !== 1 || !list[0].name.toLowerCase().endsWith(".pdf")) {
    status.textContent = "Выберите один PDF-документ.";
    return;
  }
  const file = list[0];
  if (!file.size || file.size > 100 * 1024 * 1024) {
    status.textContent = "PDF должен быть непустым и не больше 100 МБ.";
    return;
  }
  uploadFile = file;
  $("#pdf-selected").textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} МБ`;
  status.textContent = "Файл выбран. Нажмите «Распознать PDF».";
  updateOcrButtons();
}

$("#pdf-file").addEventListener("change", (event) => selectPdf(event.target.files));
const dropZone = $("#pdf-drop-zone");
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", (event) => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("drag-over");
});
// Не даём браузеру открыть перетаскиваемый файл вместо приложения.
document.addEventListener("dragover", (event) => {
  if (Array.from(event.dataTransfer?.types || []).includes("Files")) event.preventDefault();
});
document.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault();
  dropZone.classList.remove("drag-over");
  if (dropZone.contains(event.target)) selectPdf(event.dataTransfer.files);
});
document.addEventListener("paste", (event) => {
  const files = event.clipboardData?.files;
  if (!files?.length) return;
  event.preventDefault();
  selectPdf(files);
});
$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!uploadFile || ocrStarting || activeOcrJob) return;
  ocrStatusSelector = "#upload-status";
  ocrStarting = true;
  updateOcrButtons();
  const status = $("#upload-status");
  status.textContent = "Загрузка PDF и запуск распознавания…";
  const body = new FormData();
  body.append("file", uploadFile);
  body.append("authority_name", $("#upload-authority").value.trim());
  try {
    const response = await fetch("/api/ocr/upload", { method: "POST", body });
    const job = await response.json();
    if (!response.ok) {
      status.textContent = typeof job.detail === "string" ? job.detail : "Не удалось загрузить PDF.";
      return;
    }
    activeOcrJob = job.job_id;
    renderOcrStatus(job);
    pollOcr();
  } catch (error) {
    status.textContent = `Ошибка загрузки: ${error}`;
  } finally {
    ocrStarting = false;
    updateOcrButtons();
  }
});
$("#upload-status").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-ocr-cancel]");
  if (button) cancelOcr(button.dataset.ocrCancel);
});

loadHealth();
