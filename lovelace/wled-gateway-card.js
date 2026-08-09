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

// Bump on every change, and bump the ?v= on the Lovelace resource URL to match
// — the browser caches the file by URL, so without that you keep the old one.
const CARD_VERSION = "1.5.0";

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

// The slug separates its parts with underscores (71966d0e_wled_gateway), but
// the add-on's hostname uses dashes (71966d0e-wled-gateway) and is the easier
// of the two to copy by mistake. Try what was given first, then the other
// spelling, so either works.
function slugCandidates(slug) {
  const tries = [slug];
  if (slug.includes("-")) tries.push(slug.replace(/-/g, "_"));
  if (slug.includes("_")) tries.push(slug.replace(/_/g, "-"));
  return [...new Set(tries)];
}

async function lookupIngressPath(hass, slug) {
  const errors = [];
  for (const candidate of slugCandidates(slug)) {
    try {
      const info = await hass.callWS({
        type: "supervisor/api",
        endpoint: `/addons/${candidate}/info`,
        method: "get",
      });
      if (!info || !info.ingress_url) {
        throw new Error(`"${candidate}" has no ingress URL — is Ingress enabled for it?`);
      }
      return info.ingress_url.replace(/\/$/, "");
    } catch (err) {
      errors.push(`${candidate}: ${err && err.message ? err.message : err}`);
    }
  }
  throw new Error(
    `no add-on found. Tried ${errors.join(" | ")}. ` +
      `The slug is in the add-on's page URL: /hassio/addon/<slug>/info`
  );
}

