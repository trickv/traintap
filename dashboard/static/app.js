"use strict";
const $ = (id) => document.getElementById(id);
const COLORS = { EOT: "#4aa8ff", DPU: "#f1c40f", HOT: "#e74c3c" };
let range = "24h";
const charts = {};

const fmtClock = (epoch) =>
  new Date(epoch * 1000).toLocaleString([], { month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit" });
const hourLabel = (h) => h.slice(5).replace(" ", " ");  // "MM-DD HH:00"

Chart.defaults.color = "#8b97a7";
Chart.defaults.borderColor = "#2a323d";
Chart.defaults.animation = false;
Chart.defaults.plugins.legend.labels.boxWidth = 12;

function mkChart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), cfg);
}

const timeAxis = {
  type: "linear",
  ticks: { callback: (v) => new Date(v * 1000).toLocaleTimeString([],
    { hour: "2-digit", minute: "2-digit" }), maxTicksLimit: 8 },
};

async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    const light = $("light");
    light.className = "light";
    if (s.minutes_since === null) {
      light.classList.add("grey");
      $("status-line").textContent = "No packets yet";
      $("status-sub").textContent = "";
    } else if (s.train_near) {
      light.classList.add("green");
      $("status-line").textContent = "🚂 TRAIN NEAR";
      $("status-sub").textContent =
        `unit ${s.last_unit} · ${s.last_pressure} psig · ${s.minutes_since} min ago`;
    } else {
      light.classList.add(s.minutes_since < 60 ? "amber" : "red");
      $("status-line").textContent = "clear";
      $("status-sub").textContent =
        `${s.minutes_since} min since last packet (unit ${s.last_unit})`;
    }
  } catch (e) { /* keep last */ }
}

async function refreshStats() {
  let d;
  try { d = await (await fetch(`/api/stats?range=${range}`)).json(); }
  catch (e) { return; }

  $("c-trains").textContent = d.total_trains;
  $("c-units").textContent = d.unique_units;
  $("c-meets").textContent = d.meets.length;
  $("c-busiest").textContent = d.busiest_hour.count
    ? `${d.busiest_hour.count}` : "0";
  const q = d.decode_quality, qt = q.clean + q.corrected;
  $("c-quality").textContent = qt ? `${Math.round(100 * q.clean / qt)}%` : "–";
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();

  // trains per hour
  mkChart("trainsChart", {
    type: "bar",
    data: { labels: d.trains_per_hour.map((b) => hourLabel(b.hour)),
      datasets: [{ label: "trains", data: d.trains_per_hour.map((b) => b.count),
        backgroundColor: COLORS.EOT }] },
    options: { plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 12 } }, y: { beginAtZero: true, ticks: { precision: 0 } } } },
  });

  // signal strength scatter + rolling median line
  mkChart("signalChart", {
    data: { datasets: [
      { type: "scatter", label: "peak dB", parsing: false,
        data: d.signal_series.map((p) => ({ x: p[0], y: p[1] })),
        backgroundColor: COLORS.EOT, pointRadius: 3 },
      { type: "line", label: "rolling median", parsing: false,
        data: d.signal_median.map((p) => ({ x: p[0], y: p[1] })),
        borderColor: COLORS.DPU, borderWidth: 2, pointRadius: 0 } ] },
    options: { scales: { x: timeAxis, y: { title: { display: true, text: "dB" } } },
      plugins: { tooltip: { callbacks: { title: (t) => fmtClock(t[0].parsed.x) } } } },
  });

  // packets per hour stacked
  mkChart("packetsChart", {
    type: "bar",
    data: { labels: d.packets_per_hour.map((b) => hourLabel(b.hour)),
      datasets: ["EOT", "DPU", "HOT"].map((s) => ({ label: s,
        data: d.packets_per_hour.map((b) => b[s]), backgroundColor: COLORS[s] })) },
    options: { scales: { x: { stacked: true, ticks: { maxTicksLimit: 12 } },
      y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } },
  });

  // source doughnut
  mkChart("sourceChart", {
    type: "doughnut",
    data: { labels: ["EOT", "DPU", "HOT"],
      datasets: [{ data: ["EOT", "DPU", "HOT"].map((s) => d.source_counts[s] || 0),
        backgroundColor: ["EOT", "DPU", "HOT"].map((s) => COLORS[s]) }] },
  });

  // decode quality doughnut
  mkChart("qualityChart", {
    type: "doughnut",
    data: { labels: ["clean", "BCH-corrected"],
      datasets: [{ data: [q.clean, q.corrected],
        backgroundColor: [COLORS.EOT, COLORS.DPU] }] },
  });

  // pressure
  mkChart("pressureChart", {
    type: "scatter",
    data: { datasets: [{ label: "psig", parsing: false,
      data: d.pressure_series.map((p) => ({ x: p[0], y: p[1] })),
      backgroundColor: COLORS.EOT, pointRadius: 2 }] },
    options: { plugins: { legend: { display: false },
      tooltip: { callbacks: { title: (t) => fmtClock(t[0].parsed.x) } } },
      scales: { x: timeAxis, y: { title: { display: true, text: "psig" } } } },
  });

  // hour of day
  mkChart("hodChart", {
    type: "bar",
    data: { labels: [...Array(24).keys()].map((h) => `${h}`),
      datasets: [{ label: "trains", data: d.hour_of_day, backgroundColor: COLORS.DPU }] },
    options: { plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } },
  });

  // recent trains table
  const tb = $("recent").querySelector("tbody");
  tb.innerHTML = "";
  for (const t of d.recent_trains) {
    const tr = document.createElement("tr");
    const notes = [];
    if (t.dpu_units.length) notes.push("DPU " + t.dpu_units.join(","));
    if (t.eot_units.length > 1) notes.push("MEET");
    tr.innerHTML = `<td>${t.start ? t.start.slice(5) : ""}</td>
      <td>${t.eot_units.join(", ") || "—"}</td><td>${t.eot_pkts}</td>
      <td>${t.peak_db ?? "—"}</td><td>${t.duration_s}s</td>
      <td class="${t.meet ? "meet" : ""}">${notes.join(" · ")}</td>`;
    tb.appendChild(tr);
  }
}

document.querySelectorAll(".ranges button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".ranges button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    range = b.dataset.range;
    $("range-label").textContent = " · " + range;
    refreshStats();
  }));

refreshStatus(); refreshStats();
setInterval(refreshStatus, 10_000);
setInterval(refreshStats, 30_000);
