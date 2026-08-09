/* Network Info — device table card.
 *
 * Vanilla light-DOM custom element, no build step. The shell (toolbar with
 * filter + settings, table container, footer) is built once; only the table
 * body and footer repaint on state updates, so typing in the filter is never
 * eaten by a re-render.
 *
 * Per-browser settings (visible columns + order, sort, grouping, offline
 * rows) persist in localStorage keyed by entity. Grouping by connection path
 * (LAN / 5 GHz / 2.4 GHz / ...) is only offered when the integration has
 * router access — without the router password the path is unknown, so the
 * option is shown disabled with an explanation.
 */

const COLUMNS = {
  name: { label: "Name" },
  ip: { label: "IP", mono: true, num: true },
  mac: { label: "MAC", mono: true },
  hostname: { label: "Hostname" },
  vendor: { label: "Vendor" },
  connection: { label: "Path" },
  signal: { label: "Signal", num: true },
  online: { label: "Online" },
  ha_device: { label: "HA device" },
  ha_area: { label: "Area" },
  router_name: { label: "Router name" },
  first_seen: { label: "First seen", date: true },
  last_seen: { label: "Last seen", date: true },
  sources: { label: "Seen by" },
};

const DEFAULT_COLUMNS = ["name", "ip", "mac", "connection", "signal", "ha_area", "online"];

// Render order of the connection groups when grouping is on.
const GROUP_ORDER = ["Router", "LAN", "5 GHz", "2.4 GHz", "Guest", "Wi-Fi", "Unknown"];

const BADGE_CLASS = {
  "Router": "b-router",
  "LAN": "b-lan",
  "5 GHz": "b-wifi5",
  "2.4 GHz": "b-wifi24",
  "Guest": "b-guest",
  "Wi-Fi": "b-wifi",
  "Unknown": "b-unknown",
};

