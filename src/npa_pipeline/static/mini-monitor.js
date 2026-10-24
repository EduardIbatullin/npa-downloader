// Мини-монитор ресурсов сервера в шапке главной (полная версия — /monitor).
// Наведение (или клик на тач-экране) раскрывает панель с деталями и сервисами.
(() => {
  const POLL_MS = 5000;
  const root = document.getElementById("mini-mon");
  if (!root) return;
  const btn = root.querySelector(".mini-mon-btn");
  const pop = root.querySelector(".mini-mon-pop");
  const dot = root.querySelector(".dot");

  const esc = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  const gb = (n) => (n == null ? "—" : (n / 1024 ** 3).toFixed(1));
  const pctStr = (p) => (p == null ? "—" : `${p.toFixed(1)}%`);

  function cores(n) {
    const m10 = n % 10;
    const m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return `${n} ядро`;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return `${n} ядра`;
    return `${n} ядер`;
  }

  function level(pct, warn = 75, err = 90) {
    if (pct == null) return "";
    return pct >= err ? "err" : pct >= warn ? "warn" : "ok";
  }

  function setBar(key, pct, lvl) {
    const g = root.querySelector(`.mm-g[data-k="${key}"]`);
    const fill = g.querySelector("i");
    fill.style.height = pct == null ? "0" : `${Math.max(6, Math.min(100, pct))}%`;
    fill.className = lvl ?? level(pct);
    g.title = `${g.dataset.label}: ${pctStr(pct)}`;
  }

  function row(label, value, cls = "") {
    return `<div class="mm-row"><span>${label}:</span><b class="${cls}">${value}</b></div>`;
  }

  function render(d) {
    const cpu = d.cpu?.available === false ? null : d.cpu;
    const mem = d.memory?.available === false ? null : d.memory;
    const disk = d.disk?.available === false ? null : d.disk;
    const g = d.gpu?.available ? d.gpu.gpus[0] : null;

    const memPct = mem ? (mem.used / mem.total) * 100 : null;
    const vramPct = g?.mem_total ? (g.mem_used / g.mem_total) * 100 : null;
    // Свободно — как в df: от места, доступного не-root (см. monitor.js renderDisk)
    const freePct = disk ? (disk.free / (disk.used + disk.free)) * 100 : null;

    setBar("cpu", cpu?.percent ?? null);
    setBar("ram", memPct);
    setBar("gpu", g?.util ?? null);
    setBar("vram", vramPct);

    const services = d.services || [];
    const allOk = services.length > 0 && services.every((s) => s.ok);
    dot.className = `dot ${allOk ? "ok" : "warn"}`;

    const gpuName = g ? g.name.replace(/^NVIDIA\s+(GeForce\s+)?/, "") : null;
    const temp = g?.temp;
    pop.innerHTML = `
      <div class="mm-title">Системные ресурсы${d.hostname ? ` · ${esc(d.hostname)}` : ""}</div>
      ${row("CPU", cpu ? `${pctStr(cpu.percent)} (${cores(cpu.count)})` : "—")}
      ${row("RAM", mem ? `${pctStr(memPct)} (${gb(mem.used)}/${gb(mem.total)} ГБ)` : "—")}
      ${row("GPU", g ? `${pctStr(g.util)} · ${esc(gpuName)}` : "недоступен")}
      ${row("VRAM", g ? `${pctStr(vramPct)} (${gb(g.mem_used)}/${gb(g.mem_total)} ГБ)` : "—")}
      ${row("Температура GPU", temp == null ? "—" : `${temp.toFixed(0)}°C`, level(temp, 70, 83))}
      ${row("Свободно на диске", freePct == null ? "—" : `${freePct.toFixed(1)}% (${gb(disk.free)} ГБ)`,
            freePct == null ? "" : freePct < 10 ? "err" : freePct < 20 ? "warn" : "")}
      <div class="mm-title mm-sep">Сервисы</div>
      ${services
        .map((s) => `<div class="mm-row" title="${esc(s.detail)}"><span>${esc(s.name)}:</span>
          <b class="${s.ok ? "ok" : "err"}">${s.ok ? "✓" : "✗"}</b></div>`)
        .join("")}
      <a class="mm-more" href="/monitor">Подробнее →</a>`;
  }

  let timer = null;

  async function tick() {
    try {
      const res = await fetch("/api/monitor");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      render(await res.json());
    } catch (e) {
      dot.className = "dot err";
      pop.innerHTML = `<div class="mm-title">Системные ресурсы</div>
        <div class="mm-row"><span>Нет связи с сервером</span><b class="err">${esc(e.message)}</b></div>`;
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

  // Тач-экраны: hover нет — панель открывается/закрывается по нажатию
  btn.addEventListener("click", () => {
    const open = root.classList.toggle("open");
    btn.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) {
      root.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") root.classList.remove("open");
  });

  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();
})();
