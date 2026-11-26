let qrPollTimer = null;
let currentSnap = null;
let initializedSetupForm = false;

const api = () => window.pywebview?.api;

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

// Navigation
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${tab}`).classList.add("active");
    if (tab === "history") loadHistory();
  });
});

function formatUptime(sec) {
  if (!sec || sec <= 0) return "--:--";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function renderPorts(ports) {
  const grid = document.getElementById("portsGrid");
  grid.innerHTML = "";
  for (const p of ports) {
    const card = document.createElement("div");
    card.className = `port-card ${p.active ? "active" : ""} ${!p.enabled ? "disabled" : ""}`;
    const protoText = p.active ? (p.protocol || "Active") : (p.enabled ? "Chờ tải" : "Đã tắt");
    card.innerHTML = `
      <div class="port-top">
        <span class="port-name">${p.label}</span>
        <span class="proto-badge ${p.active ? "live" : ""}">${protoText}</span>
      </div>
      <div class="port-power">${p.power.toFixed(1)}<small>W</small></div>
      <div class="port-metrics">
        <span>${p.voltage.toFixed(2)} V</span>
        <span>${p.current.toFixed(2)} A</span>
      </div>
      <div class="port-footer">
        <span>${p.energy_wh.toFixed(2)} Wh</span>
        <label class="switch">
          <input type="checkbox" ${p.enabled ? "checked" : ""} data-port-id="${p.id}" />
          <span class="slider"></span>
        </label>
      </div>
    `;
    const toggle = card.querySelector("input[data-port-id]");
    toggle.addEventListener("change", async (e) => {
      const checked = e.target.checked;
      const res = await api().toggle_port(p.id, checked);
      if (!res.ok) {
        e.target.checked = !checked;
        showToast(res.error || "Không thể điều khiển cổng");
      } else {
        showToast(`${p.label}: ${checked ? "Đã bật" : "Đã tắt"}`);
      }
    });
    grid.appendChild(card);
  }
}

function renderChart(points) {
  const canvas = document.getElementById("powerChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  // Grid lines
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    const y = (h / 5) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  if (!points || points.length < 2) {
    ctx.fillStyle = "#64748b";
    ctx.font = "12px Segoe UI";
    ctx.textAlign = "center";
    ctx.fillText("Đang thu thập dữ liệu biểu đồ...", w / 2, h / 2);
    return;
  }

  const maxVal = Math.max(20, ...points.map((pt) => pt.total)) * 1.15;
  const drawSeries = (key, color, lineWidth = 2) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = lineWidth;
    ctx.beginPath();
    points.forEach((pt, idx) => {
      const x = (idx / (points.length - 1)) * w;
      const y = h - (pt[key] / maxVal) * (h - 16) - 8;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };

  drawSeries("c1", "#00ff9d", 1.5);
  drawSeries("c2", "#ff9f43", 1.5);
  drawSeries("c3", "#a55eea", 1.5);
  drawSeries("a", "#ff6b81", 1.5);
  drawSeries("total", "#00d4ff", 2.4);
}

function renderControls(snap) {
  const s = snap.settings || {};
  const sceneVal = String(s["5"] || "");
  document.querySelectorAll("#sceneModeGroup button").forEach((b) => {
    b.classList.toggle("active", b.dataset.val === sceneVal);
  });

  if (s["6"] !== undefined) document.getElementById("selScreenTimeout").value = String(s["6"]);
  if (s["13"] !== undefined) document.getElementById("selLanguage").value = String(s["13"]);
  if (s["19"] !== undefined) document.getElementById("chkIdleScreen").checked = Boolean(s["19"]);
  if (s["20"] !== undefined) document.getElementById("chkScreenLock").checked = Boolean(s["20"]);
  if (s["15"] !== undefined) document.getElementById("chkLowCurrent").checked = Boolean(s["15"]);

  const sw = snap.protocol_switches || {};
  const rows = [
    { key: "c1", label: "USB-C1", protos: ["pd", "pps", "ufcs"] },
    { key: "c2", label: "USB-C2", protos: ["pd", "pps", "ufcs"] },
    { key: "c3", label: "USB-C3", protos: ["ufcs", "scp"] },
    { key: "a", label: "USB-A", protos: ["ufcs", "scp"] },
  ];
  const matrix = document.getElementById("protoMatrix");
  matrix.innerHTML = "";
  for (const r of rows) {
    const rowEl = document.createElement("div");
    rowEl.className = "proto-row";
    const portSw = sw[r.key] || {};
    rowEl.innerHTML = `<span>${r.label}</span><div class="proto-chips"></div>`;
    const chips = rowEl.querySelector(".proto-chips");
    for (const p of r.protos) {
      const btn = document.createElement("button");
      const isOn = Boolean(portSw[p]);
      btn.className = `chip-btn ${isOn ? "on" : ""}`;
      btn.textContent = p.toUpperCase();
      btn.addEventListener("click", async () => {
        const res = await api().set_protocol_switch(r.key, p, !isOn);
        if (!res.ok) showToast(res.error || "Lỗi cập nhật giao thức");
        else showToast(`${r.label} ${p.toUpperCase()}: ${!isOn ? "Bật" : "Tắt"}`);
      });
      chips.appendChild(btn);
    }
    matrix.appendChild(rowEl);
  }
}

async function refreshSnapshot() {
  if (!api()) return;
  try {
    const snap = await api().get_snapshot();
    currentSnap = snap;

    document.getElementById("deviceName").textContent = snap.device_name || "CUKTECH 10 Ultra";
    document.getElementById("statusText").textContent = snap.status_text || "";

    const dot = document.getElementById("connDot");
    dot.classList.remove("online", "connecting");
    if (snap.connected && snap.authenticated) dot.classList.add("online");
    else if (snap.has_config) dot.classList.add("connecting");

    document.getElementById("rssiPill").textContent =
      snap.rssi !== null && snap.rssi !== undefined ? `${snap.rssi} dBm` : "-- dBm";

    const totalW = snap.total_power || 0;
    document.getElementById("totalPower").textContent = totalW.toFixed(1);
    const pct = Math.min(1, totalW / (snap.max_power || 120));
    const circumference = 477.5;
    document.getElementById("gaugeArc").style.strokeDashoffset = String(circumference * (1 - pct));

    document.getElementById("activePorts").textContent = `${snap.active_ports} / 4`;
    document.getElementById("todayWh").textContent = `${(snap.today_wh || 0).toFixed(2)} Wh`;
    document.getElementById("uptimeVal").textContent = formatUptime(snap.uptime_sec);

    renderPorts(snap.ports || []);
    renderChart(snap.chart || []);
    renderControls(snap);

    if (!initializedSetupForm) {
      initializedSetupForm = true;
      document.getElementById("cfgName").value = snap.device_name || "CUKTECH 10 Ultra";
      document.getElementById("cfgMac").value = snap.mac || "";
      const autoStartEl = document.getElementById("chkAutoStart");
      if (autoStartEl) autoStartEl.checked = Boolean(snap.auto_start);
      if (!snap.has_config) {
        // Switch straight to Setup tab if unconfigured
        document.querySelector('.nav-btn[data-tab="setup"]')?.click();
      }
    }
  } catch (e) {
    // Ignore transient startup race
  }
}

async function loadHistory() {
  if (!api()) return;
  const sessions = await api().get_sessions();
  const list = document.getElementById("sessionsList");
  list.innerHTML = "";
  if (!sessions.length) {
    list.innerHTML = `<div class="card muted-text">Chưa có phiên sạc nào được ghi nhận.</div>`;
    return;
  }
  for (const s of sessions) {
    const dt = new Date(s.start_time * 1000);
    const timeStr = dt.toLocaleString("vi-VN");
    const durSec = s.end_time ? Math.max(0, Math.round(s.end_time - s.start_time)) : null;
    const durText = durSec !== null ? `${Math.floor(durSec / 60)}p ${durSec % 60}s` : "Đang sạc";
    const card = document.createElement("div");
    card.className = "session-card";
    card.innerHTML = `
      <div>
        <strong>${s.port_name} · ${s.protocol || "N/A"}</strong>
        <small>${timeStr} · ${durText}</small>
      </div>
      <div class="session-right">
        <div class="wh">${Number(s.energy_wh || 0).toFixed(2)} Wh</div>
        <small>Đỉnh ${Number(s.peak_power || 0).toFixed(1)} W</small>
      </div>
    `;
    list.appendChild(card);
  }
}

// Wire controls
document.getElementById("btnWidgetMode")?.addEventListener("click", () => {
  api()?.switch_to_widget();
});

document.getElementById("chkAutoStart")?.addEventListener("change", async (e) => {
  const checked = e.target.checked;
  const res = await api().set_auto_start(checked);
  if (res.ok) showToast(checked ? "Đã bật khởi động cùng Windows" : "Đã tắt khởi động cùng Windows");
  else {
    e.target.checked = !checked;
    showToast(res.error || "Không thể thay đổi cài đặt khởi động");
  }
});

document.getElementById("btnReconnect").addEventListener("click", async () => {
  await api().reconnect();
  showToast("Đang kết nối lại Bluetooth...");
});

document.querySelectorAll("#sceneModeGroup button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const res = await api().set_setting(5, Number(btn.dataset.val));
    if (!res.ok) showToast(res.error || "Lỗi đổi chế độ");
    else showToast("Đã đổi chế độ phân bổ công suất");
  });
});

document.getElementById("selScreenTimeout").addEventListener("change", async (e) => {
  const res = await api().set_setting(6, Number(e.target.value));
  if (!res.ok) showToast(res.error || "Lỗi cập nhật");
});

document.getElementById("selLanguage").addEventListener("change", async (e) => {
  const res = await api().set_setting(13, Number(e.target.value));
  if (!res.ok) showToast(res.error || "Lỗi cập nhật");
});

document.getElementById("chkIdleScreen").addEventListener("change", async (e) => {
  const res = await api().set_setting(19, e.target.checked ? 1 : 0);
  if (!res.ok) showToast(res.error || "Lỗi cập nhật");
});

document.getElementById("chkScreenLock").addEventListener("change", async (e) => {
  const res = await api().set_setting(20, e.target.checked ? 1 : 0);
  if (!res.ok) showToast(res.error || "Lỗi cập nhật");
});

document.getElementById("chkLowCurrent").addEventListener("change", async (e) => {
  const res = await api().set_setting(15, e.target.checked ? 1 : 0);
  if (!res.ok) showToast(res.error || "Lỗi cập nhật");
});

document.getElementById("btnExportCsv").addEventListener("click", async () => {
  const res = await api().export_csv();
  if (res.ok) showToast(`Đã xuất CSV: ${res.path}`);
  else showToast(res.error || "Xuất CSV thất bại");
});

// BLE Scan
document.getElementById("btnScanBle").addEventListener("click", async () => {
  const btn = document.getElementById("btnScanBle");
  const list = document.getElementById("bleScanList");
  btn.disabled = true;
  btn.textContent = "Đang quét...";
  list.innerHTML = "";
  const res = await api().scan_ble_devices();
  btn.disabled = false;
  btn.textContent = "Quét thiết bị BLE (4 giây)";
  if (!res.ok || !res.devices.length) {
    list.innerHTML = `<div class="muted-text">Không tìm thấy thiết bị MiOT/Cuktech nào gần đây.</div>`;
    return;
  }
  for (const d of res.devices) {
    const item = document.createElement("div");
    item.className = "device-item";
    item.innerHTML = `
      <div>
        <strong>${d.name}</strong>
        <small>${d.mac} · ${d.rssi} dBm</small>
      </div>
      <button class="btn-secondary">Điền MAC</button>
    `;
    item.querySelector("button").addEventListener("click", () => {
      document.getElementById("cfgMac").value = d.mac;
      showToast(`Đã điền MAC ${d.mac}`);
    });
    list.appendChild(item);
  }
});

// QR Login
document.getElementById("btnStartQr").addEventListener("click", async () => {
  const server = document.getElementById("qrServer").value;
  const qrBox = document.getElementById("qrBox");
  const qrImg = document.getElementById("qrImg");
  const qrStatus = document.getElementById("qrStatus");
  const devList = document.getElementById("qrDevicesList");
  devList.innerHTML = "";

  if (qrPollTimer) clearInterval(qrPollTimer);
  const res = await api().start_qr_login(server);
  if (!res.ok) {
    showToast(res.error || "Không tạo được mã QR");
    return;
  }
  qrImg.src = res.qr_image;
  qrBox.style.display = "block";
  qrStatus.textContent = "Mở app Mi Home → Quét mã QR trên màn hình...";

  qrPollTimer = setInterval(async () => {
    const poll = await api().poll_qr_login();
    if (poll.status === "waiting") return;
    clearInterval(qrPollTimer);
    qrPollTimer = null;
    if (!poll.ok) {
      qrStatus.textContent = poll.error || "Đăng nhập QR thất bại";
      return;
    }
    qrBox.style.display = "none";
    const devices = poll.devices || [];
    if (!devices.length) {
      devList.innerHTML = `<div class="muted-text">Đăng nhập thành công nhưng không tìm thấy thiết bị nào trong server "${server}". Hãy thử đổi server (vd: cn hoặc sg).</div>`;
      return;
    }
    for (const d of devices) {
      const item = document.createElement("div");
      item.className = "device-item";
      item.innerHTML = `
        <div>
          <strong>${d.name} ${d.is_cuktech ? "⚡" : ""}</strong>
          <small>${d.model} · ${d.mac}</small>
        </div>
        <button class="btn-primary">Chọn & Kết nối</button>
      `;
      item.querySelector("button").addEventListener("click", async () => {
        await api().save_device_config(d.mac, d.token, d.ble_key, d.name);
        document.getElementById("cfgName").value = d.name;
        document.getElementById("cfgMac").value = d.mac;
        document.getElementById("cfgToken").value = d.token;
        document.getElementById("cfgBleKey").value = d.ble_key;
        showToast("Đã lưu cấu hình! Đang kết nối BLE...");
        document.querySelector('.nav-btn[data-tab="monitor"]')?.click();
      });
      devList.appendChild(item);
    }
  }, 1500);
});

// Manual save
document.getElementById("btnSaveCfg").addEventListener("click", async () => {
  const name = document.getElementById("cfgName").value.trim();
  const mac = document.getElementById("cfgMac").value.trim();
  const token = document.getElementById("cfgToken").value.trim();
  const bleKey = document.getElementById("cfgBleKey").value.trim();
  if (!mac || !token) {
    showToast("Vui lòng nhập đầy đủ MAC và Token");
    return;
  }
  await api().save_device_config(mac, token, bleKey, name);
  showToast("Đã lưu cấu hình! Đang kết nối...");
  document.querySelector('.nav-btn[data-tab="monitor"]')?.click();
});

window.addEventListener("pywebviewready", () => {
  refreshSnapshot();
  setInterval(refreshSnapshot, 600);
});