const esc = (v) =>
  String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const ipNum = (ip) => {
  const m = String(ip || "").match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (!m) return Number.MAX_SAFE_INTEGER;
  return ((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4];
};

class NetworkInfoTable extends HTMLElement {
  setConfig(cfg) {
    if (!cfg) throw new Error("Invalid configuration");
    this._cfg = Object.assign(
      {
        entity: "sensor.network_info_devices",
        title: "Network devices",
        columns: null, // initial defaults; the settings sheet overrides per browser
        max_height: "70vh",
      },
      cfg
    );
    this._filter = "";
    this._built = false;
    this._sig = null;
    this._settings = this._defaultSettings();
    this._restore();
  }

  _defaultSettings() {
    const cols = Array.isArray(this._cfg.columns) && this._cfg.columns.length
      ? this._cfg.columns.filter((c) => COLUMNS[c])
      : DEFAULT_COLUMNS.slice();
    return { cols, sort: { key: "ip", dir: 1 }, group: false, offline: true };
  }

  _storeKey() { return `network-info-table:${this._cfg.entity}`; }

  _restore() {
    try {
      const raw = localStorage.getItem(this._storeKey());
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved && typeof saved === "object") {
        if (Array.isArray(saved.cols)) {
          const cols = saved.cols.filter((c) => COLUMNS[c]);
          if (cols.length) this._settings.cols = cols;
        }
        if (saved.sort && COLUMNS[saved.sort.key]) {
          this._settings.sort = { key: saved.sort.key, dir: saved.sort.dir === -1 ? -1 : 1 };
        }
        if (typeof saved.group === "boolean") this._settings.group = saved.group;
        if (typeof saved.offline === "boolean") this._settings.offline = saved.offline;
      }
    } catch (e) { /* corrupted storage — defaults stand */ }
  }

  _store() {
    try { localStorage.setItem(this._storeKey(), JSON.stringify(this._settings)); }
    catch (e) { /* storage full/blocked — settings just won't persist */ }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) { this._render(); this._paint(); return; }
    const attrs = this._attrs();
    const sig = attrs ? `${attrs.last_scan}|${attrs.router_available}|${(attrs.devices || []).length}` : "missing";
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
  }

  get hass() { return this._hass; }

  getCardSize() { return 8; }

  static getStubConfig() { return { entity: "sensor.network_info_devices" }; }

  // ── data ─────────────────────────────────────────────────
  _attrs() {
    const st = this._hass && this._hass.states[this._cfg.entity];
    return st ? st.attributes : null;
  }

  _routerConfigured() {
    const attrs = this._attrs();
    return !!attrs && attrs.router_available !== null && attrs.router_available !== undefined;
  }

  _devices() {
    const attrs = this._attrs();
    let list = (attrs && attrs.devices) || [];
    if (!this._settings.offline) list = list.filter((d) => d.online);
    const f = this._filter.trim().toLowerCase();
    if (f) {
      list = list.filter((d) => {
        const hay = [
          d.name, d.ip, d.mac, d.hostname, d.vendor, d.connection,
          d.router_name, d.ha_device, d.ha_area, (d.sources || []).join(" "),
        ].join(" ").toLowerCase();
        return hay.includes(f);
      });
    }
    const { key, dir } = this._settings.sort;
    const sorted = list.slice().sort((a, b) => this._cmp(a, b, key) * dir);
    return sorted;
  }

  _cmp(a, b, key) {
    if (key === "ip") return ipNum(a.ip) - ipNum(b.ip);
    if (COLUMNS[key] && COLUMNS[key].date) {
      const x = a[key] ? Date.parse(a[key]) : -Infinity;
      const y = b[key] ? Date.parse(b[key]) : -Infinity;
      return x - y;
    }
    if (key === "signal") {
      const x = a.signal == null ? -Infinity : Number(a.signal);
      const y = b.signal == null ? -Infinity : Number(b.signal);
      return x - y;
    }
    if (key === "online") return (a.online ? 1 : 0) - (b.online ? 1 : 0);
    if (key === "sources") return (a.sources || []).length - (b.sources || []).length;
    const x = String(a[key] == null ? "" : a[key]).toLowerCase();
    const y = String(b[key] == null ? "" : b[key]).toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  }

  // ── shell ────────────────────────────────────────────────
  _render() {
    this.innerHTML = `
      <ha-card>
        <style>
          .nit-wrap { padding: 12px 16px 8px; }
          .nit-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
          .nit-title { font-size: 1.15em; font-weight: 500; margin-right: auto; }
          .nit-filter { min-width: 140px; flex: 0 1 220px; padding: 6px 10px;
            border: 1px solid var(--divider-color, #444); border-radius: 8px;
            background: var(--card-background-color); color: var(--primary-text-color); }
          .nit-gear, .nit-refresh { cursor: pointer; border: none; background: none;
            font-size: 1.15em; color: var(--secondary-text-color); padding: 4px; }
          .nit-refresh.spin { animation: nit-rot 1s linear infinite; }
          @keyframes nit-rot { to { transform: rotate(360deg); } }
          .nit-scroll { overflow: auto; max-height: ${esc(this._cfg.max_height)}; }
          table.nit { border-collapse: collapse; width: 100%; font-size: 0.92em; }
          .nit th { position: sticky; top: 0; z-index: 1; text-align: left; white-space: nowrap;
            background: var(--card-background-color); color: var(--secondary-text-color);
            font-weight: 500; padding: 6px 10px 6px 0; cursor: pointer; user-select: none;
            border-bottom: 1px solid var(--divider-color, #444); }
          .nit td { padding: 5px 10px 5px 0; white-space: nowrap;
            border-bottom: 1px solid color-mix(in srgb, var(--divider-color, #444) 40%, transparent); }
          .nit tr.off td { opacity: 0.45; }
          .nit tr.grp td { padding: 8px 0 4px; font-weight: 600; border-bottom: none;
            color: var(--primary-text-color); }
          .nit .mono { font-family: ui-monospace, monospace; font-size: 0.95em; }
          .nit .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.88em; }
          .b-router { background: rgba(121, 134, 203, 0.22); color: #9fa8da; }
          .b-lan    { background: rgba(33, 150, 243, 0.18); color: #64b5f6; }
          .b-wifi5  { background: rgba(76, 175, 80, 0.18);  color: #81c784; }
          .b-wifi24 { background: rgba(255, 152, 0, 0.20);  color: #ffb74d; }
          .b-guest  { background: rgba(156, 39, 176, 0.18); color: #ba68c8; }
          .b-wifi   { background: rgba(0, 188, 212, 0.18);  color: #4dd0e1; }
          .b-unknown{ background: rgba(158, 158, 158, 0.18); color: var(--secondary-text-color); }
          .nit .dot { font-size: 0.8em; }
          .nit .on  { color: #66bb6a; }
          .nit .offd{ color: var(--secondary-text-color); }
          .nit-foot { padding: 8px 0 4px; color: var(--secondary-text-color); font-size: 0.85em; }
          .nit-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 8;
            display: none; align-items: center; justify-content: center; }
          .nit-overlay.open { display: flex; }
          .nit-sheet { background: var(--card-background-color); color: var(--primary-text-color);
            border-radius: 12px; padding: 18px 20px; min-width: 260px; max-width: 92vw;
            max-height: 85vh; overflow: auto; box-shadow: 0 6px 30px rgba(0,0,0,0.5); }
          .nit-sheet h3 { margin: 0 0 10px; font-size: 1.05em; }
          .nit-sheet .sect { margin: 12px 0 4px; font-weight: 500; color: var(--secondary-text-color);
            font-size: 0.88em; }
          .nit-col { display: flex; align-items: center; gap: 6px; padding: 2px 0; }
          .nit-col button { border: none; background: none; cursor: pointer;
            color: var(--secondary-text-color); padding: 0 3px; }
          .nit-col button:disabled { opacity: 0.25; cursor: default; }
          .nit-toggle { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
          .nit-note { font-size: 0.82em; color: var(--secondary-text-color); margin: 2px 0 0 24px; }
          .nit-actions { display: flex; justify-content: space-between; margin-top: 14px; }
          .nit-actions button { cursor: pointer; border: 1px solid var(--divider-color, #444);
            background: none; color: var(--primary-text-color); border-radius: 8px; padding: 6px 14px; }
        </style>
        <div class="nit-wrap">
          <div class="nit-bar">
            <span class="nit-title">${esc(this._cfg.title)}</span>
            <input class="nit-filter" type="text" placeholder="Filter…">
            <button class="nit-refresh" title="Scan now">↻</button>
            <button class="nit-gear" title="Table settings">⚙</button>
          </div>
          <div class="nit-scroll"><table class="nit"><thead></thead><tbody></tbody></table></div>
          <div class="nit-foot"></div>
        </div>
        <div class="nit-overlay"><div class="nit-sheet"></div></div>
      </ha-card>`;

    this._els = {
      filter: this.querySelector(".nit-filter"),
      refresh: this.querySelector(".nit-refresh"),
      gear: this.querySelector(".nit-gear"),
      thead: this.querySelector("thead"),
      tbody: this.querySelector("tbody"),
      foot: this.querySelector(".nit-foot"),
      overlay: this.querySelector(".nit-overlay"),
      sheet: this.querySelector(".nit-sheet"),
    };

    this._els.filter.addEventListener("input", () => {
      this._filter = this._els.filter.value;
      this._paint();
    });
    this._els.refresh.addEventListener("click", () => {
      if (!this._hass) return;
      // update_entity on a coordinator entity triggers a full refresh cycle
      // (scan + router poll). The spin clears when the new data paints.
      this._els.refresh.classList.add("spin");
      this._hass.callService("homeassistant", "update_entity", {
        entity_id: [this._cfg.entity],
      });
    });
    this._els.gear.addEventListener("click", () => this._openSettings());
    this._els.overlay.addEventListener("click", (ev) => {
      if (ev.target === this._els.overlay) this._closeSettings();
    });
    this._els.thead.addEventListener("click", (ev) => {
      const th = ev.target.closest("th[data-key]");
      if (!th) return;
      const key = th.dataset.key;
      const sort = this._settings.sort;
      if (sort.key === key) sort.dir = -sort.dir;
      else this._settings.sort = { key, dir: 1 };
      this._store();
      this._paint();
    });

    this._built = true;
  }

  // ── table ────────────────────────────────────────────────
  _paint() {
    if (!this._built) return;
    this._els.refresh.classList.remove("spin");
    const attrs = this._attrs();
    if (!attrs) {
      this._els.tbody.innerHTML = `<tr><td>Entity ${esc(this._cfg.entity)} not found</td></tr>`;
      this._els.thead.innerHTML = "";
      this._els.foot.textContent = "";
      return;
    }

    const cols = this._settings.cols;
    const { key: sortKey, dir } = this._settings.sort;
    this._els.thead.innerHTML =
      "<tr>" +
      cols
        .map((c) => {
          const arrow = c === sortKey ? (dir === 1 ? " ↑" : " ↓") : "";
          return `<th data-key="${c}">${esc(COLUMNS[c].label)}${arrow}</th>`;
        })
        .join("") +
      "</tr>";

    const devices = this._devices();
    const grouping = this._settings.group && this._routerConfigured();

    let rows = "";
    if (grouping) {
      const groups = new Map();
      for (const d of devices) {
        const g = d.connection || "Unknown";
        if (!groups.has(g)) groups.set(g, []);
        groups.get(g).push(d);
      }
      const order = GROUP_ORDER.filter((g) => groups.has(g))
        .concat([...groups.keys()].filter((g) => !GROUP_ORDER.includes(g)));
      for (const g of order) {
        const members = groups.get(g);
        rows += `<tr class="grp"><td colspan="${cols.length}">` +
          `<span class="badge ${BADGE_CLASS[g] || "b-unknown"}">${esc(g)}</span>` +
          ` &nbsp;${members.length} device${members.length === 1 ? "" : "s"}</td></tr>`;
        rows += members.map((d) => this._row(d, cols)).join("");
      }
    } else {
      rows = devices.map((d) => this._row(d, cols)).join("");
    }
    this._els.tbody.innerHTML =
      rows || `<tr><td colspan="${cols.length}">No devices</td></tr>`;

    const all = (attrs.devices || []).length;
    const counts = attrs.counts || {};
    const parts = [`${devices.length} of ${all} devices`, `${counts.online ?? "?"} online`];
    if (attrs.router_available === false) parts.push("router unreachable");
    if (attrs.last_scan) {
      const t = new Date(attrs.last_scan);
      if (!isNaN(t)) parts.push(`scanned ${t.toLocaleTimeString()}`);
    }
    this._els.foot.textContent = parts.join(" · ");
  }

  _row(d, cols) {
    const cells = cols.map((c) => {
      switch (c) {
        case "name":
          return `<td><strong>${esc(d.name)}</strong></td>`;
        case "ip":
        case "mac":
          return `<td class="mono">${esc(d[c])}</td>`;
        case "connection": {
          const conn = d.connection || "Unknown";
          return `<td><span class="badge ${BADGE_CLASS[conn] || "b-unknown"}">${esc(conn)}</span></td>`;
        }
        case "online":
          return d.online
            ? `<td><span class="dot on">●</span></td>`
            : `<td><span class="dot offd">○</span></td>`;
        case "sources":
          return `<td>${esc((d.sources || []).join(", "))}</td>`;
        case "first_seen":
        case "last_seen": {
          if (!d[c]) return "<td></td>";
          const t = new Date(d[c]);
          return `<td>${isNaN(t) ? "" : esc(t.toLocaleString([], { dateStyle: "short", timeStyle: "short" }))}</td>`;
        }
        default:
          return `<td>${esc(d[c])}</td>`;
      }
    });
    return `<tr${d.online ? "" : ' class="off"'}>${cells.join("")}</tr>`;
  }

  // ── settings sheet ───────────────────────────────────────
  _openSettings() {
    this._paintSettings();
    this._els.overlay.classList.add("open");
  }

  _closeSettings() { this._els.overlay.classList.remove("open"); }

  _paintSettings() {
    const cols = this._settings.cols;
    const hidden = Object.keys(COLUMNS).filter((c) => !cols.includes(c));
    const routerOk = this._routerConfigured();

    const colRow = (c, i, shown) => `
      <div class="nit-col">
        <input type="checkbox" id="nit-c-${c}" data-col="${c}" ${shown ? "checked" : ""}>
        <label for="nit-c-${c}" style="flex:1">${esc(COLUMNS[c].label)}</label>
        <button data-up="${c}" ${!shown || i === 0 ? "disabled" : ""}>▲</button>
        <button data-down="${c}" ${!shown || i === cols.length - 1 ? "disabled" : ""}>▼</button>
      </div>`;

    this._els.sheet.innerHTML = `
      <h3>Table settings</h3>
      <div class="nit-toggle">
        <input type="checkbox" id="nit-group" ${this._settings.group && routerOk ? "checked" : ""}
          ${routerOk ? "" : "disabled"}>
        <label for="nit-group">Group into 2.4 GHz / 5 GHz / LAN tables</label>
      </div>
      ${routerOk ? "" : `<div class="nit-note">Needs router access — set the router admin password in the integration options.</div>`}
      <div class="nit-toggle">
        <input type="checkbox" id="nit-offline" ${this._settings.offline ? "checked" : ""}>
        <label for="nit-offline">Show offline devices</label>
      </div>
      <div class="sect">Columns</div>
      ${cols.map((c, i) => colRow(c, i, true)).join("")}
      ${hidden.map((c) => colRow(c, -1, false)).join("")}
      <div class="nit-actions">
        <button id="nit-reset">Reset</button>
        <button id="nit-close">Close</button>
      </div>`;

    const sheet = this._els.sheet;
    sheet.querySelector("#nit-close").addEventListener("click", () => this._closeSettings());
    sheet.querySelector("#nit-reset").addEventListener("click", () => {
      this._settings = this._defaultSettings();
      this._store();
      this._paintSettings();
      this._paint();
    });
    const group = sheet.querySelector("#nit-group");
    if (group) group.addEventListener("change", () => {
      this._settings.group = group.checked;
      this._store();
      this._paint();
    });
    sheet.querySelector("#nit-offline").addEventListener("change", (ev) => {
      this._settings.offline = ev.target.checked;
      this._store();
      this._paint();
    });
    sheet.querySelectorAll("input[data-col]").forEach((box) => {
      box.addEventListener("change", () => {
        const c = box.dataset.col;
        if (box.checked) {
          if (!this._settings.cols.includes(c)) this._settings.cols.push(c);
        } else {
          this._settings.cols = this._settings.cols.filter((x) => x !== c);
          if (!this._settings.cols.length) this._settings.cols = ["name"];
        }
        this._store();
        this._paintSettings();
        this._paint();
      });
    });
    sheet.querySelectorAll("button[data-up],button[data-down]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const c = btn.dataset.up || btn.dataset.down;
        const list = this._settings.cols;
        const i = list.indexOf(c);
        const j = btn.dataset.up ? i - 1 : i + 1;
        if (i < 0 || j < 0 || j >= list.length) return;
        [list[i], list[j]] = [list[j], list[i]];
        this._store();
        this._paintSettings();
        this._paint();
      });
    });
  }
}

customElements.define("network-info-table", NetworkInfoTable);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "network-info-table",
  name: "Network Info Table",
  description:
    "Device table for the Network Info integration — filterable, configurable columns, optional grouping by connection path.",
});
