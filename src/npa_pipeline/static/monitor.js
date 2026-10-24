const $ = (sel) => document.querySelector(sel);

const POLL_MS = 3000;
const HISTORY = 100; // точек на спарклайне: 100 × 3 с = 5 минут

const history = { cpu: [], mem: [], gpu: [], vram: [] };

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtBytes(n) {
  if (n == null) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return d ? `${d} д ${h} ч` : `${h} ч ${m} мин`;
}

function fmtUntil(iso) {
  if (!iso) return "—";
  const sec = Math.round((Date.parse(iso) - Date.now()) / 1000);
  if (Number.isNaN(sec)) return "—";
  if (sec > 1e8) return "постоянно";
  if (sec <= 0) return "выгружается";
  return sec >= 60 ? `${Math.floor(sec / 60)} мин ${sec % 60} с` : `${sec} с`;
}

// Порог цвета: ≥ warn — жёлтый, ≥ err — красный (диск 95% на nv-02 должен бросаться в глаза)
function level(pct, warn = 75, err = 90) {
  if (pct == null) return "";
  return pct >= err ? "err" : pct >= warn ? "warn" : "ok";
}

function setMeter(el, pct, warn, err) {
  el.style.width = `${Math.max(0, Math.min(100, pct ?? 0))}%`;
  el.className = `meter-bar ${level(pct, warn, err)}`;
}

function push(key, value) {
  const arr = history[key];
  arr.push(value ?? 0);
  if (arr.length > HISTORY) arr.shift();
}

function spark(svg, values) {
  if (!svg) return;
  const pts = values.map((v, i) => `${(i / (HISTORY - 1)) * 100},${30 - (Math.min(v, 100) / 100) * 28 - 1}`);
  const offset = 100 - ((values.length - 1) / (HISTORY - 1)) * 100;
  svg.innerHTML = values.length > 1
    ? `<g transform="translate(${offset},0)"><polyline points="${pts.join(" ")}" /></g>`
    : "";
}

function renderCpu(cpu) {
  if (!cpu || cpu.available === false) {
    $("#cpu-pct").textContent = "—";
    $("#cpu-sub").textContent = cpu?.error || "нет данных";
    return;
  }
  $("#cpu-pct").textContent = cpu.percent.toFixed(0);
  $("#cpu-pct").className = level(cpu.percent);
  const [l1, l5, l15] = cpu.load;
  $("#cpu-sub").textContent = `${cpu.count} ядер · load ${l1.toFixed(2)} / ${l5.toFixed(2)} / ${l15.toFixed(2)}`;
  push("cpu", cpu.percent);
  spark($("#cpu-spark"), history.cpu);
  $("#cpu-cores").innerHTML = cpu.per_core
    .map((p, i) => `<span class="core ${level(p)}" title="ядро ${i}: ${p.toFixed(0)}%"><i style="height:${Math.max(4, p)}%"></i></span>`)
    .join("");
}

function renderMemory(mem) {
  if (!mem || mem.available === false) {
    $("#mem-sub").textContent = mem?.error || "нет данных";
    return;
  }
  const pct = (mem.used / mem.total) * 100;
  $("#mem-pct").textContent = pct.toFixed(0);
  $("#mem-pct").className = level(pct);
  $("#mem-sub").textContent = `${fmtBytes(mem.used)} из ${fmtBytes(mem.total)} · доступно ${fmtBytes(mem.available)}`;
  setMeter($("#mem-bar"), pct);
  push("mem", pct);
  spark($("#mem-spark"), history.mem);
  $("#swap-sub").textContent = mem.swap_total
    ? `swap: ${fmtBytes(mem.swap_used)} из ${fmtBytes(mem.swap_total)}`
    : "swap отключён";
}

function renderDisk(disk) {
  if (!disk || disk.available === false) {
    $("#disk-sub").textContent = disk?.error || "нет данных";
    return;
  }
  // Как в `df`: доля от места, доступного не-root (5% блоков ext4 зарезервированы),
  // иначе на nv-02 монитор показывал бы 90% при «95%» в df.
  const pct = (disk.used / (disk.used + disk.free)) * 100;
  $("#disk-pct").textContent = pct.toFixed(0);
  $("#disk-pct").className = level(pct, 80, 90);
  $("#disk-sub").textContent = `свободно ${fmtBytes(disk.free)} из ${fmtBytes(disk.total)}`;
  setMeter($("#disk-bar"), pct, 80, 90);
  $("#disk-note").textContent = pct >= 90
    ? "Мало места: проверьте перед загрузкой моделей и docker-образов"
    : "";
}

