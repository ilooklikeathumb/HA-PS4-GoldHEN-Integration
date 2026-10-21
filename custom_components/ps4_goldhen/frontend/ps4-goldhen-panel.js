const html = (strings, ...values) =>
  strings.reduce((acc, str, i) => acc + str + (values[i] ?? ""), "");

class PS4GoldHENPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._init();
    }
  }

  set panel(panel) {
    this._panel = panel;
  }

  // HA sets narrow on phones, where its sidebar is hidden: the panel shows a menu button then
  set narrow(narrow) {
    const changed = this._narrow !== narrow;
    this._narrow = narrow;
    if (changed && this._initialized) this._render();
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._initialized = false;
    this._loading = false;
    this._tab = "ftp";

    this._entries = [];
    this._selectedEntryId = null;

    // FTP state
    this._path = "/";
    this._ftpEntries = [];
    this._ftpError = "";
    this._editing = null;

    // BinLoader state
    this._payloads = [];
    this._payloadDir = "";
    this._binTimeout = "30";
    this._binStatus = "";
    this._plConfirm = null;  // the send dialog, {payload, target: {host, timeout}}
    this._payloadFilter = "";
    this._payloadAutoRefreshMs = 5000;
    this._payloadRefreshTimer = null;

    // Klog state
    this._klogPort = "3232";
    this._klogStatus = "";
    this._klogLines = [];
    this._klogMaxLines = 800;
    this._klogUnsub = null; // unsubscribe fn returned by subscribeMessage
    this._klogConnecting = false;
    this._klogConnectGeneration = 0;
    this._klogManuallyDisconnected = false;
    this._klogRenderQueued = false;

    // Icons for payloads
    this._payloadIconBaseUrl = "/api/ps4_goldhen/frontend/payload_icons";

    // Screenshots state
    this._ssPage = 24;
    this._ssReset();

    // Play time state
    this._ptTimer = null;
    this._ptReset();

    // Saves state
    this._svReset();
  }

  connectedCallback() {
    this._onKey ||= (ev) => {
      if (ev.key === "Escape" && this._closeOverlay()) ev.stopPropagation();
    };
    window.addEventListener("keydown", this._onKey);
  }

  disconnectedCallback() {
    if (this._onKey) window.removeEventListener("keydown", this._onKey);
    this._klogDisconnect(false);
    this._stopPayloadAutoRefresh();
    this._ssStopPoll();
    this._ptStopPoll();
    this._svStopPoll();
  }

  // the console's name in HA (the user's rename first), else the entry title
  _entryName(entry) {
    return entry?.name || entry?.title || entry?.ps4_host || "";
  }

  // Escape or a click on the dark backdrop closes the topmost viewer / dialog. The FTP editor is
  // left alone: it may hold unsaved text.
  _closeOverlay() {
    if (this._plConfirm) this._plConfirm = null;
    else if (this._svConfirm) this._svConfirm = null;
    else if (this._ssDelete) this._ssDelete = null;
    else if (this._ssDialog) this._ssDialog = null;
    else if (this._ssView) this._ssView = null;
    else return false;
    this._render();
    return true;
  }

  // one date format for Saves + Screenshots, "Oct 16, 11:57 AM", the year only when it isn't this year
  _when(date) {
    if (!(date instanceof Date) || isNaN(date)) return "";
    const opts = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
    if (date.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
    return date.toLocaleString(undefined, opts);
  }

  // a game without a name in HA (not in the console's app list) shows as "Unknown game" + its ID
  _gameLabel(name, titleId) {
    const tid = `<div class="muted game-tid">${this._escapeHtml(titleId)}</div>`;
    return name && name !== titleId ? `${this._escapeHtml(name)}${tid}` : `Unknown game${tid}`;
  }

  async _init() {
    await this._loadEntries();
    await this._loadPayloads();
    if (this._selectedEntryId) await this._loadDir("/");
    this._render();
  }

  _setLoading(v) {
    this._loading = v;
    this._render();
  }

  // Switch console. The BinLoader target follows the console.
  _selectEntry(entryId) {
    // console first: _klogDisconnect renders, and on the Payloads tab the switch then waits on an FTP listing;
    // a payload click in between already goes to the new console.
    this._selectedEntryId = entryId;
    this._plConfirm = null;

    this._klogDisconnect(false);
    this._klogManuallyDisconnected = false;

    this._klogLines = [];
    this._editing = null;
    this._path = "/";
    this._ftpEntries = [];
    this._ftpError = "";
    this._ssStopPoll();
    this._ssReset();
    this._ptReset();
    this._svStopPoll();
    this._svReset();
  }

  async _loadEntries() {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "ps4_goldhen/list_entries" });
      this._entries = resp.entries || [];
      const configured = this._panel?.config?.entry_id;
      const first = this._entries[0]?.entry_id || null;
      this._selectedEntryId = configured || this._selectedEntryId || first;
    } catch (e) {
      this._entries = [];
      this._selectedEntryId = null;
      alert(`Couldn't load the PS4 list: ${e.message || e}`);
    }
  }

  _selectedEntry() {
    return this._entries.find((e) => e.entry_id === this._selectedEntryId) || null;
  }

  // Signed paths are ideal for GET downloads; uploads should use authenticated fetch.
  async _signedPath(path) {
    const resp = await this._hass.callWS({ type: "auth/sign_path", path });
    return resp.path;
  }

  async _signedFetch(path, options = {}) {
    const signed = await this._signedPath(path);
    return fetch(signed, { credentials: "same-origin", ...options });
  }

  async _authFetch(path, options = {}) {
    if (this._hass && typeof this._hass.fetchWithAuth === "function") {
      return this._hass.fetchWithAuth(path, options);
    }
    return fetch(path, { credentials: "same-origin", ...options });
  }

  async _loadPayloads() {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "ps4_goldhen/list_payloads" });
      const next = resp.payloads || [];
      this._payloadDir = resp.payload_dir || "";

      // Only re-render if list actually changes (keeps UI smoother on auto-refresh)
      const a = JSON.stringify(this._payloads);
      const b = JSON.stringify(next);
      this._payloads = next;

      if (a !== b && this._tab === "binloader") {
        this._render();
      }
    } catch (e) {
      this._payloads = [];
      this._payloadDir = "";
      this._binStatus = `Failed to list payloads: ${e.message || e}`;
    }
  }

  _startPayloadAutoRefresh() {
    if (this._payloadRefreshTimer) return;
    this._payloadRefreshTimer = window.setInterval(() => {
      if (this._tab !== "binloader") return;
      if (this._loading) return;
      this._loadPayloads();
    }, this._payloadAutoRefreshMs);
  }

  _stopPayloadAutoRefresh() {
    if (this._payloadRefreshTimer) {
      clearInterval(this._payloadRefreshTimer);
      this._payloadRefreshTimer = null;
    }
  }

  // FTP
  async _loadDir(path = this._path) {
    if (!this._hass || !this._selectedEntryId) return;
    this._setLoading(true);
    try {
      const result = await this._hass.callWS({
        type: "ps4_goldhen/ftp_list_dir",
        entry_id: this._selectedEntryId,
        path: path,
      });
      this._path = result.path;
      this._ftpEntries = result.entries;
      this._ftpError = "";
    } catch (e) {
      // shown in the tab, not a browser alert (a console in rest mode would pop one on every open)
      this._ftpError = `Couldn't list the PS4's files: ${e.message || e}`;
    } finally {
      this._setLoading(false);
    }
  }

  async _deleteEntry(entry) {
    if (!confirm(`Delete ${entry.name}?`)) return;
    try {
      await this._hass.callWS({
        type: "ps4_goldhen/ftp_delete",
        entry_id: this._selectedEntryId,
        path: entry.path,
        is_dir: entry.is_dir,
      });
      this._loadDir();
    } catch (e) {
      alert(`Delete failed: ${e.message || e}`);
    }
  }

  async _renameEntry(entry) {
    const newName = prompt(`Rename ${entry.name} to:`, entry.name);
    if (!newName || newName === entry.name) return;

    const parts = this._path.split("/").filter((p) => p);
    const parentPath = "/" + parts.join("/");
    const toPath = (parentPath === "/" ? "" : parentPath) + "/" + newName;

    try {
      await this._hass.callWS({
        type: "ps4_goldhen/ftp_rename",
        entry_id: this._selectedEntryId,
        from_path: entry.path,
        to_path: toPath,
      });
      this._loadDir();
    } catch (e) {
      alert(`Rename failed: ${e.message || e}`);
    }
  }

  async _editEntry(entry) {
    this._setLoading(true);
    try {
      const result = await this._hass.callWS({
        type: "ps4_goldhen/ftp_get_text",
        entry_id: this._selectedEntryId,
        path: entry.path,
      });
      this._editing = { path: entry.path, name: entry.name, content: result.content };
    } catch (e) {
      alert(`Read failed: ${e.message || e}`);
    } finally {
      this._setLoading(false);
    }
  }

  async _saveFile() {
    const textarea = this.shadowRoot.querySelector("#editor-textarea");
    const content = textarea ? textarea.value : "";
    this._setLoading(true);

    try {
      await this._hass.callWS({
        type: "ps4_goldhen/ftp_put_text",
        entry_id: this._selectedEntryId,
        path: this._editing.path,
        content: content,
      });
      this._editing = null;
      await this._loadDir();
    } catch (e) {
      alert(`Save failed: ${e.message || e}`);
    } finally {
      this._setLoading(false);
    }
  }

  async _downloadEntry(entry) {
    this._setLoading(true);
    try {
      const rel = `/api/ps4_goldhen/ftp/download?entry_id=${this._selectedEntryId}&path=${encodeURIComponent(
        entry.path
      )}`;

      const response = await this._signedFetch(rel, { method: "GET" });
      if (!response.ok) throw new Error(await response.text());

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = entry.name;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      a.remove();
    } catch (e) {
      alert(`Download failed: ${e.message || e}`);
    } finally {
      this._setLoading(false);
    }
  }

  async _uploadFileToFtp() {
    const fileInput = this.shadowRoot.querySelector("#ftp-upload-input");
    if (!fileInput || !fileInput.files.length) return;

    const file = fileInput.files[0];
    this._setLoading(true);

    const formData = new FormData();
    formData.append("entry_id", this._selectedEntryId);
    formData.append("path", this._path);
    formData.append("file", file);

    try {
      const response = await this._authFetch("/api/ps4_goldhen/ftp/upload", {
        method: "POST",
        body: formData,
      });
      if (!response.ok) throw new Error(await response.text());
      await this._loadDir();
    } catch (e) {
      alert(`Upload failed: ${e.message || e}`);
    } finally {
      this._setLoading(false);
    }
  }

  // --- Payload icons ---
  _normalizePayloadKey(name) {
    const base = String(name || "").replace(/\.[^/.]+$/, ""); // strip extension
    return base
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "");
  }

  _payloadIconUrl(payloadName) {
    const key = this._normalizePayloadKey(payloadName);

    // Map normalized payload key -> icon file in payload_icons/
    const map = {
      "disable-aslr": "disable-aslr.png",
      "exit-idu": "exit-idu.png",
      "backup": "backup.png",
      "disable-updates": "disable-updates.png",
      "enable-browser": "enable-browser.png",
      "enable-updates": "enable-updates.png",
      "ftp": "ftp.png",
      "history-blocker": "history-blocker.png",
      "kernel-dumper": "kernel-dumper.png",
      "kernel-clock": "kernel-clock.png",
      "linux-1gb": "Linux-1gb.png",
      "linux-2gb": "Linux-2gb.png",
      "linux-3gb": "Linux-3gb.png",
      "linux-4gb": "Linux-4gb.png",
      "module-dumper": "module-dumper.png",
      "ps4-debug-v1-1-16": "ps4-debug_v1.1.16.png",
      "permanent-uart": "permanent-uart.png",
      "rif-renamer": "rif-renamer.png",
      "todex": "todex.png",
      "app-dumper": "app-dumper.png",
      "app2usb": "app2usb.png",
      "ps4-sflash0-dumper": "ps4-sflash0-dumper.png",
      "pup-decrypt": "pup-decrypt.png",
      "restore": "restore.png",
      "webrte": "WebRTE.png",
    };

    const iconFile = map[key];

    // Fallback to GoldHEN logo if we don't have an icon for the payload
    if (!iconFile) return "/api/ps4_goldhen/frontend/goldhen_logo.png";

    // Add a tiny cache-bust so icon updates show quickly if you replace files
    return `${this._payloadIconBaseUrl}/${iconFile}?v=1`;
  }

  // BinLoader
  // a click asks first. No payload's firmware support is known yet (bundled_payloads/README.md), and
  // one built for other firmware can freeze the console. The send_payload service itself doesn't ask.
  _askSendPayload(payload) {
    const entry = this._selectedEntry();
    if (!entry) {
      alert("Select a PS4 first.");
      return;
    }
    if (!(payload || "").trim()) {
      alert("Select a payload first.");
      return;
    }
    this._plConfirm = { payload: payload.trim(), target: this._payloadTarget(entry) };
    this._render();
  }

  _renderPlConfirm() {
    const c = this._plConfirm;
    const esc = (v) => this._escapeHtml(v);
    const target = this._entries.find((e) => e.ps4_host === c.target.host);
    const fw = target?.firmware;
    return html`
      <div class="editor-overlay ss-dialog sv-dlg" id="pl-confirm" data-dismiss>
        <div class="editor-container">
          <h3 style="margin-top:0">Send a payload</h3>
          <div><b>${esc(c.payload)}</b> → <b>${esc(target ? this._entryName(target) : c.target.host)}</b>
            (${fw ? `firmware ${esc(fw)}` : "firmware unknown"})</div>
          <ul>
            <li>PS4 GoldHEN doesn't know which firmware this payload supports. Many payloads only work on the firmware they were built for.</li>
            <li>One built for other firmware can freeze the console. Then it needs a forced power-off and loading GoldHEN again.</li>
            <li>Only send it if you know it supports ${fw ? `firmware ${esc(fw)}` : "this console's firmware"}.</li>
          </ul>
          <div class="editor-actions">
            <button id="pl-confirm-cancel">Cancel</button>
            <button class="btn-danger" id="pl-confirm-go">Send anyway</button>
          </div>
        </div>
      </div>`;
  }

  // The selected console (HA sends to its BinLoader port); the timeout box, else the last value typed
  _payloadTarget(entry) {
    const timeout = parseFloat(
      (this.shadowRoot.querySelector("#bin-timeout")?.value || "").trim() || this._binTimeout || "30"
    );

    // Persist what user typed (so it doesn't reset on re-render)
    this._binTimeout = String(timeout || "");
    return { host: entry.ps4_host, timeout };
  }

  // `target` = what the dialog showed, so the send goes where the user confirmed
  async _sendPayload(payloadOverride = null, target = null) {
    const entry = this._selectedEntry();
    if (!entry) {
      alert("Select a PS4 first.");
      return;
    }

    const payload = (payloadOverride || "").trim();
    if (!payload) {
      alert("Select a payload first.");
      return;
    }

    const { host, timeout } = target || this._payloadTarget(entry);

    this._setLoading(true);
    this._binStatus = `Sending ${payload}...`;
    this._render();

    try {
      await this._hass.callService("ps4_goldhen", "send_payload", {
        payload_file: payload,
        ps4_host: host,
        timeout: timeout,
      });
      this._binStatus = `Payload sent: ${payload}`;
    } catch (e) {
      this._binStatus = `Send failed: ${e.message || e}`;
      alert(this._binStatus);
    } finally {
      this._setLoading(false);
    }
  }

  // --- Klog helpers ---
  _extractKlogLine(m) {
    const a = m?.event?.line;
    if (typeof a === "string") return a;

    const b = m?.line;
    if (typeof b === "string") return b;

    const c = m?.event?.message;
    if (typeof c === "string") return c;

    return null;
  }

  _scheduleKlogRender() {
    if (this._klogRenderQueued) return;
    this._klogRenderQueued = true;

    requestAnimationFrame(() => {
      this._klogRenderQueued = false;
      const box = this.shadowRoot?.querySelector?.("#klog-box");
      if (!box) return;

      box.textContent = this._klogLines.join("\n");
      box.scrollTop = box.scrollHeight;
    });
  }

  // Klog: subscribe to the single backend-owned PS4 TCP listener.
  async _klogConnect() {
    if (!this.isConnected || this._tab !== "klog") return;
    if (!this._hass || !this._selectedEntryId) {
      alert("Select a PS4 first.");
      return;
    }
    if (this._klogUnsub || this._klogConnecting) return;
    if (!this._hass.connection || typeof this._hass.connection.subscribeMessage !== "function") {
      this._klogManuallyDisconnected = true;
      this._klogStatus = "This HA frontend can't stream klog lines.";
      this._render();
      return;
    }

    const entryId = this._selectedEntryId;
    const generation = ++this._klogConnectGeneration;
    this._klogConnecting = true;
    this._klogStatus = "Connecting...";
    this._render();

    try {
      const unsub = await this._hass.connection.subscribeMessage(
        (m) => {
          if (generation !== this._klogConnectGeneration || entryId !== this._selectedEntryId) return;
          const line = this._extractKlogLine(m);
          if (typeof line !== "string" || !line.length) return;
          this._klogLines.push(line);
          if (this._klogLines.length > this._klogMaxLines) {
            this._klogLines.splice(0, this._klogLines.length - this._klogMaxLines);
          }
          this._scheduleKlogRender();
        },
        { type: "ps4_goldhen/klog_subscribe", entry_id: entryId }
      );
      if (generation !== this._klogConnectGeneration || entryId !== this._selectedEntryId) {
        unsub();
        return;
      }
      this._klogUnsub = unsub;
      this._klogManuallyDisconnected = false;
      this._klogStatus = "Connected. HA holds the one klog connection to the PS4; this tab reads it.";
    } catch (e) {
      if (generation === this._klogConnectGeneration) {
        this._klogManuallyDisconnected = true;
        this._klogStatus = `Subscribe failed: ${e.message || e}`;
        this._klogUnsub = null;
      }
    } finally {
      if (generation === this._klogConnectGeneration) {
        this._klogConnecting = false;
        this._render();
      }
    }
  }

  _klogDisconnect(markManual = true) {
    ++this._klogConnectGeneration;
    this._klogConnecting = false;
    if (this._klogUnsub) {
      try { this._klogUnsub(); } catch (_) {}
    }
    this._klogUnsub = null;
    if (markManual) this._klogManuallyDisconnected = true;
    this._klogStatus = "Disconnected.";
    this._render();
  }

  _klogClear() {
    this._klogLines = [];
    const box = this.shadowRoot.querySelector("#klog-box");
    if (box) box.textContent = "";
  }

  // ── Play time: read from HA's own store, nothing is asked of the PS4 ─────────────

  _ptReset() {
    this._pt = null;
    this._ptError = "";
    this._ptStopPoll();
  }

  _ptStopPoll() {
    if (this._ptTimer) clearInterval(this._ptTimer);
    this._ptTimer = null;
  }

  async _ptLoad() {
    if (!this._hass || !this._selectedEntryId) return;
    const entryId = this._selectedEntryId;
    try {
      const data = await this._hass.callWS({ type: "ps4_goldhen/play_time", entry_id: entryId });
      if (entryId !== this._selectedEntryId) return;
      this._pt = data;
      this._ptError = "";
    } catch (err) {
      this._ptError = err?.message || String(err);
    }
    if (this._tab !== "playtime") return;
    this._render();
    // while the tab is open: once a minute, the same as HA counts (HA only, cheap)
    if (!this._ptTimer) this._ptTimer = setInterval(() => this._ptLoad(), 60000);
  }

  _ptDuration(seconds) {
    // rolls into hours and days, written like HA writes the sensors ("3h 7m")
    const min = Math.floor((seconds || 0) / 60);
    if (min < 1) return seconds > 0 ? "< 1m" : "0m";
    const d = Math.floor(min / 1440);
    const h = Math.floor((min % 1440) / 60);
    const m = min % 60;
    if (d) return h ? `${d}d ${h}h` : `${d}d`;
    if (h) return m ? `${h}h ${m}m` : `${h}h`;
    return `${m}m`;
  }

  _ptWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  _ptGame(item) {
    const name = item.name ? this._escapeHtml(item.name) : `<span class="pt-tid">${this._escapeHtml(item.title_id)}</span>`;
    const tid = item.name ? `<div class="muted pt-tid">${this._escapeHtml(item.title_id)}</div>` : "";
    return `${name}${tid}`;
  }

  _renderPlayTime() {
    if (this._ptError) {
      return `<div class="card"><div class="ss-warn">Couldn't load play time: ${this._escapeHtml(this._ptError)}</div>
        <div class="row"><button class="btn" id="btn-pt-refresh">Retry</button></div></div>`;
    }
    const pt = this._pt;
    if (!pt) return `<div class="loading">Loading play time...</div>`;
    const stat = (label, secs) => `<div><div class="muted">${label}</div><div class="pt-num">${this._ptDuration(secs)}</div></div>`;
    const since = pt.since ? ` Since ${this._escapeHtml(this._ptWhen(pt.since))}.` : "";
    const totals = `
      <div class="card">
        <div class="pt-totals">
          ${stat("Today", pt.today)}${stat("Last 7 days", pt.week)}${stat("Last 30 days", pt.month)}${stat("All time", pt.total)}
        </div>
        <div class="row">
          <div class="muted" style="flex:1">Counted while a game is on screen. The home screen, rest mode and off don't count;
            a session continues if the same game is back within 30 min.${since}</div>
          <button class="btn" id="btn-pt-refresh">Refresh</button>
        </div>
      </div>`;
    if (!pt.games.length) {
      return `${totals}<div class="card muted">No play time yet. It's counted from now on, whenever a game is on screen.</div>`;
    }
    const games = pt.games.map((g) => `
      <tr><td>${this._ptGame(g)}</td><td>${this._ptDuration(g.seconds)}</td><td>${this._ptDuration(g.week)}</td>
        <td>${g.sessions}</td><td>${this._escapeHtml(this._ptWhen(g.last))}</td></tr>`).join("");
    const sessions = pt.sessions.map((x) => {
      const state = x.playing ? `<span class="pill pt-playing">Playing</span>` : x.open ? `<span class="pill">Paused</span>` : "";
      return `<tr><td>${this._ptGame(x)}</td><td>${this._escapeHtml(this._ptWhen(x.start))}</td>
        <td>${this._ptDuration(x.seconds)}</td><td>${state}</td></tr>`;
    }).join("");
    return `${totals}
      <div class="card pt-card"><h3>Games</h3>
        <table><tr><th>Game</th><th>Total</th><th>Last 7 days</th><th>Sessions</th><th>Last played</th></tr>${games}</table>
      </div>
      <div class="card pt-card"><h3>Sessions</h3><div class="muted">Newest first, the last ${pt.sessions.length}.</div>
        <table><tr><th>Game</th><th>Started</th><th>Played</th><th></th></tr>${sessions}</table>
      </div>`;
  }

  _bindPlayTimeEvents() {
    const refresh = this.shadowRoot.querySelector("#btn-pt-refresh");
    if (refresh) refresh.onclick = () => this._ptLoad();
  }

  // ── Saves ─────────────────────────────────────────────────
  // Back up walks the console in the background and a 1 s status poll follows it. Restore: Send to PS4 puts a copy in
  // Apollo's Fake USB folder on a console; Download is the same files as a zip for a real USB stick (signed path).
  // two panes. On PS4 = a console read (ps4_goldhen/saves/console: the walk a backup does, no save
  // downloads, kept 5 min by HA) with each save against HA's newest copy. In HA = HA's copies (ps4_goldhen/saves/list,
  // no PS4 call). Each pane has its own Select: On PS4 → Back up N, In HA → Send / Download / Delete N. A tick is one
  // save ("tid|slot") or one older version ("tid|slot|stamp"), one user and one version per save at a time; a game's
  // box ticks its saves. Phones show one pane at a time.

  _svReset() {
    this._svSettings = null;  // ps4_goldhen/saves/get
    this._svList = null;      // ps4_goldhen/saves/list (In HA)
    this._svPs4 = null;       // ps4_goldhen/saves/console (On PS4)
    this._svReading = false;  // a console read is running
    this._svPs4Error = "";
    this._svStatus = "";
    this._svStatusPane = "ha";  // the pane whose bar shows _svStatus
    this._svBusy = false;     // a zip / send / delete call is running
    this._svTarget = null;    // entry_id that Send to PS4 goes to (null: this console)
    this._svOpen = new Set(); // "pane|uid|title_id" of the expanded games
    this._svConfirm = null;   // Back up / Send / Download / Delete dialog: {kind, params, pane, target, alsoVersions}
    this._svPane = "ps4";     // phones: the pane on screen
    this._svSelecting = null; // "ps4" | "ha": the pane in Select mode
    this._svSel = { uid: null, keys: new Set() };
    this._svHint = "";        // why a tick moved (one version per save)
    this._svPollTimer = null;
  }

  _svStopPoll() {
    if (this._svPollTimer) clearInterval(this._svPollTimer);
    this._svPollTimer = null;
  }

  _svCall(type, extra = {}) {
    return this._hass.callWS({ type: `ps4_goldhen/saves/${type}`, entry_id: this._selectedEntryId, ...extra });
  }

  _svWhen(iso) {
    return iso ? this._when(new Date(iso)) : "";
  }

  _svAgo(iso) {
    const min = Math.round((Date.now() - new Date(iso)) / 60000);
    return min < 1 ? "just now" : min < 60 ? `${min} min ago` : this._svWhen(iso);
  }

  // A version's folder name is HA's local time: "YYYY-MM-DD HHMMSS"
  _svStamp(stamp) {
    const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2})(\d{2})(\d{2})/.exec(stamp || "");
    if (!m) return stamp || "";
    return this._when(new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  }

  async _svLoad() {
    if (!this._hass || !this._selectedEntryId) return;
    const entryId = this._selectedEntryId;
    try {
      const settings = await this._svCall("get");
      if (entryId !== this._selectedEntryId) return;
      this._svSettings = settings;
      this._svList = settings.mode === "off" ? null : await this._svCall("list");
    } catch (e) {
      this._svStatus = `Couldn't read the save backup: ${e.message || e}`;
    }
    if (this._tab !== "saves" || entryId !== this._selectedEntryId) return;
    this._render();
    if (this._svSettings?.running) this._svStartPoll();
    if (this._svSettings && this._svSettings.mode !== "off") await this._svReadPs4(false);
  }

  // The PS4 pane. HA answers from its last read while it's under 5 min old, or while the PS4 sleeps or a backup /
  // send runs; otherwise it reads the console (one FTP session, listings + the save list, a few seconds).
  async _svReadPs4(refresh) {
    const entryId = this._selectedEntryId;
    this._svReading = true;
    this._svPs4Error = "";
    this._render();
    try {
      const read = await this._svCall("console", refresh ? { refresh: true } : {});
      if (entryId !== this._selectedEntryId) return;
      this._svPs4 = read;
      this._svList = await this._svCall("list");  // a read names HA's copies and finds saves gone from the PS4
    } catch (e) {
      this._svPs4Error = `Couldn't read the PS4: ${e.message || e}`;
    } finally {
      this._svReading = false;
    }
    if (this._tab === "saves" && entryId === this._selectedEntryId) this._render();
  }

  async _svReloadHa() {
    try {
      this._svList = await this._svCall("list");
    } catch (e) {
      this._svStatusPane = "ha";
      this._svStatus = `Couldn't read HA's copies: ${e.message || e}`;
    }
    this._render();
  }

  // After a send / delete / remove: both panes from HA (the PS4 read is fresh, so no FTP)
  async _svRefreshBoth() {
    try {
      this._svList = await this._svCall("list");
      this._svPs4 = await this._svCall("console");
    } catch (_) { /* the status line already says what happened */ }
  }

  async _svSave(changes) {
    try {
      this._svSettings = await this._svCall("set", changes);
      this._svList = this._svSettings.mode === "off" ? null : await this._svCall("list");
    } catch (e) {
      this._svStatus = `Couldn't save the save backup settings: ${e.message || e}`;
      this._render();
      return;
    }
    this._render();
    if (this._svSettings.running) this._svStartPoll();
    if (changes.mode && changes.mode !== "off") await this._svReadPs4(false);
  }

  // params: {} (every save on the console) or {uid, items} (On PS4 → Select → Back up N)
  async _svBackup(params = {}) {
    this._svStatusPane = "ps4";
    try {
      await this._svCall("backup", params);
    } catch (e) {
      this._svStatus = e.message || String(e);
      this._render();
      return;
    }
    this._svStatus = "";
    this._svEndSelect();
    this._svSettings = { ...this._svSettings, running: true, progress: { done: 0, total: 0 } };
    this._render();
    this._svStartPoll();
  }

  // Once a second while a pass runs. Each time a batch of copies lands, both panes are read again (no FTP while a
  // pass runs), so a long backup shows its progress instead of sitting at "0 of 5 GB" until the end.
  _svStartPoll() {
    if (this._svPollTimer) return;
    const entryId = this._selectedEntryId;
    let busy = false;
    let lastDone = null;
    this._svPollTimer = setInterval(async () => {
      if (this._tab !== "saves" || entryId !== this._selectedEntryId) {
        this._svStopPoll();
        return;
      }
      if (busy) return;  // a slow answer: no pile-up of calls
      busy = true;
      try {
        this._svSettings = await this._svCall("get");
        if (this._svSettings.running && this._svSettings.progress?.done !== lastDone) {
          lastDone = this._svSettings.progress?.done;
          if (lastDone) await this._svRefreshBoth();
        }
      } catch (_) {
        return;
      } finally {
        busy = false;
      }
      if (this._tab !== "saves" || entryId !== this._selectedEntryId) return;
      if (this._svSettings.running) {
        this._render();
        return;
      }
      this._svStopPoll();
      this._svStatusPane = "ps4";
      this._svStatus = this._svResultText(this._svSettings.last || {});
      await this._svRefreshBoth();  // the pass read the console: the PS4 pane needs no new FTP session
      this._render();
    }, 1000);
  }

  _svResultText(last) {
    let kept = last.retried ? ` ${last.retried} changed while copying: back up again to copy them.` : "";
    if (last.open?.length) kept += ` Skipped ${last.open.join(", ")}: open on the PS4. Close it, then back up again.`;
    if (last.state === "failed") return `Stopped after ${last.copied || 0} copied: ${last.error}`;
    if (last.state === "limit_reached") {
      return `Copied ${last.copied || 0}; ${last.skipped} didn't fit under the storage limit.${kept}`;
    }
    const n = last.checked || 0;
    if (!last.copied) return `Checked ${n} save${n === 1 ? "" : "s"}: nothing new to copy.${kept}`;
    return `Done: ${last.copied} of ${n} saves were new or changed.${kept}`;
  }

  async _svKeep(uid, titleId, slot, stamp, kept) {
    this._svStatusPane = "ha";
    try {
      await this._svCall("keep", { uid, title_id: titleId, slot, stamp, kept });
      this._svList = await this._svCall("list");
    } catch (e) {
      this._svStatus = `Couldn't change Keep: ${e.message || e}`;
    }
    this._render();
  }

  // The zip is made in HA, then the browser downloads it straight to disk (no blob in memory)
  async _svDownload(params) {
    this._svBusy = true;
    this._svStatusPane = "ha";
    this._svStatus = "Making the zip...";
    this._render();
    try {
      const made = await this._svCall("zip", params);
      const a = document.createElement("a");
      a.href = await this._signedPath(made.path);
      a.download = made.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      this._svStatus = `Downloading ${made.name} (${this._ssMB(made.bytes)}). HA deletes this zip after an hour or when you make the next one.`;
      this._svEndSelect();
    } catch (e) {
      this._svStatus = `Couldn't make the zip: ${e.message || e}`;
    } finally {
      this._svBusy = false;
      this._render();
    }
  }

  // Restore, step 1: HA uploads the copies into Apollo's Fake USB folder on the chosen console (FTP, PS4 on)
  async _svSend(params, target = this._selectedEntryId) {
    const targetEntry = this._entries.find((e) => e.entry_id === target);
    this._svBusy = true;
    this._svStatusPane = "ha";
    this._svStatus = `Sending to ${this._entryName(targetEntry)}...`;
    this._render();
    try {
      const sent = await this._svCall("send", { ...params, target_entry_id: target });
      const what = sent.games.join(", ") + (sent.version ? ` (version ${this._svStamp(sent.version)})` : "");
      this._svStatus = `Sent ${what} to ${sent.target} (${sent.files} files, ${this._ssMB(sent.bytes)}). On that PS4: close the game, Apollo → USB Saves → pick it → Copy save game to HDD. Then Remove from PS4.`;
      this._svEndSelect();
      if (target === this._selectedEntryId) {
        this._svSettings = await this._svCall("get");
        this._svPs4 = await this._svCall("console");
      }
    } catch (e) {
      this._svStatus = `Couldn't send: ${e.message || e}`;
    } finally {
      this._svBusy = false;
      this._render();
    }
  }

  // In HA → Delete N from HA. HA's copies only; the PS4's saves are never touched.
  async _svDelete(params) {
    this._svBusy = true;
    this._svStatusPane = "ha";
    this._render();
    try {
      const done = await this._svCall("delete", params);
      this._svStatus = `Deleted ${done.deleted} cop${done.deleted === 1 ? "y" : "ies"} from HA.`;
      this._svEndSelect();
      this._svSettings = await this._svCall("get");
      await this._svRefreshBoth();
    } catch (e) {
      this._svStatus = `Couldn't delete: ${e.message || e}`;
    } finally {
      this._svBusy = false;
      this._render();
    }
  }

  async _svClearSent() {
    this._svBusy = true;
    this._svStatusPane = "ps4";
    this._render();
    try {
      const done = await this._svCall("clear_sent");
      this._svStatus = `Removed ${done.deleted} files from the PS4's Fake USB folder.`;
      this._svSettings = await this._svCall("get");
      await this._svRefreshBoth();
    } catch (e) {
      this._svStatus = `Couldn't remove: ${e.message || e}`;
    } finally {
      this._svBusy = false;
      this._render();
    }
  }

  // ── Saves: ticks ──

  _svKey(tid, slot, stamp) {
    return stamp ? `${tid}|${slot}|${stamp}` : `${tid}|${slot}`;
  }

  _svUser(pane, uid) {
    return ((pane === "ps4" ? this._svPs4 : this._svList)?.users || []).find((u) => u.uid === uid);
  }

  _svGame(pane, uid, tid) {
    return this._svUser(pane, uid)?.games.find((g) => g.title_id === tid);
  }

  _svSlot(pane, uid, tid, slot) {
    return this._svGame(pane, uid, tid)?.slots.find((s) => s.slot === slot);
  }

  // What a game's box ticks: its saves on the PS4 (none while it's open), or HA's newest copies
  _svGameKeys(pane, g) {
    if (!g) return [];
    if (pane === "ps4") return g.open ? [] : g.slots.map((s) => this._svKey(g.title_id, s.slot));
    return g.slots.filter((s) => s.saved).map((s) => this._svKey(g.title_id, s.slot));
  }

  _svStartSelect(pane) {
    this._svSelecting = pane;
    this._svSel = { uid: null, keys: new Set() };
    this._svHint = "";
  }

  _svEndSelect() {
    this._svSelecting = null;
    this._svSel = { uid: null, keys: new Set() };
    this._svHint = "";
  }

  // One user at a time; one version per save (Apollo's USB folder holds one copy of each), so ticking another version
  // of the same save moves the tick
  _svTick(uid, key) {
    if (this._svSel.uid !== uid) this._svSel = { uid, keys: new Set() };
    const keys = this._svSel.keys;
    this._svHint = "";
    if (keys.has(key)) {
      keys.delete(key);
      return;
    }
    const [tid, slot] = key.split("|");
    for (const k of [...keys]) {
      const [t, s] = k.split("|");
      if (t === tid && s === slot) {
        keys.delete(k);
        this._svHint = "One version per save: Apollo's USB folder holds one copy of each save, so the tick moved.";
      }
    }
    keys.add(key);
  }

  // A game's box: ticks all its saves, or unticks the game when they're all ticked
  _svTickGame(uid, tid, gameKeys) {
    if (this._svSel.uid !== uid) this._svSel = { uid, keys: new Set() };
    const keys = this._svSel.keys;
    const all = gameKeys.length > 0 && gameKeys.every((k) => keys.has(k));
    for (const k of [...keys]) if (k.split("|")[0] === tid) keys.delete(k);
    if (!all) gameKeys.forEach((k) => keys.add(k));
    this._svHint = "";
  }

  // Select all: every save of one user (the one ticked from, else the first); HA's newest copies only, and on the
  // PS4 not the open games (one version per save, like a game's box).
  _svAllKeys(pane) {
    const users = (pane === "ps4" ? this._svPs4?.users : this._svList?.users) || [];
    const user = users.find((u) => u.uid === this._svSel.uid) || users[0];
    return user ? { user, keys: user.games.flatMap((g) => this._svGameKeys(pane, g)) } : { user: null, keys: [] };
  }

  _svAllTicked(pane) {
    const { user, keys } = this._svAllKeys(pane);
    return !!user && keys.length > 0 && this._svSel.uid === user.uid && keys.every((k) => this._svSel.keys.has(k));
  }

  _svTickAll(pane) {
    const { user, keys } = this._svAllKeys(pane);
    if (!user) return;
    this._svSel = { uid: user.uid, keys: new Set(this._svAllTicked(pane) ? [] : keys) };
    this._svHint = "";
  }

  _svAllButton(pane) {
    const { user, keys } = this._svAllKeys(pane);
    const users = (pane === "ps4" ? this._svPs4?.users : this._svList?.users) || [];
    const who = users.length > 1 && user ? `: ${this._escapeHtml(user.name)}` : "";
    return `<button class="btn" data-act="pick-all" data-pane="${pane}" ${keys.length ? "" : "disabled"}>${this._svAllTicked(pane) ? "Clear all" : `Select all${who}`}</button>`;
  }

  _svItems() {
    return [...this._svSel.keys].map((k) => {
      const [title_id, slot, stamp] = k.split("|");
      return stamp ? { title_id, slot, stamp } : { title_id, slot };
    });
  }

  // ── Saves: dialogs ──

  // Send, Back up, Download and Delete ask first and say what happens (Send picks the console in its dialog);
  // the dialogs name the saves.
  _svAsk(kind, params) {
    this._svConfirm = { kind, params, pane: kind === "backup" ? "ps4" : "ha",
      target: this._svTarget || this._selectedEntryId, alsoVersions: false };
    this._render();
  }

  _svName(tid) {
    for (const list of [this._svList, this._svPs4]) {
      for (const u of list?.users || []) {
        const g = u.games.find((x) => x.title_id === tid);
        if (g && g.game && g.game !== tid) return g.game;
      }
    }
    return tid;
  }

  _svSaveName(sl, slot) {
    return sl?.name || sl?.slot || slot;
  }

  // "Game · Chapter 1 (100.0%)"; an older version adds "(copy from Oct 15, 1:12 PM)"
  _svItemLabel(pane, uid, { title_id: tid, slot, stamp }) {
    const sl = this._svSlot(pane, uid, tid, slot);
    const version = stamp ? sl?.versions?.find((v) => v.stamp === stamp) : null;
    const name = version?.name || this._svSaveName(sl, slot);
    return `${this._svName(tid)} · ${name}${stamp ? ` (copy from ${this._svStamp(stamp)})` : ""}`;
  }

  _svWhat(c) {
    const { uid, items } = c.params;
    if (!items) {
      const user = this._svUser("ha", uid);
      return { head: c.kind === "backup" ? `Every save on ${this._entryName(this._selectedEntry())}`
        : `All of ${user?.name || "this user"}'s games: their newest copies`, list: [] };
    }
    const labels = items.map((it) => this._svItemLabel(c.pane, uid, it));
    return { head: `${items.length} save${items.length === 1 ? "" : "s"}`,
      list: labels.length > 6 ? [...labels.slice(0, 5), `+${labels.length - 5} more`] : labels };
  }

  // What a zip of these copies will weigh (HA's newest copies, or older versions)
  _svZipBytes({ uid, items }) {
    const user = this._svUser("ha", uid);
    if (!user) return 0;
    if (!items) return user.games.reduce((n, g) => n + g.slots.reduce((k, sl) => k + (sl.saved ? sl.bytes : 0), 0), 0);
    return items.reduce((n, it) => {
      const sl = this._svSlot("ha", uid, it.title_id, it.slot);
      return n + ((it.stamp ? sl?.versions.find((v) => v.stamp === it.stamp)?.bytes : sl?.bytes) || 0);
    }, 0);
  }

  // Delete: the older versions of the ticked newest copies that aren't ticked themselves (the dialog's "Also delete")
  _svOlderOf({ uid, items }) {
    const picked = new Set(items.map((it) => this._svKey(it.title_id, it.slot, it.stamp)));
    return items.filter((it) => !it.stamp).flatMap((it) =>
      (this._svSlot("ha", uid, it.title_id, it.slot)?.versions || [])
        .filter((v) => !picked.has(this._svKey(it.title_id, it.slot, v.stamp)))
        .map((v) => ({ title_id: it.title_id, slot: it.slot, stamp: v.stamp, kept: v.kept })));
  }

  // what a Delete removes (the ticked items + the "older versions" box), its size, and the saves it
  // leaves with no copy in HA (`last`), some of which aren't on the PS4 either (`lost`)
  _svDeletePlan(c) {
    const { uid } = c.params;
    const extra = c.alsoVersions ? this._svOlderOf(c.params) : [];
    const all = [...(c.params.items || []), ...extra];
    const gone = new Set(all.map((it) => this._svKey(it.title_id, it.slot, it.stamp)));
    const plan = { newest: 0, older: 0, bytes: 0, last: [], lost: [] };
    const seen = new Set();
    for (const it of all) {
      const sl = this._svSlot("ha", uid, it.title_id, it.slot);
      if (it.stamp) plan.older += 1;
      else plan.newest += 1;
      plan.bytes += (it.stamp ? sl?.versions.find((v) => v.stamp === it.stamp)?.bytes : sl?.bytes) || 0;
      const key = this._svKey(it.title_id, it.slot);
      if (!sl || seen.has(key)) continue;
      seen.add(key);
      const keepsNewest = sl.saved && !gone.has(key);
      const keepsOlder = sl.versions.some((v) => !gone.has(this._svKey(it.title_id, it.slot, v.stamp)));
      if (!keepsNewest && !keepsOlder) {
        plan.last.push(key);
        if (sl.on_ps4 === false) plan.lost.push(key);
      }
    }
    return plan;
  }

  _renderSvConfirm() {
    const c = this._svConfirm;
    const s = this._svSettings;
    const esc = (v) => this._escapeHtml(v);
    const consoleName = esc(this._entryName(this._selectedEntry()));
    const what = this._svWhat(c);
    let title = "";
    let body = "";
    let go = "";
    let danger = false;   // Delete is red
    let blocked = false;  // ... and stays off until a last-copy delete is ticked as understood
    if (c.kind === "backup") {
      title = c.params.items ? "Back up to HA" : `Back up ${consoleName} now`;
      go = "Back up";
      let scope = "<li>HA checks every save on the PS4 and copies the ones that are new or changed since the last backup. Unchanged ones aren't copied again.</li>";
      if (c.params.items) {
        const copy = c.params.items.filter((it) => this._svSlot("ps4", c.params.uid, it.title_id, it.slot)?.status !== "in_ha").length;
        const already = c.params.items.length - copy;
        const inHa = already === 1 ? "1 is already in HA" : `${already} are already in HA`;
        scope = copy
          ? `<li>${copy} new or changed (at the last read) will be copied.${already ? ` ${inHa}: copied again only if changed since.` : ""}</li>`
          : `<li>${already === 1 ? "It's" : "They're"} already in HA (at the last read). HA checks again and copies only what changed since.</li>`;
      }
      body = `
        <ul>
          ${scope}
          <li>Nothing is overwritten or lost: when a save changed, HA's previous copy becomes an older version (In HA).</li>
          <li>A game that's open on the PS4 (on screen or suspended) is skipped: close it first.</li>
          <li>Storage: ${this._ssMB(s.usage.bytes)} of ${Number(s.limit_gb).toFixed(1)} GB used. Over the limit the oldest older versions go first (Keep protects one); the newest copies are never deleted.</li>
          <li>It runs in the background; progress shows on this page.</li>
        </ul>`;
    } else if (c.kind === "send") {
      const target = this._entries.length > 1
        ? `<select id="sv-target">${this._entries.map((e) =>
          `<option value="${esc(e.entry_id)}" ${e.entry_id === c.target ? "selected" : ""}>${esc(this._entryName(e))}</option>`).join("")}</select>`
        : `<b>${consoleName}</b>`;
      title = "Send to PS4";
      go = "Send";
      body = `
        <label>To ${target}</label>
        <ol>
          <li>HA copies these saves into Apollo's Fake USB folder on that PS4 (<span class="sv-tid">/data/fakeusb</span>; HA creates it if it's missing). It replaces what HA sent there before. The PS4's own saves aren't touched.</li>
          <li>On that PS4: close the game, open Apollo Save Tool (1.3.0 or later) → <b>USB Saves</b> → pick the save → <b>Copy save game to HDD</b>. If a USB stick is plugged in, Apollo reads the stick: unplug it, or set Apollo's Settings → USB Saves Source → Fake USB.</li>
          <li>Back here, <b>Remove from PS4</b> (On PS4) deletes the files HA sent.</li>
        </ol>`;
    } else if (c.kind === "delete") {
      // totals include the "older versions" box, and a delete that leaves a save with no copy in HA
      // needs its own tick (without it, two taps could wipe a console's copies)
      const d = this._svDeletePlan(c);
      const older = this._svOlderOf(c.params);
      const kept = older.filter((v) => v.kept).length;
      const counts = [d.newest ? `${d.newest} newest cop${d.newest === 1 ? "y" : "ies"}` : "",
        d.older ? `${d.older} older version${d.older === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ");
      const nLast = d.last.length;
      const nLost = d.lost.length;
      const saves = (n) => `${n} save${n === 1 ? "" : "s"}`;
      title = "Delete from HA";
      go = "Delete";
      danger = true;
      blocked = nLast > 0 && !c.ack;
      body = `
        <ul>
          <li>HA deletes ${counts} (${this._ssMB(d.bytes)}). The saves on the PS4 aren't touched.</li>
          ${nLast ? `<li><b>After this, HA has no copy of ${saves(nLast)}.</b> ${nLost === nLast
            ? `${nLost === 1 ? "It isn't" : "They aren't"} on the PS4 either: gone for good.`
            : nLost ? `${nLost} of them ${nLost === 1 ? "isn't" : "aren't"} on the PS4 either: gone for good. The others are still on the PS4, and Back up copies them again.`
              : `${nLast === 1 ? "It's" : "They're"} still on the PS4, and Back up copies ${nLast === 1 ? "it" : "them"} again.`}</li>` : ""}
          ${d.newest && !c.alsoVersions && older.length ? "<li>Their older versions stay unless you tick the box below.</li>" : ""}
          <li>This can't be undone.</li>
        </ul>
        ${older.length ? `<label><input type="checkbox" id="sv-also-versions" ${c.alsoVersions ? "checked" : ""}> Also delete their ${older.length} older version${older.length === 1 ? "" : "s"}${kept ? ` (${kept} marked Keep)` : ""}</label>` : ""}
        ${nLast ? `<label class="sv-ack"><input type="checkbox" id="sv-ack" ${c.ack ? "checked" : ""}> I understand: HA keeps no copy of these ${saves(nLast)}${nLost ? `, and ${nLost} ${nLost === 1 ? "is" : "are"} gone for good` : ""}</label>` : ""}`;
    } else {
      title = "Download";
      go = "Make the zip";
      body = `
        <ul>
          <li>HA makes a ${this._ssMB(this._svZipBytes(c.params))} zip (the console's encrypted save files, laid out like an Apollo USB stick) and your browser downloads it.</li>
          <li>HA keeps one zip per console on its disk and deletes it after an hour or when you make the next one. It won't make one that leaves HA under 1 GB free.</li>
          <li>To restore from it: unpack it to the root of an exFAT USB stick, then Apollo → USB Saves → Copy save game to HDD.</li>
        </ul>`;
    }
    return html`
      <div class="editor-overlay ss-dialog sv-dlg" id="sv-confirm" data-dismiss>
        <div class="editor-container">
          <h3 style="margin-top:0">${title}</h3>
          <div><b>${esc(what.head)}</b></div>
          ${what.list.length ? `<ul class="sv-what">${what.list.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
          ${body}
          <div class="editor-actions">
            <button id="sv-confirm-cancel">Cancel</button>
            <button class="${danger ? "btn-danger" : "btn-save"}" id="sv-confirm-go" ${blocked ? "disabled" : ""}>${go}</button>
          </div>
        </div>
      </div>`;
  }

  // game_open names what the last backup skipped (the game may be closed by now)
  _svStatusLine(s) {
    const words = {
      never: "Never backed up", backing_up: "Backing up", up_to_date: "Up to date", limit_reached: "Limit reached",
      failed: "Failed",
    };
    const skipped = (s.last?.open || []).join(", ") || "a game";
    const parts = [s.status === "game_open"
      ? `The last backup skipped ${skipped}: it was open on the PS4. Close it, then back up again`
      : words[s.status] || s.status];
    if (s.last_success) parts.push(`last complete backup ${this._svWhen(s.last_success)}`);
    if (s.status === "failed" && s.last?.error) parts.push(s.last.error);
    return parts.join(" · ");
  }

  // ── Saves: the tab ──

  _renderSaves() {
    const s = this._svSettings;
    if (!s) {
      return `<div class="card"><h3>Saves</h3><div class="muted">${this._escapeHtml(this._svStatus || "Loading...")}</div></div>`;
    }
    const name = this._escapeHtml(this._entryName(this._selectedEntry()));
    const seg = (id, value, options) => `<div class="seg" id="${id}">${options
      .map(([v, label]) => `<button data-value="${v}" class="${String(value) === String(v) ? "active" : ""}">${label}</button>`)
      .join("")}</div>`;
    const on = s.mode !== "off";
    // Off / On (On is stored as "manual")
    const notes = {
      off: "Off: HA doesn't look at this PS4's saves.",
      manual: "On: HA copies the saves when you press Back up, here or on the device page (an automation can press that button too). A game that's open on the PS4 is skipped: close it first.",
    };
    const settings = html`
      <div class="card">
        <h3>Settings: ${name}</h3>
        <div class="ss-settings">
          <div>Save backup</div>${seg("sv-mode", s.mode, [["off", "Off"], ["manual", "On"]])}
          ${on ? html`
            <div>Storage limit</div>
            <div class="ss-limit">
              <input id="sv-limit" type="range" min="${s.limit_min_gb}" max="${s.limit_max_gb}" step="0.5" value="${s.limit_gb}">
              <span id="sv-limit-label">${Number(s.limit_gb).toFixed(1)} GB</span>
              <span class="muted">(${this._ssMB(s.usage.bytes)} used, ${s.usage.versions} older versions)</span>
            </div>
            <div>Folder</div><div class="muted" style="font-family:monospace">${this._escapeHtml(s.folder || "")}</div>
            <div>Status</div><div class="muted" id="sv-status-line">${this._escapeHtml(this._svStatusLine(s))}</div>` : ""}
        </div>
        <div class="muted" style="margin-top:10px">${notes[s.mode] || ""}</div>
        ${on ? "" : this._svStatus ? `<div class="ss-warn">${this._escapeHtml(this._svStatus)}</div>` : ""}
      </div>`;
    if (!on) return settings;

    const running = s.running;
    const asleep = s.power_state === "rest" || s.power_state === "off";
    const paneBtn = (pane, label) =>
      `<button data-act="pane" data-pane="${pane}" class="${this._svPane === pane ? "active" : ""}">${label}</button>`;
    return html`
      ${settings}
      <div class="card" id="sv-root">
        <div class="seg sv-pane-toggle">${paneBtn("ps4", "On PS4")}${paneBtn("ha", "In HA")}</div>
        <div class="sv-panes" data-show="${this._svPane}">
          ${this._renderSvPs4(s, running, asleep)}
          ${this._renderSvHa(s, running)}
        </div>
      </div>
      <div class="card">
        <details>
          <summary>How to restore</summary>
          <ol class="sv-howto">
            <li>Turn the PS4 on. Under <b>In HA</b>, press <b>Select</b>, tick saves (a game's box ticks all its newest copies; open a game for single saves and older versions), then <b>Send N to PS4...</b> and pick the console. HA puts the files in Apollo's Fake USB folder on that PS4 (<span class="sv-tid">/data/fakeusb/PS4/SAVEDATA/0000000000000000</span>; HA creates it), replacing whatever it sent before. It never touches the PS4's own saves.</li>
            <li>On the PS4, close the game, then open Apollo Save Tool (1.3.0 or later) → <b>USB Saves</b>. If a USB stick is plugged in, Apollo reads the stick instead: unplug it, or set Apollo's Settings → <b>USB Saves Source</b> → <b>Fake USB</b>.</li>
            <li>Pick the save → <b>Copy save game to HDD</b> (answer Yes to overwrite). Apollo re-signs it for the user who's logged in.</li>
            <li>Back here, <b>Remove from PS4</b> (under On PS4) deletes the files HA sent.</li>
          </ol>
          <div class="muted">No FTP on that PS4? <b>Download N...</b> gives the same files as a zip: unpack it to the root of an exFAT USB stick, then step 2 with the stick. The copies are the console's own encrypted files. They restore on this PS4 or another one running GoldHEN (Apollo checks the firmware: saves from newer games need 8.00 or later). Files stay on HA's disk in the folder above, and HA's full backups include them.</div>
        </details>
      </div>
    `;
  }

  // A pass in progress, in the panes' sticky toolbars so it stays in view while the list scrolls (the HA pane's only
  // shows on phones, one pane at a time). Its count is saves to copy, known once the PS4 has been read.
  _svProgress(s) {
    if (!s?.running) return "";
    const done = s.progress?.done || 0;
    const total = s.progress?.total || 0;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const label = total ? `Backing up ${done} of ${total} save${total === 1 ? "" : "s"}` : "Reading the PS4's saves...";
    return `
      <div class="sv-progress" role="progressbar" aria-label="Save backup" aria-valuemin="0"
        aria-valuemax="${total}" aria-valuenow="${done}" ${total ? "" : `aria-valuetext="${label}"`}>
        <div class="sv-progress-text"><span>${label}</span>${total ? `<span class="muted">${pct}%</span>` : ""}</div>
        <div class="sv-progress-track"><div class="sv-progress-fill${total ? "" : " sv-progress-wait"}" style="width:${total ? pct : 100}%"></div></div>
      </div>`;
  }

  // A tick box that can be half ticked (a game with some of its saves ticked)
  _svBox(ticked, total) {
    const state = !total ? "off" : ticked === total ? "on" : ticked ? "part" : "";
    return `<span class="sv-box ${state}" aria-hidden="true"></span>`;
  }

  _svBar(pane, toolbar) {
    const status = this._svStatusPane === pane && this._svStatus
      ? `<div class="muted bar-status">${this._escapeHtml(this._svStatus)}</div>` : "";
    const hint = this._svSelecting === pane && this._svHint
      ? `<div class="muted bar-status">${this._escapeHtml(this._svHint)}</div>` : "";
    return `<div class="sticky-bar">${this._svProgress(this._svSettings)}<div class="payload-toolbar">${toolbar}</div>${status}${hint}</div>`;
  }

  // A game row: the chevron opens it; in Select mode the row ticks the game's saves
  _svGameRow(pane, u, g, meta) {
    const esc = (v) => this._escapeHtml(v);
    const ids = `data-pane="${pane}" data-uid="${esc(u.uid)}" data-tid="${esc(g.title_id)}"`;
    const open = this._svOpen.has(`${pane}|${u.uid}|${g.title_id}`);
    const selecting = this._svSelecting === pane;
    const keys = this._svGameKeys(pane, g);
    const mine = selecting && this._svSel.uid === u.uid;
    const ticked = mine ? keys.filter((k) => this._svSel.keys.has(k)).length : 0;
    const pickable = selecting && keys.length;
    return `
      <div class="sv-grow ${ticked ? "sv-picked" : ""}" data-act="${pickable ? "pick-game" : "toggle"}" ${ids}>
        <button class="sv-chev" data-act="toggle" ${ids} aria-expanded="${open}" title="${open ? "Close" : "Open"}">${open ? "▾" : "▸"}</button>
        ${selecting ? this._svBox(ticked, keys.length) : ""}
        <div class="sv-gname">${this._gameLabel(g.game, g.title_id)}</div>
        <div class="sv-gmeta muted">${meta}</div>
      </div>`;
  }

  // A save row (or an older version): tick box in Select mode, name, when · size · slot ID, badge
  _svSaveRow(pane, u, g, { slot, stamp, name, when, bytes, badge, older, pickable }) {
    const esc = (v) => this._escapeHtml(v);
    const selecting = this._svSelecting === pane;
    const key = this._svKey(g.title_id, slot, stamp);
    const picked = selecting && this._svSel.uid === u.uid && this._svSel.keys.has(key);
    const act = selecting && pickable
      ? `data-act="pick" data-pane="${pane}" data-uid="${esc(u.uid)}" data-tid="${esc(g.title_id)}" data-slot="${esc(slot)}"${stamp ? ` data-stamp="${esc(stamp)}"` : ""}`
      : "";
    const details = [when, this._ssMB(bytes), name !== slot && !older ? `<span class="sv-tid">${esc(slot)}</span>` : ""]
      .filter(Boolean).join(" · ");
    return `
      <div class="sv-srow ${older ? "sv-older" : ""} ${act ? "sv-pickable" : ""} ${picked ? "sv-picked" : ""}" ${act}>
        <span>${selecting ? this._svBox(picked ? 1 : 0, pickable ? 1 : 0) : ""}</span>
        <div class="sv-sname">${esc(name)}<div class="muted sv-sdetail">${details}</div></div>
        <div class="sv-sbadge">${badge || ""}</div>
      </div>`;
  }

  _renderSvPs4(s, running, asleep) {
    const esc = (v) => this._escapeHtml(v);
    const r = this._svPs4;
    const users = r?.users || [];
    const all = users.flatMap((u) => u.games.flatMap((g) => g.slots.map((sl) => ({ g, sl }))));
    const count = (st) => all.filter((x) => x.sl.status === st).length;
    const fresh = all.filter((x) => x.sl.status !== "in_ha" && !x.g.open).length;
    const selecting = this._svSelecting === "ps4";
    const n = selecting ? this._svSel.keys.size : 0;
    const sleeping = s.power_state === "rest" ? "in rest mode" : "off";
    let state = "";
    if (this._svReading) state = "Reading the PS4...";
    else if (this._svPs4Error) state = this._svPs4Error;
    else if (!r?.read_at) state = asleep ? `The PS4 is ${sleeping}: turn it on to see its saves.` : "";
    else if (asleep) state = `The PS4 is ${sleeping}: this is what HA read ${this._svWhen(r.read_at)}.`;
    const toolbar = selecting
      ? html`
          <button class="btn btn-save" data-act="ask-backup-sel" ${n && !running && !asleep ? "" : "disabled"}>Back up ${n} to HA...</button>
          ${this._svAllButton("ps4")}
          <button class="btn" data-act="select" data-pane="ps4">Cancel</button>
          <span class="muted">${n ? `${esc(this._svUser("ps4", this._svSel.uid)?.name || "")}: ${n} ticked` : "Tap a game or a save to tick it (one user at a time)"}</span>`
      : html`
          <button class="btn btn-save" data-act="ask-backup" ${running || asleep || this._svReading ? "disabled" : ""}>${fresh ? `Back up ${fresh} new...` : "Back up now..."}</button>
          <button class="btn" data-act="select" data-pane="ps4" ${all.length && !running ? "" : "disabled"}>Select</button>
          <button class="btn" data-act="read-ps4" ${this._svReading || asleep || running ? "disabled" : ""}>Refresh</button>`;
    const parts = [`${all.length} save${all.length === 1 ? "" : "s"}`, `${count("in_ha")} in HA`];
    if (count("changed")) parts.push(`${count("changed")} changed since the backup`);
    if (count("not_in_ha")) parts.push(`${count("not_in_ha")} not in HA`);
    const summary = r?.read_at ? `${parts.join(" · ")} · read ${this._svAgo(r.read_at)}` : "";
    const badges = {
      in_ha: `<span class="pill sv-ok" title="HA's newest copy is this save">✓ In HA</span>`,
      changed: `<span class="pill sv-changed" title="The game saved again after HA's copy">● Changed</span>`,
      not_in_ha: `<span class="pill sv-new" title="HA has no copy of this save">○ Not in HA</span>`,
    };
    const userCards = users.map((u) => {
      const rows = u.games.map((g) => {
        const inHa = g.slots.filter((x) => x.status === "in_ha").length;
        const roll = g.open ? "Open on the PS4: close it to back up"
          : inHa === g.slots.length ? "✓ All in HA" : `${inHa} of ${g.slots.length} in HA`;
        const meta = `${esc(roll)}<br>${g.slots.length} save${g.slots.length === 1 ? "" : "s"} · ${esc(this._svWhen(g.last_saved))}`;
        let out = this._svGameRow("ps4", u, g, meta);
        if (this._svOpen.has(`ps4|${u.uid}|${g.title_id}`)) {
          out += g.slots.map((sl) => this._svSaveRow("ps4", u, g, {
            slot: sl.slot, name: this._svSaveName(sl), when: this._svWhen(sl.saved), bytes: sl.bytes,
            badge: g.open ? `<span class="pill">Open</span>` : badges[sl.status], pickable: !g.open,
          })).join("");
        }
        return out;
      }).join("");
      return `<div class="sv-user"><h4>${esc(u.name)}</h4>${rows || `<div class="muted">No saves for this user.</div>`}</div>`;
    }).join("");

    // What waits for Apollo: the folder as the last read found it, else HA's record of its last send
    const fake = r?.read_at ? r.fake_usb || [] : null;
    const sent = s.sent;
    let fakeUsb = "";
    if ((fake && fake.length) || (!fake && sent)) {
      const lines = fake
        ? fake.flatMap((g) => g.slots.map((x) => `${g.game !== g.title_id ? g.game : this._svName(g.title_id)} · ${x.name || x.slot}`))
        : sent.games;
      const from = sent ? `Sent by HA ${this._svWhen(sent.when)} from ${sent.from} · ${sent.user}.` : "Not sent from this HA.";
      fakeUsb = `
        <div class="sv-sent">
          <div class="sv-sent-text"><b>In Apollo's Fake USB folder</b>
            <ul class="sv-what">${lines.slice(0, 8).map((l) => `<li>${esc(l)}</li>`).join("")}${lines.length > 8 ? `<li>+${lines.length - 8} more</li>` : ""}</ul>
            <span class="muted">${esc(from)} On the PS4: Apollo → USB Saves → Copy save game to HDD.</span>
          </div>
          <button class="btn" data-act="clear-sent" ${this._svBusy || asleep || running ? "disabled" : ""}>Remove from PS4</button>
        </div>`;
    }
    return html`
      <section class="sv-pane sv-pane-ps4">
        <h3>On PS4</h3>
        <div class="muted sv-summary">${esc(summary)}</div>
        ${this._svBar("ps4", toolbar)}
        ${state ? `<div class="muted" style="margin-top:8px">${esc(state)}</div>` : ""}
        ${userCards}
        ${fakeUsb}
      </section>`;
  }

  _renderSvHa(s, running) {
    const esc = (v) => this._escapeHtml(v);
    const users = this._svList?.users || [];
    const selecting = this._svSelecting === "ha";
    const n = selecting ? this._svSel.keys.size : 0;
    const slots = users.flatMap((u) => u.games.flatMap((g) => g.slots));
    const newest = slots.filter((sl) => sl.saved).length;
    const older = slots.reduce((k, sl) => k + sl.versions.length, 0);
    const toolbar = selecting
      ? html`
          <button class="btn btn-save" data-act="ask-send-sel" ${n && !running && !this._svBusy ? "" : "disabled"}>Send ${n} to PS4...</button>
          <button class="btn" data-act="ask-zip-sel" ${n && !this._svBusy ? "" : "disabled"}>Download ${n}...</button>
          <button class="btn" data-act="ask-delete-sel" ${n && !running && !this._svBusy ? "" : "disabled"}>Delete ${n} from HA...</button>
          ${this._svAllButton("ha")}
          <button class="btn" data-act="select" data-pane="ha">Cancel</button>
          <span class="muted">${n ? `${esc(this._svUser("ha", this._svSel.uid)?.name || "")}: ${n} ticked` : "Tap a game, a save or an older version to tick it (one user at a time)"}</span>`
      : html`
          <button class="btn" data-act="select" data-pane="ha" ${newest || older ? "" : "disabled"}>Select</button>
          <button class="btn" data-act="reload-ha" ${running || this._svBusy ? "disabled" : ""}>Refresh</button>`;
    const summary = `${this._ssMB(s.usage.bytes)} of ${Number(s.limit_gb).toFixed(1)} GB · ${newest} newest cop${newest === 1 ? "y" : "ies"} · ${older} older version${older === 1 ? "" : "s"}`;
    const gone = `<span class="pill" title="The last read of the PS4 didn't find it">Not on the PS4</span>`;
    const userCards = users.map((u) => {
      const rows = u.games.map((g) => {
        const nSaves = g.slots.filter((sl) => sl.saved).length;
        const nOld = g.slots.reduce((k, sl) => k + sl.versions.length, 0);
        const meta = `${g.on_ps4 === false ? "Not on the PS4 now<br>" : ""}${nSaves} save${nSaves === 1 ? "" : "s"}${nOld ? ` + ${nOld} older` : ""} · ${this._ssMB(g.bytes)}${g.last_saved ? ` · ${esc(this._svWhen(g.last_saved))}` : ""}`;
        let out = this._svGameRow("ha", u, g, meta);
        if (this._svOpen.has(`ha|${u.uid}|${g.title_id}`)) {
          out += g.slots.map((sl) => {
            const head = sl.saved
              ? this._svSaveRow("ha", u, g, { slot: sl.slot, name: this._svSaveName(sl), when: `newest ${this._svWhen(sl.saved)}`,
                bytes: sl.bytes, badge: sl.on_ps4 === false ? gone : "", pickable: true })
              : `<div class="sv-srow"><span></span><div class="sv-sname">${esc(this._svSaveName(sl))}<div class="muted sv-sdetail">only older versions${sl.name ? ` · <span class="sv-tid">${esc(sl.slot)}</span>` : ""}</div></div><div></div></div>`;
            const versions = sl.versions.map((v) => this._svSaveRow("ha", u, g, {
              slot: sl.slot, stamp: v.stamp, name: v.name && v.name !== sl.name ? v.name : "Older copy",
              when: this._svStamp(v.stamp), bytes: v.bytes, older: true, pickable: true,
              badge: selecting ? (v.kept ? `<span class="pill">Keep</span>` : "")
                : `<label class="muted sv-keep"><input type="checkbox" data-act="keep" data-uid="${esc(u.uid)}" data-tid="${esc(g.title_id)}" data-slot="${esc(sl.slot)}" data-stamp="${esc(v.stamp)}" ${v.kept ? "checked" : ""} ${running ? "disabled" : ""}> Keep</label>`,
            })).join("");
            return head + versions;
          }).join("") + `<div class="muted sv-note">Older versions: every one from the last few saves, then one per hour for a day, one per day for 30 days, one per week after that. Keep exempts a version.</div>`;
        }
        return out;
      }).join("");
      return `<div class="sv-user"><h4>${esc(u.name)}</h4>${rows || `<div class="muted">No saves backed up for this user yet.</div>`}</div>`;
    }).join("");
    return html`
      <section class="sv-pane sv-pane-ha">
        <h3>In HA</h3>
        <div class="muted sv-summary">${esc(summary)}</div>
        ${this._svBar("ha", toolbar)}
        ${userCards || `<div class="muted" style="margin-top:8px">Nothing backed up yet.</div>`}
      </section>`;
  }

  _bindSavesEvents() {
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const mode = $("#sv-mode");
    if (mode) {
      mode.onclick = (ev) => {
        const b = ev.target.closest("button[data-value]");
        if (b && b.dataset.value !== this._svSettings.mode) this._svSave({ mode: b.dataset.value });
      };
    }
    const limit = $("#sv-limit");
    if (limit) {
      limit.oninput = () => {
        const label = $("#sv-limit-label");
        if (label) label.textContent = `${Number(limit.value).toFixed(1)} GB`;
      };
      limit.onchange = () => this._svSave({ limit_gb: Number(limit.value) });
    }
    const c = this._svConfirm;
    if (c) {
      const target = $("#sv-target");
      if (target) target.onchange = () => { c.target = target.value; };
      const also = $("#sv-also-versions");
      if (also) also.onchange = () => { c.alsoVersions = also.checked; c.ack = false; this._render(); };
      const ack = $("#sv-ack");
      if (ack) ack.onchange = () => { c.ack = ack.checked; this._render(); };
      const cancel = $("#sv-confirm-cancel");
      if (cancel) cancel.onclick = () => this._closeOverlay();
      const go = $("#sv-confirm-go");
      if (go) {
        go.onclick = () => {
          if (c.kind === "delete" && this._svDeletePlan(c).last.length && !c.ack) return;
          this._svConfirm = null;
          if (c.kind === "backup") this._svBackup(c.params);
          else if (c.kind === "zip") this._svDownload(c.params);
          else if (c.kind === "delete") {
            const extra = c.alsoVersions ? this._svOlderOf(c.params).map(({ kept, ...it }) => it) : [];
            this._svDelete({ ...c.params, items: [...c.params.items, ...extra] });
          } else {
            this._svTarget = c.target;  // the next Send starts from the same console
            this._svSend(c.params, c.target);
          }
        };
      }
    }
    const root = $("#sv-root");
    if (!root) return;
    root.onclick = (ev) => {
      const el = ev.target.closest("[data-act]");
      if (!el || el.disabled) return;
      const { act, pane, uid, tid, slot, stamp } = el.dataset;
      const ticked = { uid: this._svSel.uid, items: this._svItems() };
      if (act === "pane") {
        this._svPane = pane;
        this._render();
      }
      if (act === "select") {
        if (this._svSelecting === pane) this._svEndSelect();
        else this._svStartSelect(pane);
        this._render();
      }
      if (act === "toggle") {
        const key = `${pane}|${uid}|${tid}`;
        if (this._svOpen.has(key)) this._svOpen.delete(key);
        else this._svOpen.add(key);
        this._render();
      }
      if (act === "pick-game") {
        this._svTickGame(uid, tid, this._svGameKeys(pane, this._svGame(pane, uid, tid)));
        this._render();
      }
      if (act === "pick") {
        this._svTick(uid, this._svKey(tid, slot, stamp));
        this._render();
      }
      if (act === "pick-all") {
        this._svTickAll(pane);
        this._render();
      }
      if (act === "ask-backup") this._svAsk("backup", {});
      if (act === "ask-backup-sel") this._svAsk("backup", ticked);
      if (act === "ask-send-sel") this._svAsk("send", ticked);
      if (act === "ask-zip-sel") this._svAsk("zip", ticked);
      if (act === "ask-delete-sel") this._svAsk("delete", ticked);
      if (act === "read-ps4") this._svReadPs4(true);
      if (act === "reload-ha") this._svReloadHa();
      if (act === "keep") this._svKeep(uid, tid, slot, stamp, el.checked);
      if (act === "clear-sent") this._svClearSent();
    };
  }

  _render() {
    const entry = this._selectedEntry();
    // the console's HA name (the entry title repeats the IP)
    const entryLabel = entry ? `${this._escapeHtml(this._entryName(entry))} (${entry.ps4_host})` : "No PS4 configured";

    this.shadowRoot.innerHTML = html`
      <style>
        :host { display:block; padding:16px; font-family:sans-serif; background:var(--primary-background-color); color:var(--primary-text-color); height:100vh; overflow-y:auto; }
        .topbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; padding-bottom:12px; border-bottom:1px solid var(--divider-color); margin-bottom:12px; }
        .logo { height:44px; width:auto; display:block; }
        .picker { display:flex; gap:8px; align-items:center; background:var(--secondary-background-color); padding:8px 10px; border-radius:6px; flex-wrap:wrap; }
        select, input[type="text"], input[type="number"], input[type="search"] { padding:6px 8px; border-radius:6px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); }
        .tabs { display:flex; gap:8px; margin:12px 0 16px 0; flex-wrap:wrap; }
        .tabbtn { cursor:pointer; padding:8px 12px; border-radius:999px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); }
        .tabbtn.active { border-color:var(--primary-color); box-shadow:0 0 0 2px rgba(3,169,244,0.25); }
        .loading { font-style:italic; color:var(--secondary-text-color); margin:8px 0; }
        .card { background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:10px; padding:12px; margin-bottom:12px; }
        .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:8px 0; }
        .muted { color:var(--secondary-text-color); font-size:13px; }

        .header { display:flex; align-items:center; margin-bottom:16px; border-bottom:1px solid var(--divider-color); padding-bottom:8px; gap:8px; flex-wrap:wrap; }
        .path { flex:1; font-weight:bold; font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:200px; }
        .nav-btns { display:flex; gap:4px; }
        .upload-section { display:flex; align-items:center; gap:8px; background:var(--secondary-background-color); padding:4px 8px; border-radius:4px; }
        table { width:100%; border-collapse:collapse; margin-top:8px; }
        th { text-align:left; padding:8px; border-bottom:2px solid var(--divider-color); }
        td { padding:8px; border-bottom:1px solid var(--divider-color); vertical-align:middle; }
        tr:hover { background:var(--secondary-background-color); }
        .folder { color:var(--primary-color); cursor:pointer; text-decoration:underline; }
        .actions { display:flex; gap:4px; }
        .actions button, .nav-btns button, .btn { cursor:pointer; padding:6px 10px; background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:6px; color:var(--primary-text-color); }
        .actions button:hover, .nav-btns button:hover, .btn:hover { background:var(--secondary-background-color); }

        .editor-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); display:flex; align-items:center; justify-content:center; z-index:1000; }
        .editor-container { background:var(--primary-background-color); width:90%; height:90%; display:flex; flex-direction:column; padding:16px; border-radius:8px; box-shadow:0 4px 20px rgba(0,0,0,0.5); }
        #editor-textarea { flex:1; font-family:Consolas,Monaco,monospace; font-size:14px; padding:12px; border:1px solid var(--divider-color); background:var(--secondary-background-color); color:var(--primary-text-color); resize:none; outline:none; }
        .editor-actions { margin-top:16px; display:flex; justify-content:flex-end; gap:12px; }
        .editor-actions button { padding:8px 20px; border-radius:6px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); cursor:pointer; }
        .btn-save { background:var(--primary-color)!important; color:#fff!important; border:none!important; }
        .btn-save { font-weight:600; }
        .btn-danger { background:var(--error-color, #c62828)!important; color:#fff!important; border:none!important; font-weight:600; }
        .sv-ack { font-weight:600; }
        /* a disabled button looks disabled by its shape, not just its color (with a grey primary color, an
           enabled main button would look dead): no fill, dashed outline, faded */
        button:disabled { opacity:0.5; cursor:not-allowed!important; background:transparent!important;
          color:var(--secondary-text-color)!important; border:1px dashed var(--divider-color)!important; }
        .sv-dlg ul { margin:8px 0; padding-left:20px; }
        .sv-dlg li { margin:4px 0; }
        .sv-dlg ol { margin:8px 0; padding-left:20px; }
        .menu-btn { background:none; border:none; padding:4px; margin-right:-4px; color:var(--primary-text-color); cursor:pointer; }
        .menu-btn svg { width:24px; height:24px; fill:currentColor; display:block; }
        /* Toolbar + status stay in view while a long grid / list scrolls */
        .sticky-bar { position:sticky; top:-16px; z-index:5; background:var(--card-background-color); margin:0 -12px; padding:8px 12px; }
        .bar-status { margin-top:8px; }
        .game-tid { font-family:monospace; }

        pre.klog { margin-top:10px; height:45vh; overflow:auto; padding:10px; border:1px solid var(--divider-color); border-radius:8px; background:var(--secondary-background-color); white-space:pre-wrap; }

        /* Payload grid */
        .payload-toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:8px; }
        .payload-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap:10px; margin-top:12px; }
        .payload-card { border:1px solid var(--divider-color); border-radius:10px; overflow:hidden; background:var(--card-background-color); }
        .payload-btn { width:100%; padding:0; border:none; background:transparent; color:inherit; cursor:pointer; display:flex; flex-direction:column; align-items:stretch; }
        .payload-btn:hover { background:var(--secondary-background-color); }
        .payload-imgwrap { width:100%; aspect-ratio: 1 / 1; display:flex; align-items:center; justify-content:center; background:rgba(0,0,0,0.06); }
        .payload-imgwrap img { width:100%; height:100%; object-fit:cover; display:block; }
        .payload-name { padding:8px 10px; font-size:13px; line-height:1.2; word-break:break-word; }
        .payload-sendrow { padding:8px 10px; border-top:1px solid var(--divider-color); display:flex; justify-content:space-between; align-items:center; gap:8px; }
        .pill { font-size:12px; padding:4px 8px; border-radius:999px; border:1px solid var(--divider-color); background:var(--secondary-background-color); }

        /* Screenshots: same cards as the payload grid, 16:9 like the console's thumbnails */
        .ss-settings { display:grid; grid-template-columns: max-content minmax(0, 1fr); gap:10px 16px; align-items:center; }
        .ss-settings > * { min-width:0; overflow-wrap:anywhere; }
        .seg { display:inline-flex; justify-self:start; border:1px solid var(--divider-color); border-radius:999px; overflow:hidden; }
        .seg button { cursor:pointer; padding:6px 12px; border:none; background:var(--card-background-color); color:var(--primary-text-color); }
        .seg button + button { border-left:1px solid var(--divider-color); }
        .seg button.active { background:var(--primary-color); color:#fff; }
        .ss-limit { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
        .ss-limit input[type="range"] { width:min(320px, 60vw); }
        .ss-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap:10px; margin-top:12px; }
        .ss-card { position:relative; }
        .ss-card.selected { outline:3px solid var(--primary-color); outline-offset:-1px; }
        .ss-imgwrap { aspect-ratio: 16 / 9; }
        .ss-badges { position:absolute; top:6px; right:6px; display:flex; gap:4px; }
        .ss-badges .pill { background:rgba(0,0,0,0.6); color:#fff; border:none; }
        .ss-check { position:absolute; top:6px; left:6px; width:20px; height:20px; pointer-events:none; }
        .pill.in-ha { color:var(--success-color, #2e7d32); border-color:currentColor; }
        .ss-card .payload-name { text-align:center; }
        .ss-game { display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .ss-card .payload-sendrow { min-height:26px; }
        .ss-dialog label { display:flex; gap:8px; align-items:center; margin:10px 0; flex-wrap:wrap; min-width:0; }
        .ss-dialog select, .ss-dialog input { max-width:100%; min-width:0; }
        .ss-dialog .editor-container { height:auto; max-height:90%; width:min(560px, 92%); overflow:auto; box-sizing:border-box; }
        .ss-warn { color:var(--warning-color, #b26a00); margin-top:8px; }
        .ss-view-box { height:auto; max-height:95%; width:auto; max-width:96%; }
        .ss-view-img { width:min(92vw, calc(78vh * 16 / 9)); aspect-ratio:16 / 9; object-fit:contain; display:block; margin:0 auto;
          background:#000 center / contain no-repeat; }
        .ss-view-wait { opacity:0.5; }

        /* Play time */
        .pt-totals { display:grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap:12px; }
        .pt-num { font-size:22px; font-weight:600; margin-top:2px; }
        .pt-card h3 { margin:0 0 4px 0; font-size:16px; }
        .pt-tid { font-family:monospace; }
        .pill.pt-playing { color:var(--success-color, #2e7d32); border-color:currentColor; }

        /* Saves: two panes, On PS4 | In HA, side by side; one at a time below 900 px (a toggle) */
        .sv-panes { display:grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap:20px; align-items:start; }
        .sv-pane { min-width:0; }
        .sv-pane h3 { margin:0; }
        .sv-pane .sticky-bar { margin:0; padding:8px 0; }
        .sv-pane-toggle { display:none; margin-bottom:10px; }
        .sv-progress { margin-bottom:10px; }
        .sv-progress-text { display:flex; justify-content:space-between; gap:8px; margin-bottom:6px; }
        .sv-progress-track { height:8px; border-radius:4px; background:var(--divider-color); overflow:hidden; }
        .sv-progress-fill { height:100%; background:var(--primary-color); }
        .sv-progress-wait { opacity:0.35; }
        .sv-summary { margin-top:4px; }
        .sv-user { margin-top:12px; }
        .sv-user h4 { margin:8px 0 4px 0; }
        .sv-tid { font-family:monospace; }
        .sv-grow { display:flex; gap:8px; align-items:center; padding:8px 4px; border-bottom:1px solid var(--divider-color); cursor:pointer; }
        .sv-grow:hover, .sv-srow.sv-pickable:hover { background:var(--secondary-background-color); }
        .sv-chev { background:none; border:none; color:var(--primary-text-color); cursor:pointer; padding:2px; width:24px; flex:none; font-size:18px; line-height:1; }
        .sv-gname { flex:1; min-width:0; overflow-wrap:anywhere; }
        .sv-gmeta { text-align:right; font-size:12px; max-width:50%; }
        .sv-srow { display:grid; grid-template-columns: auto minmax(0, 1fr) auto; gap:4px 10px; align-items:center;
          padding:6px 4px 6px 34px; border-bottom:1px solid var(--divider-color); }
        .sv-srow.sv-older { padding-left:58px; font-size:14px; }
        .sv-srow.sv-pickable { cursor:pointer; }
        .sv-picked { background:var(--secondary-background-color); }
        .sv-sname { min-width:0; overflow-wrap:anywhere; }
        .sv-sdetail { font-size:12px; }
        .sv-keep { display:flex; gap:4px; align-items:center; }
        .sv-note { margin:6px 0 10px 34px; font-size:12px; }
        .sv-box { width:16px; height:16px; box-sizing:border-box; border:2px solid var(--secondary-text-color); border-radius:3px;
          display:inline-flex; align-items:center; justify-content:center; flex:none; vertical-align:middle; }
        .sv-box.on { background:var(--primary-color); border-color:var(--primary-color); }
        .sv-box.on::after { content:"✓"; color:#fff; font-size:11px; line-height:1; }
        .sv-box.part::after { content:""; width:8px; height:2px; background:var(--primary-text-color); }
        .sv-box.off { opacity:0.3; border-style:dashed; }
        .pill.sv-ok { color:var(--success-color, #2e7d32); border-color:currentColor; }
        .pill.sv-changed { color:var(--warning-color, #b26a00); border-color:currentColor; }
        .pill.sv-new { color:var(--error-color, #c62828); border-color:currentColor; }
        .sv-what { margin:6px 0; padding-left:20px; }
        .sv-howto { margin:8px 0; padding-left:20px; }
        .sv-howto li { margin:4px 0; }
        .sv-sent { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; padding:8px 10px; border-radius:6px; background:var(--secondary-background-color); }
        .sv-sent-text { flex:1 1 260px; min-width:0; }
        @media (min-width: 900px) {
          .sv-pane-ha .sv-progress { display:none; }
        }
        @media (max-width: 899px) {
          .sv-panes { grid-template-columns: minmax(0, 1fr); }
          .sv-pane-toggle { display:inline-flex; }
          .sv-panes[data-show="ps4"] > .sv-pane-ha, .sv-panes[data-show="ha"] > .sv-pane-ps4 { display:none; }
        }

        /* Phones: settings stack, two screenshots a row; Saves rows indent less */
        @media (max-width: 600px) {
          :host { padding:12px; }
          .sticky-bar { top:-12px; }
          .ss-settings { grid-template-columns: minmax(0, 1fr); gap:4px; }
          .ss-settings > :nth-child(even) { margin-bottom:8px; }
          .ss-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap:8px; }
          .ss-card .payload-name { padding:6px; font-size:12px; }
          .ss-card .payload-sendrow { padding:6px; }
          .sv-grow { flex-wrap:wrap; }
          .sv-gmeta { flex-basis:100%; max-width:none; text-align:left; padding-left:32px; }
          .sv-srow { padding-left:16px; }
          .sv-srow.sv-older { padding-left:32px; }
          .sv-note { margin-left:16px; }
        }
      </style>

      <div class="topbar">
        ${this._narrow ? `<button class="menu-btn" id="btn-menu" title="Menu" aria-label="Menu"><svg viewBox="0 0 24 24"><path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg></button>` : ""}
        <img class="logo" src="/api/ps4_goldhen/frontend/goldhen_logo.png" alt="GoldHEN">
        <div class="picker">
          <div>
            <div class="muted">Selected PS4</div>
            <div style="font-weight:600">${entryLabel}</div>
          </div>
          <select id="entry-select">
            ${this._entries
              .map((e) => {
                const label = `${this._escapeHtml(this._entryName(e))} — ${e.ps4_host}`;
                const sel = e.entry_id === this._selectedEntryId ? "selected" : "";
                return `<option value="${e.entry_id}" ${sel}>${label}</option>`;
              })
              .join("")}
          </select>
          <button class="btn" id="btn-reload-entries">Reload</button>
        </div>
      </div>

      <div class="tabs">
        <button class="tabbtn ${this._tab === "ftp" ? "active" : ""}" data-tab="ftp">FTP</button>
        <button class="tabbtn ${this._tab === "binloader" ? "active" : ""}" data-tab="binloader">Payloads</button>
        <button class="tabbtn ${this._tab === "klog" ? "active" : ""}" data-tab="klog">Klog</button>
        <button class="tabbtn ${this._tab === "saves" ? "active" : ""}" data-tab="saves">Saves</button>
        <button class="tabbtn ${this._tab === "screenshots" ? "active" : ""}" data-tab="screenshots">Screenshots</button>
        <button class="tabbtn ${this._tab === "playtime" ? "active" : ""}" data-tab="playtime">Play time</button>
      </div>

      ${this._loading ? `<div class="loading">Processing...</div>` : ""}

      ${this._tab === "ftp" ? this._renderFtp() : ""}
      ${this._tab === "binloader" ? this._renderBinLoader() : ""}
      ${this._tab === "klog" ? this._renderKlog() : ""}
      ${this._tab === "screenshots" ? this._renderScreenshots() : ""}
      ${this._tab === "playtime" ? this._renderPlayTime() : ""}
      ${this._tab === "saves" ? this._renderSaves() : ""}

      ${this._editing ? this._renderEditor() : ""}
      ${this._tab === "screenshots" && this._ssDialog ? this._renderSsDialog() : ""}
      ${this._tab === "screenshots" && this._ssDelete ? this._renderSsDelete() : ""}
      ${this._tab === "saves" && this._svConfirm ? this._renderSvConfirm() : ""}
      ${this._tab === "binloader" && this._plConfirm ? this._renderPlConfirm() : ""}
    `;

    const menu = this.shadowRoot.querySelector("#btn-menu");
    if (menu) menu.onclick = () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    this.shadowRoot.querySelectorAll(".editor-overlay[data-dismiss]").forEach((overlay) => {
      overlay.addEventListener("click", (ev) => {
        if (ev.target === overlay) this._closeOverlay();
      });
    });

    const sel = this.shadowRoot.querySelector("#entry-select");
    if (sel) {
      sel.onchange = async () => {
        this._selectEntry(sel.value);
        if (this._tab === "screenshots") {
          await this._ssLoad();  // no FTP listing of / for this tab
          return;
        }
        if (this._tab === "playtime") {
          await this._ptLoad();  // HA only, never the PS4
          return;
        }
        if (this._tab === "saves") {
          await this._svLoad();  // HA's copies, then the PS4 pane (HA's last read while under 5 min old)
          return;
        }
        await this._loadDir("/");
        this._render();
      };
    }

    const reloadBtn = this.shadowRoot.querySelector("#btn-reload-entries");
    if (reloadBtn) {
      reloadBtn.onclick = async () => {
        this._setLoading(true);
        await this._loadEntries();
        await this._loadPayloads();
        this._setLoading(false);
        if (this._selectedEntryId) await this._loadDir("/");
      };
    }

    this.shadowRoot.querySelectorAll(".tabbtn").forEach((b) => {
      b.onclick = () => {
        const nextTab = b.dataset.tab;

        if (this._tab === "klog" && nextTab !== "klog") {
          this._klogDisconnect(false);
        }

        if (this._tab === "binloader" && nextTab !== "binloader") {
          this._stopPayloadAutoRefresh();
          this._plConfirm = null;
        }

        if (this._tab === "screenshots" && nextTab !== "screenshots") {
          this._ssStopPoll();
          this._ssDialog = null;
          this._ssDelete = null;
        }

        if (this._tab === "playtime" && nextTab !== "playtime") this._ptStopPoll();
        if (this._tab === "saves" && nextTab !== "saves") {
          this._svStopPoll();
          this._svConfirm = null;
        }

        const opening = nextTab === "screenshots" && this._tab !== "screenshots";
        const openingPt = nextTab === "playtime" && this._tab !== "playtime";
        const openingSv = nextTab === "saves" && this._tab !== "saves";
        this._tab = nextTab;
        this._render();
        if (opening) this._ssLoad();  // the PS4 is only asked when the tab opens or on Refresh
        if (openingPt) this._ptLoad();
        if (openingSv) this._svLoad();
      };
    });

    if (this._tab === "screenshots") this._bindScreenshotsEvents();
    if (this._tab === "playtime") this._bindPlayTimeEvents();
    if (this._tab === "saves") this._bindSavesEvents();

    if (this._tab === "ftp") this._bindFtpEvents();

    if (this._tab === "binloader") {
      this._startPayloadAutoRefresh();

      const refreshPayloads = this.shadowRoot.querySelector("#btn-refresh-payloads");
      if (refreshPayloads) {
        refreshPayloads.onclick = async () => {
          this._setLoading(true);
          await this._loadPayloads();
          this._setLoading(false);
          this._render();
        };
      }

      const timeout = this.shadowRoot.querySelector("#bin-timeout");
      const filter = this.shadowRoot.querySelector("#payload-filter");

      if (timeout) timeout.oninput = () => (this._binTimeout = timeout.value);
      if (filter) {
        filter.oninput = () => {
          this._payloadFilter = filter.value || "";
          this._render();
        };
      }

      const grid = this.shadowRoot.querySelector("#payload-grid");
      if (grid) {
        grid.onclick = (ev) => {
          const btn = ev.target.closest("button[data-payload]");
          if (!btn) return;
          const payload = btn.dataset.payload;
          this._askSendPayload(payload);
        };
      }
      const c = this._plConfirm;
      if (c) {
        const cancel = this.shadowRoot.querySelector("#pl-confirm-cancel");
        if (cancel) cancel.onclick = () => this._closeOverlay();
        const go = this.shadowRoot.querySelector("#pl-confirm-go");
        if (go) {
          go.onclick = () => {
            this._plConfirm = null;
            this._sendPayload(c.payload, c.target);
          };
        }
      }
    }

    if (this._tab === "klog") {
      const c = this.shadowRoot.querySelector("#btn-klog-connect");
      const d = this.shadowRoot.querySelector("#btn-klog-disconnect");
      const clr = this.shadowRoot.querySelector("#btn-klog-clear");
      if (c) c.onclick = () => this._klogConnect();
      if (d) d.onclick = () => this._klogDisconnect(true);
      if (clr) clr.onclick = () => this._klogClear();

      const box = this.shadowRoot.querySelector("#klog-box");
      if (box) {
        box.textContent = this._klogLines.join("\n");
        box.scrollTop = box.scrollHeight;
      }

      if (!this._klogUnsub && !this._klogConnecting && !this._klogManuallyDisconnected && this._selectedEntryId) {
        setTimeout(() => {
          if (this.isConnected && this._tab === "klog") this._klogConnect();
        }, 0);
      }
    }

    if (this._editing) {
      const cancel = this.shadowRoot.querySelector("#btn-cancel");
      const save = this.shadowRoot.querySelector("#btn-save");
      if (cancel) cancel.onclick = () => { this._editing = null; this._render(); };
      if (save) save.onclick = () => this._saveFile();
    }
  }

  _escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  _formatModified(entry) {
    if (!entry.modified_utc) return entry.modified || "Unknown";
    const date = new Date(entry.modified_utc);
    if (Number.isNaN(date.getTime())) return "Unknown";
    return date.toLocaleString(undefined, {timeZoneName: "short"});
  }

  _renderFtp() {
    return html`
      <div class="header">
        <div class="path">PS4 FTP: ${this._escapeHtml(this._path)}</div>
        <div class="nav-btns">
          <button id="btn-root">Root</button>
          <button id="btn-back">Back</button>
          <button id="btn-refresh">Refresh</button>
        </div>
        <div class="upload-section">
          <input type="file" id="ftp-upload-input" style="display:none">
          <button id="btn-ftp-select">Select file</button>
          <button id="btn-ftp-upload">Upload to current folder</button>
        </div>
      </div>
      ${this._ftpError ? `<div class="ss-warn">${this._escapeHtml(this._ftpError)}</div>` : ""}

      <table>
        <thead>
          <tr><th>Name</th><th>Size</th><th>Modified</th><th>Actions</th></tr>
        </thead>
        <tbody>
          ${this._ftpEntries.map(e => `
            <tr>
              <td>
                <span class="${e.is_dir ? "folder" : "file"}" data-path="${this._escapeHtml(e.path)}" data-isdir="${e.is_dir}">
                  ${e.is_dir ? "📁" : "📄"} ${this._escapeHtml(e.name)}
                </span>
              </td>
              <td>${e.is_dir ? "-" : (typeof e.size === "number" ? (e.size / 1024 / 1024).toFixed(2) + " MB" : "Unknown")}</td>
              <td title="${this._escapeHtml(e.modified_utc || 'FTP list date (no time zone)')}">${this._escapeHtml(this._formatModified(e))}</td>
              <td class="actions">
                ${!e.is_dir ? `<button data-action="download" title="Download" data-path="${this._escapeHtml(e.path)}" data-name="${this._escapeHtml(e.name)}">💾</button>` : ""}
                ${!e.is_dir ? `<button data-action="edit" title="Edit" data-path="${this._escapeHtml(e.path)}" data-name="${this._escapeHtml(e.name)}">✏️</button>` : ""}
                <button data-action="rename" title="Rename" data-path="${this._escapeHtml(e.path)}" data-name="${this._escapeHtml(e.name)}">🏷️</button>
                <button data-action="delete" title="Delete" data-path="${this._escapeHtml(e.path)}" data-isdir="${e.is_dir}" data-name="${this._escapeHtml(e.name)}">🗑️</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
  }

  _renderBinLoader() {
    const shownTimeout = this._binTimeout || "30";

    const filter = (this._payloadFilter || "").trim().toLowerCase();
    const payloads = (this._payloads || []).filter((p) => {
      if (!filter) return true;
      return String(p).toLowerCase().includes(filter);
    });

    return html`
      <div class="card">
        <h3>Payloads (tap one to send it)</h3>

        <div class="payload-toolbar">
          <input id="bin-timeout" type="number" placeholder="Timeout (s)" value="${shownTimeout}" style="width:160px;">
          <button class="btn" id="btn-refresh-payloads">Refresh</button>
          <input id="payload-filter" type="search" placeholder="Filter payloads..." value="${this._payloadFilter || ""}" style="min-width:220px;">
          <span class="pill">Auto-refresh: ${Math.round(this._payloadAutoRefreshMs / 1000)}s</span>
        </div>

        <div class="muted" style="margin-top:8px;">
          Payload directory on HA: ${this._escapeHtml(this._payloadDir || "(unknown)")}
        </div>

        <div id="payload-grid" class="payload-grid">
          ${payloads.length
            ? payloads
                .map((p) => {
                  const icon = this._payloadIconUrl(p);
                  const name = this._escapeHtml(p);
                  return `
                    <div class="payload-card">
                      <button class="payload-btn" data-payload="${name}">
                        <div class="payload-imgwrap">
                          <img src="${icon}" alt="${name}">
                        </div>
                        <div class="payload-name">${name}</div>
                        <div class="payload-sendrow">
                          <span class="muted">Send</span>
                          <span class="pill">BIN/ELF</span>
                        </div>
                      </button>
                    </div>
                  `;
                })
                .join("")
            : `<div class="muted">No payloads found. Put .bin/.elf files in the payload folder, then hit Refresh.</div>`}
        </div>
      </div>

      ${this._binStatus ? `<div class="card"><h3>Status</h3><div>${this._escapeHtml(this._binStatus)}</div></div>` : ""}
    `;
  }

  // ── Screenshots ─────────────────────────────────────────────
  // Settings live in HA (.storage, per console); the PS4 is only asked when the tab opens, on Refresh / Show more,
  // and for a copy. Thumbnails come inline from HA's cache of the console's own 440x248 previews.

  _ssReset() {
    this._ssSettings = null;  // ps4_goldhen/screenshots/get
    this._ssShots = [];
    this._ssMore = false;
    this._ssStatus = "";
    this._ssBusy = false;     // a list / plan / import call is running
    this._ssSelecting = false;
    this._ssSelected = new Set();
    this._ssDialog = null;    // Sync dialog: {plan, kind, titleId, count}
    this._ssView = null;      // full-size viewer: {url, shot, note, failed}
    this._ssDelete = null;    // delete dialog: {ha, ps4}
    this._ssPollTimer = null;
  }

  _ssStopPoll() {
    if (this._ssPollTimer) {
      clearInterval(this._ssPollTimer);
      this._ssPollTimer = null;
    }
  }

  _ssCall(type, extra = {}) {
    return this._hass.callWS({ type: `ps4_goldhen/screenshots/${type}`, entry_id: this._selectedEntryId, ...extra });
  }

  _ssMB(bytes) {
    const mb = (bytes || 0) / 1048576;
    return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
  }

  // The file name's time is the console's clock: shown as is, no time zone conversion
  _ssTaken(taken) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(taken || "");
    if (!m) return "";
    return this._when(new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
  }

  async _ssLoad() {
    if (!this._hass || !this._selectedEntryId) return;
    const entryId = this._selectedEntryId;
    try {
      const settings = await this._ssCall("get");
      if (entryId !== this._selectedEntryId) return;
      this._ssSettings = settings;
    } catch (e) {
      this._ssStatus = `Couldn't read the screenshot settings: ${e.message || e}`;
      this._render();
      return;
    }
    this._ssShots = [];
    this._ssMore = false;
    this._render();
    if (this._ssSettings.syncing) this._ssStartPoll();
    if (this._ssSettings.enabled) await this._ssLoadShots(true);
  }

  async _ssLoadShots(reset) {
    if (this._ssBusy || !this._ssSettings?.enabled) return;
    const entryId = this._selectedEntryId;
    const offset = reset ? 0 : this._ssShots.length;
    this._ssBusy = true;
    this._ssStatus = reset ? "Reading the PS4's newest screenshots..." : "Loading more...";
    this._render();
    try {
      const resp = await this._ssCall("list", { offset, count: this._ssPage });
      if (entryId !== this._selectedEntryId) return;
      this._ssShots = reset ? resp.shots : this._ssShots.concat(resp.shots);
      this._ssMore = resp.more;
      this._ssStatus = this._ssShots.length ? "" : "No screenshots on this PS4 yet.";
    } catch (e) {
      this._ssStatus = `${e.message || e}`;
    } finally {
      this._ssBusy = false;
      this._render();
    }
  }

  async _ssSave(changes) {
    try {
      this._ssSettings = await this._ssCall("set", changes);
    } catch (e) {
      this._ssStatus = `Couldn't save the screenshot settings: ${e.message || e}`;
      this._render();
      return;
    }
    if (changes.enabled === true) {
      await this._ssLoadShots(true);
      return;
    }
    if (changes.enabled === false) {
      this._ssShots = [];
      this._ssStatus = "";
    }
    if (changes.enabled === false) {
      this._ssSelecting = false;
      this._ssSelected.clear();
    }
    this._render();
  }

  // a tap only views. HA's copy if it has one; otherwise HA reads the shot from the PS4 for the
  // viewer and keeps nothing. The viewer's Sync to HA keeps it. Asleep: the thumbnail + a note, no FTP.
  async _ssCardClick(rel) {
    const shot = this._ssShots.find((s) => s.rel === rel);
    if (!shot) return;
    if (this._ssSelecting) {
      if (this._ssSelected.has(rel)) this._ssSelected.delete(rel);
      else this._ssSelected.add(rel);
      this._render();
      return;
    }
    const asleep = !shot.in_ha && this._ssSettings.power_state !== "on";
    const view = { url: null, shot, note: asleep ? "The PS4 is in rest mode or off: turn it on to see this one full size." : "" };
    this._ssView = view;
    this._render();
    if (asleep) return;
    try {
      const path = `/api/ps4_goldhen/screenshots/${this._selectedEntryId}/full?rel=${encodeURIComponent(shot.rel)}`;
      view.url = await this._signedPath(path);
    } catch (e) {
      view.note = `${e.message || e}`;
    }
    if (this._ssView === view) this._render();
  }

  // The viewer's Sync to HA: this one shot into HA's media folder
  async _ssViewSync() {
    const view = this._ssView;
    if (!view || view.shot.in_ha || this._ssBusy) return;
    this._ssBusy = true;
    view.note = "Syncing it to HA...";
    this._render();
    try {
      const result = await this._ssCall("import", { rel: view.shot.rel });
      this._ssSettings = await this._ssCall("get");
      if (result.in_ha) {
        view.shot.in_ha = true;
        view.note = "Synced to HA.";
      } else {
        view.note = "Not synced: it doesn't fit under the storage limit (or HA would have less than 1 GB free).";
      }
    } catch (e) {
      view.note = `Couldn't sync: ${e.message || e}`;
    } finally {
      this._ssBusy = false;
      this._render();
    }
  }

  async _ssOpenSync() {
    if (this._ssBusy) return;
    this._ssBusy = true;
    this._ssStatus = "Counting the screenshots on the PS4...";
    this._render();
    try {
      const plan = await this._ssCall("plan");
      this._ssDialog = { plan, kind: "all", titleId: plan.new[0]?.title_id || "", count: Math.min(10, plan.new.length) || 1 };
      this._ssStatus = "";
    } catch (e) {
      this._ssStatus = `${e.message || e}`;
    } finally {
      this._ssBusy = false;
      this._render();
    }
  }

  // What the dialog's choice covers, newest first (plan.new is newest first)
  _ssChosen() {
    const d = this._ssDialog;
    if (d.kind === "game") return d.plan.new.filter((s) => s.title_id === d.titleId);
    if (d.kind === "newest") return d.plan.new.slice(0, Math.max(1, d.count | 0));
    return d.plan.new;
  }

  // Same rule as capture_files.newest_that_fit: newest first until the next one doesn't fit
  _ssFit(items, room) {
    const fit = [];
    let used = 0;
    for (const s of items) {
      if (used + s.size > room) break;
      fit.push(s);
      used += s.size;
    }
    return fit;
  }

  async _ssStartSync(scope) {
    try {
      const result = await this._ssCall("sync", scope);
      this._ssDialog = null;
      this._ssSelecting = false;
      this._ssSelected.clear();
      this._ssStatus = result.total ? "" : "Nothing new to copy.";
      this._ssSettings = await this._ssCall("get");
      if (this._ssSettings.syncing) this._ssStartPoll();
    } catch (e) {
      this._ssDialog = null;
      this._ssStatus = `Couldn't sync: ${e.message || e}`;
    }
    this._render();
  }

  _ssStartPoll() {
    if (this._ssPollTimer) return;
    const entryId = this._selectedEntryId;
    this._ssPollTimer = setInterval(async () => {
      if (this._tab !== "screenshots" || entryId !== this._selectedEntryId) {
        this._ssStopPoll();
        return;
      }
      try {
        this._ssSettings = await this._ssCall("get");
      } catch (_) {
        return;
      }
      if (this._ssSettings.syncing) {
        this._render();
        return;
      }
      this._ssStopPoll();
      const message = this._ssResultText(this._ssSettings.last_sync || {});
      await this._ssLoadShots(true);  // fresh In HA badges
      this._ssStatus = message;
      this._render();
    }, 1000);
  }

  _ssResultText(last) {
    if (last.state === "failed") return `Stopped after ${last.copied || 0} synced: ${last.error}`;
    if (last.state === "limit_reached") return `Synced ${last.copied || 0}; ${last.skipped} didn't fit under the storage limit.`;
    return `Done: ${last.copied || 0} synced to HA.`;
  }

  // Select mode acts on the ticks only. Sync N to HA = the ticked PS4-only shots (add-only, the count is on
  // the button, so no second ask); Delete = a dialog that asks HA copy and/or PS4 original every time.
  async _ssCopySelected() {
    const picked = this._ssShots.filter((s) => this._ssSelected.has(s.rel) && !s.in_ha);
    if (!picked.length) return;
    await this._ssStartSync({ kind: "selected", rels: picked.map((x) => x.rel) });
  }

  _ssOpenDelete() {
    if (!this._ssSelected.size) return;
    // HA copy ticked, PS4 original not (the safe default); none of them in HA: nothing ticked
    const inHa = this._ssShots.some((x) => x.in_ha && this._ssSelected.has(x.rel));
    this._ssDelete = { ha: inHa, ps4: false };
    this._render();
  }

  async _ssRunDelete() {
    const d = this._ssDelete;
    const rels = [...this._ssSelected];
    if (!d || !rels.length || (!d.ha && !d.ps4)) return;
    this._ssDelete = null;
    this._ssBusy = true;
    this._ssStatus = `Deleting ${rels.length}...`;
    this._render();
    let message;
    try {
      const r = await this._ssCall("delete", { rels, ha: d.ha, ps4: d.ps4 });
      const parts = [];
      if (d.ha) parts.push(`${r.ha_deleted} HA cop${r.ha_deleted === 1 ? "y" : "ies"}`);
      if (d.ps4) parts.push(`${r.ps4_deleted} from the PS4`);
      message = `Deleted ${parts.join(" and ")}.`;
      this._ssSelecting = false;
      this._ssSelected.clear();
      this._ssSettings = await this._ssCall("get");
    } catch (e) {
      message = `${e.message || e}`;
    }
    this._ssBusy = false;
    await this._ssLoadShots(true);
    this._ssStatus = message;
    this._render();
  }

  _renderSsDelete() {
    const d = this._ssDelete;
    const picked = this._ssShots.filter((x) => this._ssSelected.has(x.rel));
    const inHa = picked.filter((x) => x.in_ha).length;
    const n = picked.length;
    const plural = (k, one, many) => `${k} ${k === 1 ? one : many}`;
    const what = [d.ha && inHa ? plural(inHa, "HA copy", "HA copies") : "", d.ps4 ? plural(n, "PS4 original", "PS4 originals") : ""]
      .filter(Boolean).join(" and ");
    // none in HA → the HA box is unticked and says so; the Delete button stays, disabled with a hint
    const haBox = inHa
      ? `<label><input type="checkbox" id="ss-del-ha" ${d.ha ? "checked" : ""}>
          HA copy (${inHa} of ${n} ${inHa === 1 ? "is" : "are"} in HA)</label>`
      : `<label class="muted"><input type="checkbox" disabled> HA copy (none of these are in HA)</label>`;
    return html`
      <div class="editor-overlay ss-dialog" id="ss-delete-dialog" data-dismiss>
        <div class="editor-container">
          <h3 style="margin-top:0">Delete ${plural(n, "screenshot", "screenshots")}: ${this._escapeHtml(this._entryName(this._selectedEntry()))}</h3>
          ${haBox}
          <label><input type="checkbox" id="ss-del-ps4" ${d.ps4 ? "checked" : ""}>
            PS4 original</label>
          ${d.ps4 ? `<div class="ss-warn">Deleting from the PS4 is permanent: the console's own copy is gone, HA's copy stays only if "HA copy" is unticked.</div>` : ""}
          ${what ? "" : `<div class="muted">Tick what to delete.</div>`}
          <div class="editor-actions">
            <button id="ss-del-cancel">Cancel</button>
            <button class="btn-danger" id="ss-del-go" ${what ? "" : "disabled"}>${what ? `Delete ${what}` : "Delete"}</button>
          </div>
        </div>
      </div>`;
  }

  _renderScreenshots() {
    const s = this._ssSettings;
    if (!s) {
      return `<div class="card"><h3>Screenshots</h3><div class="muted">${this._escapeHtml(this._ssStatus || "Loading...")}</div></div>`;
    }
    const name = this._escapeHtml(this._entryName(this._selectedEntry()));
    const seg = (id, value, options) => `<div class="seg" id="${id}">${options
      .map(([v, label]) => `<button data-value="${v}" class="${String(value) === String(v) ? "active" : ""}">${label}</button>`)
      .join("")}</div>`;
    // Off / On only: viewing reads from the PS4; syncing is always on request.
    const settings = html`
      <div class="card">
        <h3>Settings: ${name}</h3>
        <div class="ss-settings">
          <div>Screenshots</div>${seg("ss-enabled", s.enabled, [[false, "Off"], [true, "On"]])}
          ${s.enabled ? html`
            <div>Storage limit</div>
            <div class="ss-limit">
              <input id="ss-limit" type="range" min="${s.limit_min_gb}" max="${s.limit_max_gb}" step="0.5" value="${s.limit_gb}">
              <span id="ss-limit-label">${Number(s.limit_gb).toFixed(1)} GB</span>
              <span class="muted">(${this._ssMB(s.usage.bytes)} used, ${s.usage.count} in HA)</span>
            </div>
            <div>Folder</div><div class="muted" style="font-family:monospace">${this._escapeHtml(s.folder || "")}</div>` : ""}
        </div>
        <div class="muted" style="margin-top:10px">${s.enabled
          ? "Tap a shot to see it: HA reads it from the PS4 and keeps nothing. Sync copies shots into HA's media folder: Sync all..., or Select, tick the shots, Sync N to HA."
          : "Off: HA doesn't look at this PS4's screenshots."}</div>
      </div>`;
    if (!s.enabled) return settings;

    const picked = this._ssShots.filter((x) => this._ssSelected.has(x.rel) && !x.in_ha).length;
    const ticked = this._ssSelected.size;
    const hint = !ticked ? "Tap shots to tick them"
      : !picked ? `${ticked === 1 ? "It's" : "They're"} already in HA` : ticked > picked ? `${ticked - picked} already in HA` : "";
    const toolbar = this._ssSelecting
      ? html`
          <button class="btn btn-save" id="ss-copy-selected" ${picked && !s.syncing ? "" : "disabled"}>Sync ${picked} to HA</button>
          <button class="btn" id="ss-delete" ${ticked && !s.syncing && !this._ssBusy ? "" : "disabled"}>Delete ${ticked}...</button>
          <button class="btn" id="ss-select">Cancel</button>
          ${hint ? `<span class="muted">${hint}</span>` : ""}`
      : html`
          <button class="btn" id="ss-sync" ${s.syncing || this._ssBusy ? "disabled" : ""}>Sync all...</button>
          <button class="btn" id="ss-select">Select</button>
          <button class="btn" id="ss-refresh" ${this._ssBusy ? "disabled" : ""}>Refresh</button>`;
    // one line per game name (even card heights), "Unknown game" for a bare title ID, a pill only
    // for "In HA"
    const cards = this._ssShots.map((shot) => {
      const selected = this._ssSelected.has(shot.rel);
      const img = shot.thumb ? `<img src="${shot.thumb}" alt="">` : `<span class="muted">No preview</span>`;
      const named = shot.game && shot.game !== shot.title_id;
      const game = named ? shot.game : "Unknown game";
      return `
        <div class="payload-card ss-card ${selected ? "selected" : ""}">
          <button class="payload-btn" data-rel="${this._escapeHtml(shot.rel)}" title="${this._escapeHtml(`${named ? shot.game : shot.title_id || ""} · ${shot.name}`)}">
            <div class="payload-imgwrap ss-imgwrap">${img}</div>
            <div class="payload-name"><span class="ss-game">${this._escapeHtml(game)}</span><span class="muted">${this._escapeHtml(this._ssTaken(shot.taken))}</span></div>
            <div class="payload-sendrow">
              ${shot.in_ha ? `<span class="pill in-ha">In HA</span>` : "<span></span>"}
              <span class="muted">${this._ssMB(shot.size)}</span>
            </div>
          </button>
          ${shot.trophy ? `<div class="ss-badges"><span class="pill">Trophy</span></div>` : ""}
          ${this._ssSelecting ? `<input class="ss-check" type="checkbox" tabindex="-1" ${selected ? "checked" : ""}>` : ""}
        </div>`;
    }).join("");

    return html`
      ${settings}
      <div class="card">
        <h3>Newest on the PS4</h3>
        <div class="sticky-bar">
          <div class="payload-toolbar">
            ${toolbar}
            ${s.syncing ? `<span class="pill">Syncing ${s.progress.done} / ${s.progress.total}...</span>` : ""}
          </div>
          ${this._ssStatus ? `<div class="muted bar-status">${this._escapeHtml(this._ssStatus)}</div>` : ""}
        </div>
        <div class="ss-grid" id="ss-grid">${cards}</div>
        ${this._ssMore ? `<div class="row"><button class="btn" id="ss-more" ${this._ssBusy ? "disabled" : ""}>Show more</button></div>` : ""}
      </div>
      ${this._ssView ? this._renderSsView() : ""}
    `;
  }

  // a fixed 16:9 box, the thumbnail first (dimmed until the full size is there, then behind it).
  // a PS4-only shot is read from the console for this view ("On the PS4 · not in HA" + Sync to HA).
  _renderSsView() {
    const { url, shot, note, failed } = this._ssView;
    const safeUrl = this._escapeHtml(url || "");
    const thumb = shot.thumb ? this._escapeHtml(shot.thumb) : "";
    const img = url && !failed
      ? `<img class="ss-view-img" id="ss-view-img" src="${safeUrl}" alt="" ${thumb ? `style="background-image:url('${thumb}')"` : ""}>`
      : thumb ? `<img class="ss-view-img ss-view-wait" src="${thumb}" alt="">` : `<div class="ss-view-img"></div>`;
    const game = shot.game && shot.game !== shot.title_id ? shot.game : "Unknown game";
    const where = shot.in_ha ? `<span class="pill in-ha">In HA</span>` : `<span class="muted">On the PS4 · not in HA</span>`;
    return html`
      <div class="editor-overlay" id="ss-view" data-dismiss>
        <div class="editor-container ss-view-box">
          ${img}
          <div class="editor-actions" style="justify-content:space-between; align-items:center; flex-wrap:wrap;">
            <span class="muted">${this._escapeHtml(game)} · ${this._escapeHtml(this._ssTaken(shot.taken))} · ${this._escapeHtml(shot.name)}</span>
            <span class="row" style="margin:0">
              ${where}
              ${shot.in_ha ? "" : `<button class="btn btn-save" id="ss-view-sync" ${this._ssBusy || this._ssSettings.syncing ? "disabled" : ""}>Sync to HA</button>`}
              ${url && !failed ? `<a href="${safeUrl}" target="_blank" rel="noopener">Open full size</a>` : ""}
              <button class="btn" id="ss-view-close">Close</button>
            </span>
          </div>
          ${note ? `<div class="muted bar-status">${this._escapeHtml(note)}</div>` : ""}
        </div>
      </div>`;
  }

  _renderSsDialog() {
    const d = this._ssDialog;
    const p = d.plan;
    const sum = (items) => items.reduce((a, x) => a + x.size, 0);
    const games = {};
    for (const x of p.new) {
      const g = (games[x.title_id] ||= { game: x.game, n: 0, bytes: 0 });
      g.n += 1;
      g.bytes += x.size;
    }
    const chosen = this._ssChosen();
    const fit = this._ssFit(chosen, p.room_bytes);
    const radio = (kind, label) =>
      `<label><input type="radio" name="ss-kind" value="${kind}" ${d.kind === kind ? "checked" : ""}> ${label}</label>`;
    const gameSelect = `<select id="ss-game">${Object.entries(games)
      .map(([tid, g]) => `<option value="${this._escapeHtml(tid)}" ${tid === d.titleId ? "selected" : ""}>${this._escapeHtml(g.game)} (${g.n}, ${this._ssMB(g.bytes)})</option>`)
      .join("")}</select>`;
    const limit = this._ssSettings.limit_gb;
    let warn = "";
    if (chosen.length && !fit.length) {
      warn = `None of these fit: ${this._ssMB(p.used_bytes)} of the ${limit} GB limit is used (and 1 GB of disk stays free). Raise the limit first.`;
    } else if (fit.length < chosen.length) {
      warn = `Only ${fit.length} of these ${chosen.length} fit under the ${limit} GB limit (1 GB of disk stays free): HA syncs the newest ${fit.length}.`;
    }
    return html`
      <div class="editor-overlay ss-dialog" id="ss-dialog" data-dismiss>
        <div class="editor-container">
          <h3 style="margin-top:0">Sync to HA: ${this._escapeHtml(this._entryName(this._selectedEntry()))}</h3>
          <div class="muted">${p.new.length} new on the PS4 (${this._ssMB(sum(p.new))}) · ${p.in_ha} already in HA · room under the limit: ${this._ssMB(p.room_bytes)}</div>
          ${p.new.length ? html`
            ${radio("all", `All new: ${p.new.length} (${this._ssMB(sum(p.new))})`)}
            ${radio("game", `One game: ${gameSelect}`)}
            ${radio("newest", `Newest <input id="ss-count" type="number" min="1" max="${p.new.length}" value="${d.count}" style="width:80px"> of the new ones`)}
            ${warn ? `<div class="ss-warn">${warn}</div>` : ""}
          ` : `<div style="margin:12px 0">Everything on the PS4 is already in HA.</div>`}
          <div class="editor-actions">
            <button id="ss-cancel">Cancel</button>
            ${fit.length ? `<button class="btn-save" id="ss-go">Sync ${fit.length} (${this._ssMB(sum(fit))})</button>` : ""}
          </div>
        </div>
      </div>`;
  }

  _bindScreenshotsEvents() {
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    const on = (sel, fn) => {
      const el = $(sel);
      if (el) el.onclick = fn;
    };
    const seg = (sel, fn) => on(sel, (ev) => {
      const b = ev.target.closest("button[data-value]");
      if (b) fn(b.dataset.value);
    });
    seg("#ss-enabled", (v) => {
      const enabled = v === "true";
      if (enabled !== this._ssSettings.enabled) this._ssSave({ enabled });
    });
    const limit = $("#ss-limit");
    if (limit) {
      limit.oninput = () => {
        const label = $("#ss-limit-label");
        if (label) label.textContent = `${Number(limit.value).toFixed(1)} GB`;
      };
      limit.onchange = () => this._ssSave({ limit_gb: Number(limit.value) });
    }
    on("#ss-refresh", () => this._ssLoadShots(true));
    on("#ss-more", () => this._ssLoadShots(false));
    on("#ss-sync", () => this._ssOpenSync());
    on("#ss-select", () => {
      this._ssSelecting = !this._ssSelecting;
      this._ssSelected.clear();
      this._render();
    });
    on("#ss-copy-selected", () => this._ssCopySelected());
    on("#ss-delete", () => this._ssOpenDelete());
    const del = this._ssDelete;
    if (del) {
      const ha = $("#ss-del-ha");
      const ps4 = $("#ss-del-ps4");
      if (ha) ha.onchange = () => { del.ha = ha.checked; this._render(); };
      if (ps4) ps4.onchange = () => { del.ps4 = ps4.checked; this._render(); };
      on("#ss-del-cancel", () => { this._ssDelete = null; this._render(); });
      on("#ss-del-go", () => this._ssRunDelete());
    }
    on("#ss-grid", (ev) => {
      const b = ev.target.closest("button[data-rel]");
      if (b) this._ssCardClick(b.dataset.rel);
    });
    on("#ss-view-close", () => {
      this._ssView = null;
      this._render();
    });
    on("#ss-view-sync", () => this._ssViewSync());
    const viewImg = $("#ss-view-img");
    if (viewImg && this._ssView) {
      const view = this._ssView;
      viewImg.onerror = () => {
        if (this._ssView !== view || view.failed) return;
        view.failed = true;
        view.note = view.shot.in_ha ? "Couldn't load HA's copy." : "Couldn't read it from the PS4 (is it on, with GoldHEN's FTP enabled?).";
        this._render();
      };
    }
    const d = this._ssDialog;
    if (!d) return;
    this.shadowRoot.querySelectorAll('input[name="ss-kind"]').forEach((r) => {
      r.onchange = () => {
        d.kind = r.value;
        this._render();
      };
    });
    const game = $("#ss-game");
    if (game) {
      game.onchange = () => {
        d.titleId = game.value;
        d.kind = "game";
        this._render();
      };
    }
    const count = $("#ss-count");
    if (count) {
      count.onchange = () => {
        d.count = Math.max(1, parseInt(count.value, 10) || 1);
        d.kind = "newest";
        this._render();
      };
    }
    on("#ss-cancel", () => {
      this._ssDialog = null;
      this._render();
    });
    on("#ss-go", () => {
      const scope = d.kind === "game" ? { kind: "game", title_id: d.titleId }
        : d.kind === "newest" ? { kind: "newest", count: d.count }
        : { kind: "all" };
      this._ssStartSync(scope);
    });
  }

  _renderKlog() {
    const connected = !!this._klogUnsub;
    const connecting = this._klogConnecting;
    return html`
      <div class="card">
        <h3>Live Klog</h3>
        <div class="row">
          <button class="btn" id="btn-klog-connect" ${(connected || connecting) ? "disabled" : ""}>Connect</button>
          <button class="btn" id="btn-klog-disconnect" ${(connected || connecting) ? "" : "disabled"}>Disconnect</button>
          <button class="btn" id="btn-klog-clear">Clear</button>
        </div>
        <div class="muted">${this._klogStatus || "Connecting..."}</div>
        <pre class="klog" id="klog-box"></pre>
      </div>
    `;
  }

  _bindFtpEvents() {
    const table = this.shadowRoot.querySelector("table");
    if (table) {
      table.onclick = (ev) => {
        const btn = ev.target.closest("button");
        const span = ev.target.closest("span.folder");

        if (span) {
          this._loadDir(span.dataset.path);
          return;
        }
        if (!btn) return;

        const action = btn.dataset.action;
        const path = btn.dataset.path;
        const name = btn.dataset.name;
        const isDir = btn.dataset.isdir === "true";

        if (action === "delete") this._deleteEntry({ name, path, is_dir: isDir });
        else if (action === "rename") this._renameEntry({ name, path, is_dir: isDir });
        else if (action === "edit") this._editEntry({ name, path, is_dir: isDir });
        else if (action === "download") this._downloadEntry({ name, path, is_dir: isDir });
      };
    }

    const root = this.shadowRoot.querySelector("#btn-root");
    const back = this.shadowRoot.querySelector("#btn-back");
    const refresh = this.shadowRoot.querySelector("#btn-refresh");
    if (root) root.onclick = () => this._loadDir("/");
    if (back) back.onclick = () => this._loadDir(this._path.split("/").slice(0, -1).join("/") || "/");
    if (refresh) refresh.onclick = () => this._loadDir();

    const btnSelect = this.shadowRoot.querySelector("#btn-ftp-select");
    const uploadInput = this.shadowRoot.querySelector("#ftp-upload-input");
    const btnUpload = this.shadowRoot.querySelector("#btn-ftp-upload");

    if (btnSelect && uploadInput) btnSelect.onclick = () => uploadInput.click();
    if (btnUpload) btnUpload.onclick = () => this._uploadFileToFtp();

    if (uploadInput && btnUpload) {
      uploadInput.onchange = () => {
        btnUpload.textContent = uploadInput.files.length
          ? `Upload: ${uploadInput.files[0].name}`
          : "Upload to current folder";
      };
    }
  }

  _renderEditor() {
    return html`
      <div class="editor-overlay">
        <div class="editor-container">
          <div style="margin-bottom:10px">
            <strong>Editing: ${this._escapeHtml(this._editing.name)}</strong><br>
            <small class="muted">${this._escapeHtml(this._editing.path)}</small>
          </div>
          <textarea id="editor-textarea" spellcheck="false">${this._escapeHtml(this._editing.content)}</textarea>
          <div class="editor-actions">
            <button id="btn-cancel">Cancel</button>
            <button id="btn-save" class="btn-save">Save to PS4</button>
          </div>
        </div>
      </div>
    `;
  }
}

customElements.define("ps4-goldhen-panel", PS4GoldHENPanel);
