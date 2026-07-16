/* RGBC Drive Dashboard — Sprint 3.5
 * Vanilla JS, no build step. Polls /api/dashboard/* every 5 seconds.
 *
 * The dashboard is loopback-only on the master; verify_localhost rejects
 * any non-127.0.0.1 / ::1 client. No JWT needed — same-origin localhost.
 */

(() => {
  "use strict";

  // ── Polling ────────────────────────────────────────────────────────
  const REFRESH_MS = 5000;
  let pollTimer = null;
  let isPaginating = false;
  let filesState = { offset: 0, limit: 50, total: 0, q: "", status: "" };

  // ── Helpers ────────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2), v);
      } else if (v !== null && v !== undefined && v !== false) {
        node.setAttribute(k, v);
      }
    }
    for (const c of children) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }
  function fmtBytes(n) {
    if (n == null || n === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 && i > 0 ? 1 : 0) + " " + units[i];
  }
  function fmtRelativeTime(iso) {
    if (!iso) return "—";
    try {
      const t = new Date(iso).getTime();
      const diff = Date.now() - t;
      if (diff < 0) return new Date(iso).toLocaleString();
      if (diff < 60_000) return "just now";
      if (diff < 3_600_000) return Math.floor(diff / 60_000) + "m ago";
      if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + "h ago";
      return Math.floor(diff / 86_400_000) + "d ago";
    } catch (_) { return "—"; }
  }
  function fmtUptime(seconds) {
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    if (seconds < 86400) {
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      return h + "h " + m + "m";
    }
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    return d + "d " + h + "h";
  }
  function setActionStatus(text, kind) {
    const el = $("action-status");
    el.textContent = text || "";
    el.className = "action-status muted small" + (kind ? " " + kind : "");
    if (text && kind === "success") {
      setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
    }
  }

  // ── API client ─────────────────────────────────────────────────────
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { const j = await r.json(); if (j.detail) msg += ": " + j.detail; } catch (_) {}
      throw new Error(msg);
    }
    return r.json();
  }

  // ── Status header ──────────────────────────────────────────────────
  async function refreshStatus() {
    let s;
    try { s = await api("/api/dashboard/status"); }
    catch (e) {
      $("status-subtitle").textContent = "Unable to reach master: " + e.message;
      const badge = $("sync-badge");
      badge.textContent = "OFFLINE";
      badge.className = "status-badge err";
      return;
    }

    $("status-subtitle").textContent = (s.owner_email || "Not signed in")
      + " · " + (s.device_name || "this device");

    const badge = $("sync-badge");
    if (s.sync_paused) {
      badge.textContent = "PAUSED";
      badge.className = "status-badge paused";
      $("btn-pause").hidden = true;
      $("btn-resume").hidden = false;
    } else {
      const hasTunnel = !!s.tunnel_url;
      badge.textContent = hasTunnel ? "ONLINE" : "LAN ONLY";
      badge.className = "status-badge " + (hasTunnel ? "ok" : "warn");
      $("btn-pause").hidden = false;
      $("btn-resume").hidden = true;
    }

    const grid = $("status-grid");
    grid.innerHTML = "";
    const items = [
      ["Sync folder", s.sync_root || "—", true],
      ["Tunnel URL", s.tunnel_url || "Not connected", true],
      ["Files", (s.stats.total_files ?? 0).toLocaleString() + " (" + fmtBytes(s.stats.total_size_bytes) + ")"],
      ["Pending upload", (s.stats.pending_upload ?? 0).toLocaleString()],
      ["Disk free", s.disk ? fmtBytes(s.disk.free_bytes) : "—"],
      ["Uptime", fmtUptime(s.uptime_seconds || 0)],
      ["App version", s.app_version || "—"],
    ];
    for (const [label, value, mono] of items) {
      grid.appendChild(
        el("div", { class: "status-item" },
          el("span", { class: "label" }, label),
          el("span", { class: "value" + (mono ? " mono" : "") }, String(value)))
      );
    }

    $("server-time").textContent = "Last update: " + new Date().toLocaleTimeString();
  }

  // ── Uploads in progress ────────────────────────────────────────────
  async function refreshUploads() {
    let resp;
    try { resp = await api("/api/dashboard/uploads"); }
    catch (_) { return; }

    const items = resp.items || [];
    $("uploads-count").textContent = items.length;
    const list = $("uploads-list");
    const empty = $("uploads-empty");

    if (items.length === 0) {
      list.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    // Preserve expansion state across refreshes
    const expanded = new Set(
      Array.from(list.querySelectorAll(".upload-row.expanded"))
        .map(n => n.dataset.uploadId)
    );

    list.innerHTML = "";
    for (const u of items) {
      const row = el("div", {
        class: "upload-row" + (expanded.has(u.upload_id) ? " expanded" : ""),
        "data-upload-id": u.upload_id,
      });

      const header = el("div", { class: "upload-row-header" },
        el("div", { class: "upload-row-name" }, u.original_name || u.relative_path || u.upload_id),
        el("div", { class: "upload-row-pct" },
          u.received_count + " / " + u.total_chunks + " · " + u.progress_pct + "%"
        )
      );
      header.addEventListener("click", () => row.classList.toggle("expanded"));
      row.appendChild(header);

      const bar = el("div", { class: "upload-progress-bar" },
        el("div", { class: "fill", style: "width: " + u.progress_pct + "%" })
      );
      row.appendChild(bar);

      row.appendChild(el("div", { class: "upload-row-meta" },
        el("span", {}, "Size: " + fmtBytes(u.total_size)),
        el("span", {}, "Chunk: " + fmtBytes(u.chunk_size)),
        el("span", {}, "SHA-256: " + (u.total_sha256 || "").slice(0, 16) + "…"),
        el("span", {}, "Last chunk: " + fmtRelativeTime(u.last_chunk_at)),
      ));

      // Detail panel: chunk grid
      const detail = el("div", { class: "upload-row-detail" });
      const grid = el("div", { class: "chunks-grid" });
      for (let i = 0; i < (u.chunks_state || []).length; i++) {
        grid.appendChild(el("div", {
          class: "chunk-cell" + (u.chunks_state[i] ? " received" : ""),
          "data-idx": String(i),
          title: "Chunk " + i + (u.chunks_state[i] ? " (received)" : " (pending)"),
        }));
      }
      detail.appendChild(grid);
      detail.appendChild(el("div", { class: "muted small", style: "margin-top: 8px;" },
        "Upload ID: " + u.upload_id + " · expires " + fmtRelativeTime(u.expires_at)
      ));
      row.appendChild(detail);

      list.appendChild(row);
    }
  }

  // ── Recent activity ────────────────────────────────────────────────
  async function refreshRecent() {
    let resp;
    try { resp = await api("/api/dashboard/recent"); }
    catch (_) { return; }

    const items = resp.items || [];
    const list = $("recent-list");
    const empty = $("recent-empty");

    if (items.length === 0) {
      list.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    list.innerHTML = "";
    for (const r of items.slice(0, 20)) {
      list.appendChild(el("div", { class: "recent-row" },
        el("div", { class: "recent-name" }, r.relative_path),
        el("div", { class: "recent-time" }, fmtBytes(r.size) + " · " + fmtRelativeTime(r.last_synced_at))
      ));
    }
  }

  // ── Files table ────────────────────────────────────────────────────
  async function refreshFiles() {
    if (isPaginating) return;
    const params = new URLSearchParams({
      limit: filesState.limit,
      offset: filesState.offset,
    });
    if (filesState.q) params.set("q", filesState.q);
    if (filesState.status) params.set("status", filesState.status);

    let resp;
    try { resp = await api("/api/dashboard/files?" + params.toString()); }
    catch (e) {
      $("files-tbody").innerHTML = '<tr><td colspan="4" class="muted">Failed: ' + e.message + '</td></tr>';
      return;
    }

    filesState.total = resp.total;
    const tbody = $("files-tbody");
    tbody.innerHTML = "";

    if (resp.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">No files match.</td></tr>';
    } else {
      for (const f of resp.items) {
        const tr = el("tr", {},
          el("td", {}, f.relative_path),
          el("td", { class: "num" }, fmtBytes(f.size)),
          el("td", {}, el("span", { class: "status-tag " + f.sync_status }, f.sync_status.replace("_", " "))),
          el("td", { class: "muted" }, fmtRelativeTime(f.last_synced_at) || "—")
        );
        tbody.appendChild(tr);
      }
    }

    // Pagination
    const start = filesState.offset + 1;
    const end = Math.min(filesState.offset + resp.items.length, resp.total);
    $("files-pageinfo").textContent =
      resp.total === 0 ? "No results"
      : start + "–" + end + " of " + resp.total.toLocaleString();
    $("files-prev").disabled = filesState.offset === 0;
    $("files-next").disabled = filesState.offset + filesState.limit >= filesState.total;
  }

  // ── Action handlers ────────────────────────────────────────────────
  async function callAction(path, body) {
    setActionStatus("Working…");
    try {
      const r = await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      return r;
    } catch (e) {
      setActionStatus(e.message, "error");
      throw e;
    }
  }

  $("btn-scan").addEventListener("click", async () => {
    try {
      await callAction("/api/dashboard/scan");
      setActionStatus("Scan triggered", "success");
      setTimeout(refreshStatus, 1500);
      setTimeout(refreshFiles, 2000);
    } catch (_) { /* status already set */ }
  });

  $("btn-pause").addEventListener("click", async () => {
    try {
      await callAction("/api/dashboard/pause");
      setActionStatus("Sync paused", "success");
      refreshStatus();
    } catch (_) { /* status already set */ }
  });

  $("btn-resume").addEventListener("click", async () => {
    try {
      await callAction("/api/dashboard/resume");
      setActionStatus("Sync resumed", "success");
      refreshStatus();
    } catch (_) { /* status already set */ }
  });

  $("btn-switch").addEventListener("click", () => {
    $("switch-wipe-db").checked = false;
    $("switch-dialog").showModal();
  });

  $("switch-confirm").addEventListener("click", async (ev) => {
    // The dialog form has method=dialog; we run our action here BEFORE
    // it auto-closes. preventDefault keeps the dialog open until the
    // request finishes.
    ev.preventDefault();
    const wipe = $("switch-wipe-db").checked;
    try {
      await callAction("/api/dashboard/switch-account", { wipe_db: wipe });
      $("switch-dialog").close();
      setActionStatus("Account binding cleared. Restart RGBC Drive to sign in again.", "success");
    } catch (_) {
      // dialog stays open so user can read error
    }
  });

  // ── Files table search/filter/paginate ─────────────────────────────
  let searchDebounce;
  $("files-q").addEventListener("input", (e) => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      filesState.q = e.target.value.trim();
      filesState.offset = 0;
      refreshFiles();
    }, 250);
  });
  $("files-status").addEventListener("change", (e) => {
    filesState.status = e.target.value;
    filesState.offset = 0;
    refreshFiles();
  });
  $("files-prev").addEventListener("click", () => {
    isPaginating = true;
    filesState.offset = Math.max(0, filesState.offset - filesState.limit);
    refreshFiles().finally(() => { isPaginating = false; });
  });
  $("files-next").addEventListener("click", () => {
    isPaginating = true;
    filesState.offset += filesState.limit;
    refreshFiles().finally(() => { isPaginating = false; });
  });

  // ── Collapsible cards ──────────────────────────────────────────────
  document.querySelectorAll(".card-header.collapsible").forEach(h => {
    h.addEventListener("click", () => {
      const target = $(h.dataset.target);
      if (!target) return;
      target.classList.toggle("collapsed");
      h.classList.toggle("collapsed");
    });
  });

  // ── Polling loop ───────────────────────────────────────────────────
  async function tick() {
    await Promise.all([
      refreshStatus(),
      refreshUploads(),
      refreshRecent(),
      refreshFiles(),
    ]);
  }
  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    tick();
    pollTimer = setInterval(tick, REFRESH_MS);
  }
  document.addEventListener("visibilitychange", () => {
    // Pause polling when tab is hidden to avoid burning CPU
    if (document.hidden) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
    } else {
      startPolling();
    }
  });

  startPolling();
})();