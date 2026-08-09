/*
 * WLED Gateway card — live preview as a native Lovelace card.
 *
 * An iframe pointing at an Ingress URL can't authenticate itself: Supervisor
 * requires an `ingress_session` cookie, only Home Assistant's frontend can mint
 * one, and the cookie belongs to a single origin. So when the Companion app
 * switches between your external URL and the local IP, every card 401s until
 * you open the add-on panel by hand.
 *
 * This card runs inside the frontend instead, so it can create that session
 * itself (and keep it alive), then stream the preview straight into a canvas —
 * no iframe. Switching networks just re-mints for the new origin.
 *
 * It also looks up the add-on's Ingress URL, so no token is hardcoded and
 * reinstalling the add-on doesn't break your cards.
 *
 * Install:
 *   1. copy this file to  <config>/www/wled-gateway-card.js
 *   2. Settings -> Dashboards -> Resources -> Add resource
 *        URL:  /local/wled-gateway-card.js
 *        Type: JavaScript module
 *   3. add a card:
 *        type: custom:wled-gateway-card
 *        addon: abcd1234_wled_gateway     # slug, see below
 *        device: "1"
 *
 * The add-on slug is in the URL of its page in Home Assistant:
 *   /hassio/addon/<slug>/info
 */

const CARD_VERSION = "1.0.0";

/* ------------------------------------------------------------------ *
 * Ingress session, shared by every card on the dashboard so a page of
 * previews mints one session rather than one each.
 * ------------------------------------------------------------------ */

let sessionPromise = null;
let keepAliveTimer = null;
const KEEP_ALIVE_MS = 60000;

function setIngressCookie(session) {
  // Same attributes the frontend uses; Secure only over https, or the browser
  // would refuse to send it back on a plain-http local connection.
  document.cookie =
    `ingress_session=${session};path=/api/hassio_ingress/;SameSite=Strict` +
    (location.protocol === "https:" ? ";Secure" : "");
}

function ensureSession(hass) {
  if (sessionPromise) return sessionPromise;

  sessionPromise = hass
    .callWS({ type: "supervisor/api", endpoint: "/ingress/session", method: "post" })
    .then((result) => {
      const session = result.session;
      setIngressCookie(session);

      if (keepAliveTimer) clearInterval(keepAliveTimer);
      keepAliveTimer = setInterval(() => {
        hass
          .callWS({
            type: "supervisor/api",
            endpoint: "/ingress/validate_session",
            method: "post",
            data: { session },
          })
          .catch(() => {
            // Session died (expired, or Supervisor restarted). Drop it so the
            // next render mints a fresh one instead of retrying a dead token.
            clearInterval(keepAliveTimer);
            keepAliveTimer = null;
            sessionPromise = null;
          });
      }, KEEP_ALIVE_MS);

      return session;
    })
    .catch((err) => {
      sessionPromise = null;
      throw err;
    });

  return sessionPromise;
}

/* ------------------------------------------------------------------ *
 * Ingress path lookup, cached per add-on slug.
 * ------------------------------------------------------------------ */

const ingressPaths = {};

function resolveIngressPath(hass, slug) {
  if (ingressPaths[slug]) return ingressPaths[slug];

  ingressPaths[slug] = hass
    .callWS({ type: "supervisor/api", endpoint: `/addons/${slug}/info`, method: "get" })
    .then((info) => {
      if (!info || !info.ingress_url) {
        throw new Error(`add-on "${slug}" has no ingress URL — is Ingress enabled?`);
      }
      return info.ingress_url.replace(/\/$/, "");
    })
    .catch((err) => {
      delete ingressPaths[slug];
      throw err;
    });

  return ingressPaths[slug];
}

/* ------------------------------------------------------------------ *
 * The card
 * ------------------------------------------------------------------ */

class WledGatewayCard extends HTMLElement {
  constructor() {
    super();
    this._ws = null;
    this._retry = null;
    this._started = false;

    // Brightness handling, mirroring the add-on's own preview pages: the live
    // view can arrive scaled by the device's brightness, so it's scaled back
    // up — but never beyond what the frame data itself justifies, or a feed
    // that already reaches full scale would bleach out.
    this._deviceBri = 255;
    this._peak = 255;
    this._peakSetAt = 0;
    this._factor = 1.75;

    this.attachShadow({ mode: "open" });
  }

