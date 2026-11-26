const api = () => window.pywebview?.api;

async function refreshWidget() {
  if (!api()) return;
  try {
    const snap = await api().get_snapshot();
    const dot = document.getElementById("wDot");
    dot.classList.remove("online", "connecting");
    if (snap.connected && snap.authenticated) dot.classList.add("online");
    else if (snap.has_config) dot.classList.add("connecting");

    const rssiEl = document.getElementById("wRssi");
    if (snap.rssi !== null && snap.rssi !== undefined) {
      rssiEl.textContent = `${snap.rssi}dBm`;
      rssiEl.style.display = "inline-block";
    } else {
      rssiEl.style.display = "none";
    }

    document.getElementById("wTotal").textContent = (snap.total_power || 0).toFixed(1);
    document.getElementById("wSub").textContent =
      snap.connected && snap.authenticated
        ? `${snap.active_ports}/4 cổng · ${(snap.today_wh || 0).toFixed(1)} Wh`
        : snap.status_text || "Đang kết nối...";

    const portsEl = document.getElementById("wPorts");
    portsEl.innerHTML = "";
    const shortNames = { c1: "C1", c2: "C2", c3: "C3", a: "A" };
    for (const p of snap.ports || []) {
      const el = document.createElement("div");
      el.className = `w-port ${p.active ? "active" : ""}`;
      el.innerHTML = `
        <div class="wp-name">${shortNames[p.key] || p.label}</div>
        <div class="wp-val">${p.active ? p.power.toFixed(0) + "W" : "--"}</div>
        <div class="wp-proto">${p.active ? (p.protocol || "") : ""}</div>
      `;
      portsEl.appendChild(el);
    }
  } catch (e) {
    // Ignore transient startup race
  }
}

document.getElementById("btnExpand").addEventListener("click", () => {
  api()?.switch_to_main();
});

document.getElementById("btnClose").addEventListener("click", () => {
  api()?.quit_app();
});

window.addEventListener("pywebviewready", () => {
  refreshWidget();
  setInterval(refreshWidget, 600);
});