function renderGpu(gpu) {
  const body = $("#gpu-body");
  if (!gpu || !gpu.available) {
    body.innerHTML = `<span class="mon-note">${esc(gpu?.error || "нет данных")}</span>`;
    return;
  }
  body.innerHTML = gpu.gpus
    .map((g) => {
      const vramPct = g.mem_total ? (g.mem_used / g.mem_total) * 100 : null;
      const power = g.power != null ? `${g.power.toFixed(0)} / ${g.power_limit?.toFixed(0) ?? "—"} Вт` : "—";
      return `
        <div class="gpu">
          <div class="gpu-name">${esc(g.name)}</div>
          <div class="gpu-row">
            <div><div class="mon-big"><span class="${level(g.util)}">${g.util ?? "—"}</span><span class="mon-unit">%</span></div>
              <div class="mon-sub">загрузка</div></div>
            <div><div class="mon-big"><span class="${level(vramPct)}">${vramPct == null ? "—" : vramPct.toFixed(0)}</span><span class="mon-unit">%</span></div>
              <div class="mon-sub">VRAM</div></div>
          </div>
          <div class="mon-sub">VRAM ${fmtBytes(g.mem_used)} из ${fmtBytes(g.mem_total)} · ${g.temp ?? "—"} °C · ${power}</div>
          <div class="meter"><div class="meter-bar ${level(vramPct)}" style="width:${vramPct ?? 0}%"></div></div>
          <svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none" data-spark="gpu${g.index}"></svg>
          <div class="mon-legend"><span class="lg-util">загрузка</span> <span class="lg-vram">VRAM</span></div>
        </div>`;
    })
    .join("");
  // История — только первой карты (на nv-02 она одна)
  const g0 = gpu.gpus[0];
  if (g0) {
    push("gpu", g0.util);
    push("vram", g0.mem_total ? (g0.mem_used / g0.mem_total) * 100 : 0);
    const svg = body.querySelector(`[data-spark="gpu${g0.index}"]`);
    spark(svg, history.gpu);
    if (history.vram.length > 1) {
      const offset = 100 - ((history.vram.length - 1) / (HISTORY - 1)) * 100;
      const pts = history.vram.map((v, i) => `${(i / (HISTORY - 1)) * 100},${30 - (v / 100) * 28 - 1}`);
      svg.insertAdjacentHTML("beforeend", `<g transform="translate(${offset},0)"><polyline class="alt" points="${pts.join(" ")}" /></g>`);
    }
  }
}

function renderOllama(ol) {
  const body = $("#ollama-body");
  $("#ollama-url").textContent = ol?.url ? `· ${ol.url}` : "";
  if (!ol || !ol.available) {
    body.innerHTML = `<span class="mon-note">Ollama недоступна: ${esc(ol?.error || "нет данных")}</span>`;
    return;
  }
  if (!ol.models.length) {
    body.textContent = "Сейчас ни одна модель не загружена";
    return;
  }
  const total = ol.models.reduce((s, m) => s + (m.vram || 0), 0);
  body.innerHTML = `
    <table class="mon-table">
      <thead><tr><th>Модель</th><th>Параметры</th><th>VRAM</th><th>Всего</th><th>Выгрузится через</th></tr></thead>
      <tbody>
        ${ol.models
          .map(
            (m) => `<tr>
              <td class="mono">${esc(m.name)}</td>
              <td>${esc([m.params, m.quant].filter(Boolean).join(" · ") || "—")}</td>
              <td>${fmtBytes(m.vram)}</td>
              <td>${fmtBytes(m.size)}</td>
              <td>${fmtUntil(m.expires_at)}</td>
            </tr>`,
          )
          .join("")}
      </tbody>
      <tfoot><tr><td>Итого в VRAM</td><td></td><td>${fmtBytes(total)}</td><td></td><td></td></tr></tfoot>
    </table>`;
}

let timer = null;

async function tick() {
  const box = $("#mon-status");
  const text = $("#mon-status-text");
  try {
    const res = await fetch("/api/monitor");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    $("#hostname").textContent = d.hostname ? `· ${d.hostname}` : "";
    renderCpu(d.cpu);
    renderMemory(d.memory);
    renderDisk(d.disk);
    renderGpu(d.gpu);
    renderOllama(d.ollama);
    const up = typeof d.uptime === "number" ? ` · аптайм ${fmtUptime(d.uptime)}` : "";
    text.textContent = `обновлено ${new Date().toLocaleTimeString("ru-RU")}${up}`;
    box.className = "status-box ok";
  } catch (e) {
    text.textContent = `нет связи с сервером (${e.message})`;
    box.className = "status-box err";
  }
}

function start() {
  if (timer) return;
  tick();
  timer = setInterval(tick, POLL_MS);
}

function stop() {
  clearInterval(timer);
  timer = null;
}

// Скрытая вкладка не опрашивает сервер
document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
start();