  static getStubConfig() {
    return { addon: "", device: "1" };
  }

  setConfig(config) {
    if (!config.addon && !config.ingress_path) {
      throw new Error('Set "addon" to the add-on slug (see /hassio/addon/<slug>/info), or "ingress_path".');
    }
    this._config = {
      device: "1",
      view: "auto", // auto | strip | matrix
      rotate: 0,
      height: null, // css length; defaults per view
      normalize: true,
      bright: 0, // fixed percentage; 0 = follow the device
      gain: 1,
      title: null,
      ...config,
    };
    this._config.device = String(this._config.device);
    this._render();
    if (this._hass) this._start();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this._config) this._start();
  }

  getCardSize() {
    return this._config && this._config.view === "matrix" ? 4 : 1;
  }

  connectedCallback() {
    if (this._config && this._hass) this._start();
  }

  disconnectedCallback() {
    this._stop();
  }

  /* ---------------- rendering ---------------- */

  _render() {
    const cfg = this._config;
    const isMatrix = cfg.view === "matrix";
    const height = cfg.height || (isMatrix ? "180px" : "40px");

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .wrap { position: relative; background: #000; height: ${height}; }
        canvas { display: block; width: 100%; height: 100%; }
        .msg {
          position: absolute; inset: 0; display: flex;
          align-items: center; justify-content: center;
          color: var(--secondary-text-color); font-size: 0.85em;
          text-align: center; padding: 0 8px; background: var(--card-background-color);
        }
        .hidden { display: none; }
      </style>
      <ha-card ${cfg.title ? `header="${cfg.title}"` : ""}>
        <div class="wrap">
          <canvas></canvas>
          <div class="msg">connecting…</div>
        </div>
      </ha-card>
    `;
    this._canvas = this.shadowRoot.querySelector("canvas");
    this._msg = this.shadowRoot.querySelector(".msg");

    if (cfg.rotate) {
      this._canvas.style.transform = `rotate(${cfg.rotate}deg)`;
      this._canvas.style.transformOrigin = "center center";
    }
  }

  _status(text) {
    if (!this._msg) return;
    if (text) {
      this._msg.textContent = text;
      this._msg.classList.remove("hidden");
    } else {
      this._msg.classList.add("hidden");
    }
  }

  /* ---------------- brightness ---------------- */

  _notePeak(frameMax) {
    const now = Date.now();
    if (frameMax >= this._peak || now - this._peakSetAt > 2000) {
      this._peak = frameMax;
      this._peakSetAt = now;
    }
  }

  _recomputeFactor() {
    const cfg = this._config;
    const MAX = 24;
    if (cfg.bright > 0) {
      this._factor = Math.min(MAX, (cfg.bright / 100) * cfg.gain);
      return;
    }
    if (!cfg.normalize) {
      this._factor = 1.75 * cfg.gain;
      return;
    }
    const byBrightness = 255 / Math.max(this._deviceBri, 1);
    const byData = 255 / Math.max(this._peak, 1);
    this._factor = Math.min(MAX, 1.75 * cfg.gain * Math.min(byBrightness, byData));
  }

  _scale(r, g, b) {
    const f = this._factor;
    if (f === 1) return [r, g, b];
    const mx = Math.max(r, g, b);
    if (mx <= 2) return [0, 0, 0]; // quantisation remnants, not light
    const s = mx * f > 255 ? 255 / mx : f;
    return [Math.round(r * s), Math.round(g * s), Math.round(b * s)];
  }

  /* ---------------- streaming ---------------- */

  async _start() {
    if (this._started || !this.isConnected) return;
    this._started = true;
    try {
      const [session, path] = await Promise.all([
        ensureSession(this._hass),
        this._config.ingress_path
          ? Promise.resolve(this._config.ingress_path.replace(/\/$/, ""))
          : resolveIngressPath(this._hass, this._config.addon),
      ]);
      void session; // needed for its cookie side effect only
      this._path = path;
      this._connect();
    } catch (err) {
      this._started = false;
      this._status(`Cannot reach the add-on: ${err && err.message ? err.message : err}`);
      this._retryLater();
    }
  }

  _connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}${this._path}/ws/${this._config.device}`;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._status(`Cannot open preview: ${err.message}`);
      return this._retryLater();
    }
    this._ws = ws;
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      this._status(null);
      ws.send("{'lv':true}");
    };
    ws.onmessage = (event) => this._onMessage(event);
    ws.onclose = () => {
      if (this._ws === ws) {
        this._ws = null;
        this._status("reconnecting…");
        this._retryLater();
      }
    };
    ws.onerror = () => ws.close();
  }

  _retryLater() {
    if (this._retry) return;
    this._retry = setTimeout(() => {
      this._retry = null;
      this._started = false;
      this._start();
    }, 3000);
  }

  _stop() {
    this._started = false;
    if (this._retry) {
      clearTimeout(this._retry);
      this._retry = null;
    }
    if (this._ws) {
      const ws = this._ws;
      this._ws = null;
      ws.close();
    }
  }

  _onMessage(event) {
    // The gateway forwards the device's brightness as a small JSON message.
    if (typeof event.data === "string") {
      try {
        const msg = JSON.parse(event.data);
        if (typeof msg.bri === "number") {
          this._deviceBri = msg.bri;
          this._recomputeFactor();
        }
      } catch (err) {
        /* not a message we care about */
      }
      return;
    }
    if (Object.prototype.toString.call(event.data) !== "[object ArrayBuffer]") return;

    const bytes = new Uint8Array(event.data);
    if (bytes[0] !== 76) return; // 'L' — WLED live-view frame

    const is2d = bytes[1] === 2;
    const offset = is2d ? 4 : 2;

    let frameMax = 0;
    for (let i = offset; i < bytes.length; i++) {
      if (bytes[i] > frameMax) frameMax = bytes[i];
    }
    this._notePeak(frameMax);
    this._recomputeFactor();

    const wantMatrix =
      this._config.view === "matrix" || (this._config.view === "auto" && is2d);
    if (wantMatrix && is2d) this._drawMatrix(bytes);
    else this._drawStrip(bytes, offset);
  }

  _sizeCanvas() {
    const c = this._canvas;
    const rect = c.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    return c.getContext("2d");
  }

  _drawStrip(bytes, offset) {
    const ctx = this._sizeCanvas();
    if (!ctx) return;
    const c = this._canvas;
    const count = Math.floor((bytes.length - offset) / 3);
    if (count <= 0) return;

    const width = c.width / count;
    for (let i = 0; i < count; i++) {
      const p = offset + i * 3;
      const [r, g, b] = this._scale(bytes[p], bytes[p + 1], bytes[p + 2]);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      // +1 avoids hairline gaps between cells on fractional widths
      ctx.fillRect(Math.floor(i * width), 0, Math.ceil(width) + 1, c.height);
    }
  }

  _drawMatrix(bytes) {
    const ctx = this._sizeCanvas();
    if (!ctx) return;
    const c = this._canvas;
    const cols = bytes[2];
    const rows = bytes[3];
    if (!cols || !rows) return;

    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, c.width, c.height);

    const scale = Math.min(c.width / cols, c.height / rows);
    const xOffset = Math.floor((c.width - scale * cols) / 2);
    const yOffset = Math.floor((c.height - scale * rows) / 2);

    let i = 4;
    for (let y = 0.5; y < rows; y++) {
      for (let x = 0.5; x < cols; x++) {
        const [r, g, b] = this._scale(bytes[i], bytes[i + 1], bytes[i + 2]);
        ctx.fillStyle = `rgb(${r},${g},${b})`;
        ctx.beginPath();
        ctx.arc(x * scale + xOffset, y * scale + yOffset, 0.4 * scale, 0, 2 * Math.PI);
        ctx.fill();
        i += 3;
      }
    }
  }
}

customElements.define("wled-gateway-card", WledGatewayCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "wled-gateway-card",
  name: "WLED Gateway preview",
  description: "Live WLED preview that authenticates itself, so it survives switching between local and remote URLs.",
});

console.info(`%c WLED-GATEWAY-CARD %c ${CARD_VERSION} `, "color:#fff;background:#4a4;font-weight:700", "color:#4a4;background:#222");
