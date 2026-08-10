/* Network Info — external IP change log card.
 *
 * Same vanilla light-DOM pattern as the device table: shell built once,
 * only the table body and footer repaint, so the filter input never loses
 * focus or content. Filter, click-to-sort, pagination; the settings sheet
 * holds the default page size and default sort, persisted per browser in
 * localStorage keyed by entity.
 */

const LOG_COLUMNS = {
  date: { label: "Date" },
  ip: { label: "IP address" },
};

const PAGE_SIZES = [10, 25, 50, 100];

const SORTS = [
  { key: "date", dir: -1, label: "Date — newest first" },
  { key: "date", dir: 1, label: "Date — oldest first" },
  { key: "ip", dir: 1, label: "IP — ascending" },
  { key: "ip", dir: -1, label: "IP — descending" },
];

const escIp = (v) =>
  String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

const ipSortNum = (ip) => {
  const m = String(ip || "").match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (!m) return Number.MAX_SAFE_INTEGER;
  return ((+m[1] * 256 + +m[2]) * 256 + +m[3]) * 256 + +m[4];
};

class NetworkInfoIpLog extends HTMLElement {
  setConfig(cfg) {
    if (!cfg) throw new Error("Invalid configuration");
    this._cfg = Object.assign(
      {
        entity: "sensor.network_info_external_ip_log",
        title: "External IP log",
        page_size: 10,
        sort: "date_desc", // date_desc | date_asc | ip_asc | ip_desc
      },
      cfg
    );
    this._filter = "";
    this._page = 0;
    this._built = false;
    this._sig = null;
    this._settings = this._defaultSettings();
    this._restore();
  }

  _defaultSettings() {
    const bySlug = {
      date_desc: { key: "date", dir: -1 },
      date_asc: { key: "date", dir: 1 },
      ip_asc: { key: "ip", dir: 1 },
      ip_desc: { key: "ip", dir: -1 },
    };
    return {
      pageSize: PAGE_SIZES.includes(Number(this._cfg.page_size))
        ? Number(this._cfg.page_size)
        : 10,
      sort: bySlug[this._cfg.sort] || { key: "date", dir: -1 },
    };
  }

  _storeKey() { return `network-info-ip-log:${this._cfg.entity}`; }