function resolveIngressPath(hass, slug) {
  if (ingressPaths[slug]) return ingressPaths[slug];
  ingressPaths[slug] = lookupIngressPath(hass, slug).catch((err) => {
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
      // 0 / 90 / 180 / 270, turning the preview clockwise:
      //   strip    0 runs left to right, 90 top to bottom, 180 right to left,
      //            270 bottom to top
      //   matrix   turns the panel, swapping its width and height at 90/270
      // Applied while drawing rather than as a CSS transform, so a vertical
      // strip lays out as a genuinely tall card instead of a wide one tipped
      // on its side and overflowing.
      rotate: 0,
      // Sizing, in order of precedence:
      //   height       explicit css length, e.g. "60px"
      //   aspect_ratio "16:9", or a percentage like "5%" (height/width, as the
      //                built-in iframe card uses)
      //   fill         otherwise stretch to whatever height the card is given,
      //                which is what you want in a sections dashboard
      height: null,
      // Narrows the preview within the card — mainly for vertical strips, which
      // otherwise stretch across the full width. The card itself keeps its grid
      // size; this only constrains what's drawn.
      width: null,
      align: "center", // center | left | right, when width is narrower than the card
      aspect_ratio: null,
      fill: true,
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

  // Sections dashboards size by grid rows rather than content. Without this a
  // strip gets a default-height cell and the preview floats in empty space.
  // Anything set under grid_options in the card config still wins.
  // Normalised to 0/90/180/270; anything else is treated as no rotation.
  _rotation() {
    const raw = ((parseInt(this._config.rotate, 10) || 0) % 360 + 360) % 360;
    return raw === 90 || raw === 180 || raw === 270 ? raw : 0;
  }

  getGridOptions() {
    const cfg = this._config || {};
    const matrix = cfg.view === "matrix";
    const rot = this._rotation();
    const vertical = matrix ? false : rot === 90 || rot === 270;
    return {
      // A vertical strip is useless one row tall, so it asks for height and a
      // narrow default width instead.
      rows: matrix ? 4 : vertical ? 6 : 1,
      min_rows: 1,
      columns: vertical ? 3 : 12,
      min_columns: vertical ? 1 : 3,
    };
  }

  connectedCallback() {
    if (this._config && this._hass) this._start();
  }

  disconnectedCallback() {
    this._stop();
  }

  /* ---------------- rendering ---------------- */

  // "5%" means height = 5% of width, matching the built-in iframe card, which
  // is what people will have used before switching to this one.
  _aspectRatioCss(value) {
    const raw = String(value).trim();
    if (raw.endsWith("%")) {
      const pct = parseFloat(raw);
      return pct > 0 ? `${100 / pct} / 1` : null;
    }
    const parts = raw.split(/[:/]/).map((n) => parseFloat(n));
    if (parts.length === 2 && parts[0] > 0 && parts[1] > 0) return `${parts[0]} / ${parts[1]}`;
    return null;
  }

  _render() {
    const cfg = this._config;
    const ratio = cfg.aspect_ratio ? this._aspectRatioCss(cfg.aspect_ratio) : null;

    // Fill has to be carried all the way down from the host, or the card keeps
    // its content height and leaves the rest of the grid cell empty.
    //
    // height/aspect_ratio are layered ON TOP of fill rather than replacing it:
    // used alone they'd overflow a smaller grid cell and simply be clipped —
    // which on a rotated strip hides the lit end and looks like a dead card.
    // With fill still applied they cap at the cell instead.
    let sizing = "";
    if (cfg.fill) {
      sizing += `
        :host { display: block; height: 100%; }
        ha-card { height: 100%; display: flex; flex-direction: column; }
        .wrap { flex: 1 1 auto; min-height: 24px; }`;
    }
    if (cfg.height) {
      sizing += `\n.wrap { flex: 0 1 auto; height: ${cfg.height};${cfg.fill ? " max-height: 100%;" : ""} }`;
    } else if (ratio) {
      sizing += `\n.wrap { flex: 0 1 auto; aspect-ratio: ${ratio}; width: 100%;${cfg.fill ? " max-height: 100%;" : ""} }`;
    } else if (!cfg.fill) {
      sizing += `\n.wrap { height: ${cfg.view === "matrix" ? "180px" : "40px"}; }`;
    }

    if (cfg.width) {
      // Comes last so it overrides the width any of the modes above set.
      const margin =
        cfg.align === "left" ? "0 auto 0 0" : cfg.align === "right" ? "0 0 0 auto" : "0 auto";
      sizing += `\n.wrap { width: ${cfg.width}; max-width: 100%; margin: ${margin}; }`;
    }

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .wrap { position: relative; background: #000; overflow: hidden; }
        /* Absolute, so the canvas can never contribute to layout. Sized from
           its own box it would feed back through devicePixelRatio — each pass
           measuring the size it just grew to — and run away down the page. */
        canvas { position: absolute; inset: 0; display: block; width: 100%; height: 100%; }
        ${sizing}
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
    this._wrap = this.shadowRoot.querySelector(".wrap");
    this._msg = this.shadowRoot.querySelector(".msg");
    // No CSS transform for `rotate` — it's applied while drawing, so the canvas
    // keeps the card's own shape instead of being turned inside it.

    // Repaint the last frame when the card is resized, so dragging it in the
    // editor tracks live instead of waiting for the device's next frame.
    if (this._resizeObserver) this._resizeObserver.disconnect();
    if (typeof ResizeObserver !== "undefined") {
      this._resizeObserver = new ResizeObserver(() => {
        if (this._lastFrame) this._paint(this._lastFrame);
      });
      this._resizeObserver.observe(this._wrap);
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

    const offset = bytes[1] === 2 ? 4 : 2;
    let frameMax = 0;
    for (let i = offset; i < bytes.length; i++) {
      if (bytes[i] > frameMax) frameMax = bytes[i];
    }
    this._notePeak(frameMax);
    this._recomputeFactor();

    this._lastFrame = bytes;
    this._paint(bytes);
  }

  _paint(bytes) {
    const is2d = bytes[1] === 2;
    const offset = is2d ? 4 : 2;
    const wantMatrix =
      this._config.view === "matrix" || (this._config.view === "auto" && is2d);
    if (wantMatrix && is2d) this._drawMatrix(bytes);
    else this._drawStrip(bytes, offset);
  }

  // Backing store follows the device pixel ratio, otherwise the matrix dots
  // look soft on phones and hi-dpi screens. Drawing stays in CSS pixels.
  _sizeCanvas() {
    const c = this._canvas;
    // Measure the wrapper, never the canvas: the canvas' size is a consequence
    // of this measurement, so reading it back would be circular.
    const rect = (this._wrap || c).getBoundingClientRect();
    const cssW = Math.max(1, Math.round(rect.width));
    const cssH = Math.max(1, Math.round(rect.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const w = Math.round(cssW * dpr);
    const h = Math.round(cssH * dpr);
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    const ctx = c.getContext("2d");
    if (ctx) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this._cssW = cssW;
      this._cssH = cssH;
    }
    return ctx;
  }

  _drawStrip(bytes, offset) {
    const ctx = this._sizeCanvas();
    if (!ctx) return;
    const count = Math.floor((bytes.length - offset) / 3);
    if (count <= 0) return;

    const rot = this._rotation();
    const vertical = rot === 90 || rot === 270;
    const backwards = rot === 180 || rot === 270;
    const span = (vertical ? this._cssH : this._cssW) / count;

    for (let i = 0; i < count; i++) {
      const p = offset + i * 3;
      const [r, g, b] = this._scale(bytes[p], bytes[p + 1], bytes[p + 2]);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      const slot = backwards ? count - 1 - i : i;
      // Overlap by a pixel: fractional sizes otherwise leave hairline gaps.
      if (vertical) ctx.fillRect(0, slot * span, this._cssW, span + 1);
      else ctx.fillRect(slot * span, 0, span + 1, this._cssH);
    }
  }

  _drawMatrix(bytes) {
    const ctx = this._sizeCanvas();
    if (!ctx) return;
    const cols = bytes[2];
    const rows = bytes[3];
    if (!cols || !rows) return;

    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, this._cssW, this._cssH);

    // At 90/270 the panel is drawn on its side, so the grid it has to fit into
    // swaps width and height.
    const rot = this._rotation();
    const turned = rot === 90 || rot === 270;
    const gridCols = turned ? rows : cols;
    const gridRows = turned ? cols : rows;

    const scale = Math.min(this._cssW / gridCols, this._cssH / gridRows);
    const xOffset = Math.floor((this._cssW - scale * gridCols) / 2);
    const yOffset = Math.floor((this._cssH - scale * gridRows) / 2);

    let i = 4;
    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < cols; col++) {
        const [r, g, b] = this._scale(bytes[i], bytes[i + 1], bytes[i + 2]);
        i += 3;

        let gx = col;
        let gy = row;
        if (rot === 90) {
          gx = rows - 1 - row;
          gy = col;
        } else if (rot === 180) {
          gx = cols - 1 - col;
          gy = rows - 1 - row;
        } else if (rot === 270) {
          gx = row;
          gy = cols - 1 - col;
        }

        ctx.fillStyle = `rgb(${r},${g},${b})`;
        ctx.beginPath();
        ctx.arc(
          (gx + 0.5) * scale + xOffset,
          (gy + 0.5) * scale + yOffset,
          0.4 * scale,
          0,
          2 * Math.PI
        );
        ctx.fill();
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