  _restore() {
    try {
      const raw = localStorage.getItem(this._storeKey());
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved && typeof saved === "object") {
        if (PAGE_SIZES.includes(Number(saved.pageSize))) {
          this._settings.pageSize = Number(saved.pageSize);
        }
        if (saved.sort && LOG_COLUMNS[saved.sort.key]) {
          this._settings.sort = {
            key: saved.sort.key,
            dir: saved.sort.dir === -1 ? -1 : 1,
          };
        }
      }
    } catch (e) { /* corrupted storage — defaults stand */ }
  }

  _store() {
    try { localStorage.setItem(this._storeKey(), JSON.stringify(this._settings)); }
    catch (e) { /* storage full/blocked */ }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) { this._render(); this._paint(); return; }
    const st = hass.states[this._cfg.entity];
    const rows = (st && st.attributes && st.attributes.log) || [];
    const sig = st
      ? `${st.state}|${rows.length}|${rows.length ? rows[rows.length - 1].date : ""}`
      : "missing";
    if (sig !== this._sig) { this._sig = sig; this._paint(); }
  }

  get hass() { return this._hass; }

  getCardSize() { return 5; }

  static getStubConfig() { return { entity: "sensor.network_info_external_ip_log" }; }

  _rows() {
    const st = this._hass && this._hass.states[this._cfg.entity];
    let rows = (st && st.attributes && st.attributes.log) || [];
    const f = this._filter.trim().toLowerCase();
    if (f) {
      rows = rows.filter((r) =>
        `${r.date} ${r.ip}`.toLowerCase().includes(f)
      );
    }
    const { key, dir } = this._settings.sort;
    return rows.slice().sort((a, b) => {
      if (key === "ip") return (ipSortNum(a.ip) - ipSortNum(b.ip)) * dir;
      // "YYYY-MM-DD HH:MM:SS" sorts correctly as a string.
      const x = String(a.date || ""), y = String(b.date || "");
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }

  _render() {
    this.innerHTML = `
      <ha-card>
        <style>
          .nil-wrap { padding: 12px 16px 8px; }
          .nil-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
          .nil-title { font-size: 1.15em; font-weight: 500; margin-right: auto; }
          .nil-filter { min-width: 120px; flex: 0 1 180px; padding: 6px 10px;
            border: 1px solid var(--divider-color, #444); border-radius: 8px;
            background: var(--card-background-color); color: var(--primary-text-color); }
          .nil-gear { cursor: pointer; border: none; background: none; font-size: 1.15em;
            color: var(--secondary-text-color); padding: 4px; }
          table.nil { border-collapse: collapse; width: 100%; font-size: 0.92em; }
          .nil th { text-align: left; white-space: nowrap; font-weight: 500;
            color: var(--secondary-text-color); padding: 6px 10px 6px 0;
            cursor: pointer; user-select: none;
            border-bottom: 1px solid var(--divider-color, #444); }
          .nil td { padding: 5px 10px 5px 0; white-space: nowrap;
            font-family: ui-monospace, monospace; font-size: 0.95em;
            border-bottom: 1px solid color-mix(in srgb, var(--divider-color, #444) 40%, transparent);
            /* The Home Assistant shell disables selection app-wide; cell text
               has to ask for it back, or an address cannot be copied. */
            -webkit-user-select: text; user-select: text; cursor: text; }
          .nil tr.current td { font-weight: 600;
            background: rgba(76, 175, 80, 0.10); }
          .nil .cur-pill { display: inline-block; margin-left: 8px; padding: 0 8px;
            border-radius: 10px; font-size: 0.82em; font-weight: 500;
            font-family: var(--paper-font-body1_-_font-family, inherit);
            background: rgba(76, 175, 80, 0.22); color: #81c784; }
          .nil-foot { display: flex; align-items: center; gap: 10px; padding: 8px 0 4px;
            color: var(--secondary-text-color); font-size: 0.85em; }
          .nil-foot .info { margin-right: auto; }
          .nil-foot button { cursor: pointer; border: 1px solid var(--divider-color, #444);
            background: none; color: var(--primary-text-color); border-radius: 6px;
            padding: 2px 10px; }
          .nil-foot button:disabled { opacity: 0.3; cursor: default; }
          .nil-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 8;
            display: none; align-items: center; justify-content: center; }
          .nil-overlay.open { display: flex; }
          .nil-sheet { background: var(--card-background-color); color: var(--primary-text-color);
            border-radius: 12px; padding: 18px 20px; min-width: 250px; max-width: 92vw;
            box-shadow: 0 6px 30px rgba(0,0,0,0.5); }
          .nil-sheet h3 { margin: 0 0 12px; font-size: 1.05em; }
          .nil-sheet label { display: block; margin: 10px 0 4px; font-size: 0.88em;
            color: var(--secondary-text-color); }
          .nil-sheet select { width: 100%; padding: 6px 8px; border-radius: 8px;
            border: 1px solid var(--divider-color, #444);
            background: var(--card-background-color); color: var(--primary-text-color); }
          .nil-actions { display: flex; justify-content: space-between; margin-top: 16px; }
          .nil-actions button { cursor: pointer; border: 1px solid var(--divider-color, #444);
            background: none; color: var(--primary-text-color); border-radius: 8px; padding: 6px 14px; }
        </style>
        <div class="nil-wrap">
          <div class="nil-bar">
            <span class="nil-title">${escIp(this._cfg.title)}</span>
            <input class="nil-filter" type="text" placeholder="Filter…">
            <button class="nil-gear" title="Table settings">⚙</button>
          </div>
          <table class="nil"><thead></thead><tbody></tbody></table>
          <div class="nil-foot">
            <span class="info"></span>
            <button class="prev">‹</button>
            <span class="page"></span>
            <button class="next">›</button>
          </div>
        </div>
        <div class="nil-overlay"><div class="nil-sheet"></div></div>
      </ha-card>`;

    this._els = {
      filter: this.querySelector(".nil-filter"),
      gear: this.querySelector(".nil-gear"),
      thead: this.querySelector("thead"),
      tbody: this.querySelector("tbody"),
      info: this.querySelector(".nil-foot .info"),
      page: this.querySelector(".nil-foot .page"),
      prev: this.querySelector(".nil-foot .prev"),
      next: this.querySelector(".nil-foot .next"),
      overlay: this.querySelector(".nil-overlay"),
      sheet: this.querySelector(".nil-sheet"),
    };

    this._els.filter.addEventListener("input", () => {
      this._filter = this._els.filter.value;
      this._page = 0;
      this._paint();
    });
    this._els.gear.addEventListener("click", () => this._openSettings());
    this._els.overlay.addEventListener("click", (ev) => {
      if (ev.target === this._els.overlay) this._els.overlay.classList.remove("open");
    });
    this._els.prev.addEventListener("click", () => { this._page--; this._paint(); });
    this._els.next.addEventListener("click", () => { this._page++; this._paint(); });
    this._els.thead.addEventListener("click", (ev) => {
      const th = ev.target.closest("th[data-key]");
      if (!th) return;
      const key = th.dataset.key;
      const sort = this._settings.sort;
      if (sort.key === key) sort.dir = -sort.dir;
      else this._settings.sort = { key, dir: key === "date" ? -1 : 1 };
      this._store();
      this._paint();
    });

    this._built = true;
  }

  _paint() {
    if (!this._built) return;
    const st = this._hass && this._hass.states[this._cfg.entity];
    if (!st) {
      this._els.thead.innerHTML = "";
      this._els.tbody.innerHTML =
        `<tr><td>Entity ${escIp(this._cfg.entity)} not found</td></tr>`;
      this._els.info.textContent = "";
      this._els.page.textContent = "";
      this._els.prev.disabled = this._els.next.disabled = true;
      return;
    }

    const { key: sortKey, dir } = this._settings.sort;
    this._els.thead.innerHTML =
      "<tr>" +
      Object.keys(LOG_COLUMNS)
        .map((c) => {
          const arrow = c === sortKey ? (dir === 1 ? " ↑" : " ↓") : "";
          return `<th data-key="${c}">${escIp(LOG_COLUMNS[c].label)}${arrow}</th>`;
        })
        .join("") +
      "</tr>";

    const rows = this._rows();
    const log = (st.attributes && st.attributes.log) || [];
    const all = log.length;
    const size = this._settings.pageSize;
    const pages = Math.max(1, Math.ceil(rows.length / size));
    this._page = Math.min(Math.max(this._page, 0), pages - 1);
    const start = this._page * size;
    const slice = rows.slice(start, start + size);
    // The IP in use right now; the newest log row is the fallback for the
    // moments between a change landing in the log and the sensor updating.
    const currentIp =
      (st.attributes && st.attributes.external_ip) ||
      (all ? log[all - 1].ip : null);
    const newest = all ? log[all - 1] : null;

    this._els.tbody.innerHTML = slice.length
      ? slice
          .map((r) => {
            const isCurrent = currentIp && r.ip === currentIp;
            const isNewest = newest && r.date === newest.date && r.ip === newest.ip;
            const pill = isCurrent && isNewest
              ? '<span class="cur-pill">current</span>' : "";
            return `<tr${isCurrent ? ' class="current"' : ""}><td>${escIp(r.date)}</td><td>${escIp(r.ip)}${pill}</td></tr>`;
          })
          .join("")
      : `<tr><td colspan="2">No entries</td></tr>`;

    const shownTo = Math.min(start + size, rows.length);
    this._els.info.textContent = rows.length
      ? `${start + 1}–${shownTo} of ${rows.length}` +
        (rows.length !== all ? ` (${all} total)` : "")
      : `0 of ${all}`;
    this._els.page.textContent = `${this._page + 1} / ${pages}`;
    this._els.prev.disabled = this._page === 0;
    this._els.next.disabled = this._page >= pages - 1;
  }

  _openSettings() {
    const cur = this._settings;
    const sortIndex = SORTS.findIndex(
      (s) => s.key === cur.sort.key && s.dir === cur.sort.dir
    );
    this._els.sheet.innerHTML = `
      <h3>Table settings</h3>
      <label for="nil-size">Default page size</label>
      <select id="nil-size">
        ${PAGE_SIZES.map((n) =>
          `<option value="${n}"${n === cur.pageSize ? " selected" : ""}>${n}</option>`
        ).join("")}
      </select>
      <label for="nil-sort">Default sort</label>
      <select id="nil-sort">
        ${SORTS.map((s, i) =>
          `<option value="${i}"${i === sortIndex ? " selected" : ""}>${escIp(s.label)}</option>`
        ).join("")}
      </select>
      <div class="nil-actions">
        <button id="nil-reset">Reset</button>
        <button id="nil-close">Close</button>
      </div>`;

    const sheet = this._els.sheet;
    sheet.querySelector("#nil-size").addEventListener("change", (ev) => {
      this._settings.pageSize = Number(ev.target.value);
      this._page = 0;
      this._store();
      this._paint();
    });
    sheet.querySelector("#nil-sort").addEventListener("change", (ev) => {
      const s = SORTS[Number(ev.target.value)] || SORTS[0];
      this._settings.sort = { key: s.key, dir: s.dir };
      this._store();
      this._paint();
    });
    sheet.querySelector("#nil-reset").addEventListener("click", () => {
      this._settings = this._defaultSettings();
      this._page = 0;
      this._store();
      this._openSettings();
      this._paint();
    });
    sheet.querySelector("#nil-close").addEventListener("click", () =>
      this._els.overlay.classList.remove("open")
    );
    this._els.overlay.classList.add("open");
  }
}

customElements.define("network-info-ip-log", NetworkInfoIpLog);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "network-info-ip-log",
  name: "Network Info IP Log",
  description:
    "External IP change history from the Network Info integration — filterable, sortable, paginated.",
});
