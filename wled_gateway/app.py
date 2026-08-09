import asyncio
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wled-gateway")

OPTIONS_PATH = Path("/data/options.json")
HA_API = "http://supervisor/core/api"
HA_WS = "ws://supervisor/core/websocket"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")

# One place for everything WLED-preview related, run as a Home Assistant
# add-on with Ingress enabled. Because Ingress traffic is proxied through
# Home Assistant's own webserver, this is reachable through every access
# path HA itself already supports (local IP, local domain, external tunnel)
# with zero protocol-specific handling needed here — the served pages just
# derive their own path prefix from the URL they were loaded at, since
# Ingress assigns that prefix dynamically per install.
#
# Device list comes from the add-on's own Configuration tab in Supervisor
# (stored in /data/options.json) — edit there and restart the add-on to
# pick up changes, no rebuild needed.


CARD_SOURCE = Path("/app/www/wled-gateway-card.js")
# Where Home Assistant's config folder shows up depends on the mapping the
# Supervisor gives us, so look rather than assume — and if it isn't there,
# carry on without it.
HA_CONFIG_DIRS = (Path("/homeassistant"), Path("/config"))
card_status = {"installed": False, "path": None, "detail": "not attempted yet", "resource": None}


def install_lovelace_card():
    """Copy the bundled card into <config>/www so it updates with the add-on.

    Never raises: a missing mapping or a read-only filesystem should cost the
    card, not the add-on."""
    try:
        if not CARD_SOURCE.exists():
            card_status["detail"] = "card not bundled in this image"
            return
        config_dir = next((d for d in HA_CONFIG_DIRS if d.is_dir()), None)
        if config_dir is None:
            card_status["detail"] = "Home Assistant's config folder isn't mapped into the add-on"
            return

        www = config_dir / "www"
        www.mkdir(parents=True, exist_ok=True)
        target = www / CARD_SOURCE.name
        source_text = CARD_SOURCE.read_text()

        if target.exists() and target.read_text() == source_text:
            card_status.update(installed=True, path=str(target), detail="already up to date")
            log.info("lovelace card already current at %s", target)
            return

        target.write_text(source_text)
        card_status.update(installed=True, path=str(target), detail="installed")
        log.info("installed lovelace card to %s", target)
    except Exception as e:
        card_status["detail"] = f"could not install: {e}"
        log.warning("could not install the lovelace card: %s", e)


def card_version():
    try:
        match = re.search(r'CARD_VERSION\s*=\s*"([^"]+)"', CARD_SOURCE.read_text())
        return match.group(1) if match else None
    except Exception:
        return None


async def fetch_own_slug(app):
    """Ask Supervisor what we're called, so the info page can spell out the slug
    the card config needs instead of sending people to read a URL."""
    if not SUPERVISOR_TOKEN:
        return None
    try:
        async with app["session"].get(
            "http://supervisor/addons/self/info",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=5,
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
            return (payload.get("data") or {}).get("slug")
    except Exception as e:
        log.warning("could not ask Supervisor for our own slug: %s", e)
        return None


def slugify(text):
    """Match how Home Assistant turns a helper's name into its entity_id, so the
    id we predict up front is the id HA actually ends up creating."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_") or "device"


def helper_name(dev_id):
    """Name the helper such that HA slugifies it into default_input_select()."""
    return f"WLED Effect {dev_id}"


def default_input_select(dev_id):
    return f"input_select.wled_effect_{slugify(dev_id)}"


OPTIONS = json.loads(OPTIONS_PATH.read_text())
# Adding a device shouldn't mean hand-creating a matching helper and typing its
# entity id into the config. When left unset, each device gets a predictable
# input_select.wled_effect_<id>, created on first connect if it's missing.
AUTO_CREATE_HELPERS = OPTIONS.get("auto_create_helpers", True)
# Registering the card as a dashboard resource is the last manual step; doing it
# here also keeps its ?v= in step with the installed version.
AUTO_REGISTER_CARD = OPTIONS.get("auto_register_card", True)


def load_devices():
    devices = {}
    for dev in OPTIONS.get("devices", []):
        dev_id = str(dev["id"])
        configured = dev.get("input_select")
        devices[dev_id] = {
            "name": dev.get("name", dev_id),
            "ip": dev["ip"],
            "input_select": configured or (default_input_select(dev_id) if AUTO_CREATE_HELPERS else None),
            "full_brightness_preview": bool(dev.get("full_brightness_preview", True)),
            # 0 / unset means "follow the device"; anything else is a constant.
            "preview_brightness": int(dev.get("preview_brightness") or 0),
        }
    return devices


DEVICES = load_devices()
subscribers = {dev_id: set() for dev_id in DEVICES}
device_status = {
    dev_id: {
        "connected": False,
        "last_connected_at": None,
        "bri": None,
        "frame_peak": None,
        # Diagnostics: "the preview looks wrong" is much easier to act on with
        # the frame rate and viewer count visible.
        "fps": 0.0,
        "frames": 0,
        "frames_since": None,
    }
    for dev_id in DEVICES
}
FPS_WINDOW_SECONDS = 5


async def measure_frame_rates():
    """Turn the running frame counters into a rate, on a fixed window so the
    number on the page means the same thing every time it's read."""
    while True:
        await asyncio.sleep(FPS_WINDOW_SECONDS)
        now = time.time()
        for dev_id, status in device_status.items():
            started = status["frames_since"]
            elapsed = (now - started) if started else FPS_WINDOW_SECONDS
            status["fps"] = round(status["frames"] / elapsed, 1) if elapsed > 0 else 0.0
            status["frames"] = 0
            status["frames_since"] = now

PREVIEW_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1">
  <meta charset="utf-8">
  <meta name="theme-color" content="#222222">
  <title>WLED Live Preview</title>
  <style>
  html, body { margin: 0; background: #000; overflow: hidden; width: 100%; height: 100%; }
  #canv { position: absolute; transform-origin: center center; background: #000; }
  * { box-sizing: border-box; }
  </style>
  <script>
    function getUrlParameter(name, defaultVal = null) {
      const urlParams = new URLSearchParams(window.location.search);
      return urlParams.get(name) || defaultVal;
    }
    var wledId = getUrlParameter('wled', '1');
    var rotate = parseInt(getUrlParameter('rotate', '0'));

    // Live-preview pixels arrive already scaled by the device's master
    // brightness, so a dimmed strip previews dim. Undo that scaling to show the
    // colours at full strength. Defaults to this device's add-on setting; a
    // ?normalize=0/1 on the card URL overrides it for that card only.
    var normalizeDefault = __NORMALIZE_DEFAULT__;
    var normalizeParam = getUrlParameter('normalize');
    var normalize = normalizeParam === null ? normalizeDefault : normalizeParam !== '0';
    var gain = parseFloat(getUrlParameter('gain', '1')) || 1;

    // A fixed percentage (from the device's setting, or ?bright= on the card)
    // ignores the device brightness altogether: 100 = exactly as received,
    // 175 = the boost this preview always used, 0 = follow the device.
    var fixedPercent = parseFloat(getUrlParameter('bright', '__FIXED_PERCENT__')) || 0;

    var BASE_GAIN = 1.75;   // the preview has always been shown boosted; without
                            // this it reads as flat at full brightness
    var MAX_FACTOR = 24;    // high enough to actually finish normalising a
                            // heavily dimmed strip — capping lower leaves the
                            // preview dim, which reads as washed-out and
                            // low-contrast. The noise floor, not the cap, is
                            // what keeps quantisation speckle out.
    var NOISE_FLOOR = 2;    // 1-2 counts on a dimmed strip are quantisation
                            // remnants, not light — scaling them makes "snow"
    var deviceBri = 255;
    var factor = BASE_GAIN * gain;

    // Rolling peak of what's actually arriving. WLED restores pixel colour when
    // it reads the strip back, so on some setups the live view is already at
    // full scale even when the device is dimmed. Dividing by brightness there
    // would double-correct and blow the picture out to white, so the boost is
    // also limited by what the data itself justifies: if frames already reach
    // 255, nothing is scaled up regardless of the brightness reported.
    var peak = 255, peakSetAt = 0;
    var PEAK_HOLD_MS = 2000;

    function notePeak(frameMax) {
      const now = Date.now();
      if (frameMax >= peak || now - peakSetAt > PEAK_HOLD_MS) {
        peak = frameMax;
        peakSetAt = now;
      }
    }

    function recomputeFactor() {
      if (fixedPercent > 0) {
        factor = Math.min(MAX_FACTOR, (fixedPercent / 100) * gain);
        return;
      }
      if (!normalize) { factor = BASE_GAIN * gain; return; }
      const byBrightness = 255 / Math.max(deviceBri, 1);
      const byData = 255 / Math.max(peak, 1);
      factor = Math.min(MAX_FACTOR, BASE_GAIN * gain * Math.min(byBrightness, byData));
    }
    recomputeFactor();

    // Scale the pixel as a whole rather than each channel independently: if a
    // channel would clip, back the whole pixel off instead, so boosting can't
    // shift hue (a boosted orange turning yellow as red saturates first).
    function scalePixel(r, g, b) {
      if (factor === 1) return [r, g, b];
      const mx = Math.max(r, g, b);
      if (mx <= NOISE_FLOOR) return [0, 0, 0];
      const f = mx * factor > 255 ? 255 / mx : factor;
      return [Math.round(r * f), Math.round(g * f), Math.round(b * f)];
    }

    function onStateMessage(raw) {
      try {
        const msg = JSON.parse(raw);
        if (typeof msg.bri === 'number') {
          deviceBri = msg.bri;
          recomputeFactor();
        }
      } catch (err) { /* not a state message we care about */ }
    }

    function applyRotation() {
      const canv = document.getElementById("canv");
      let transform = '', width = '100%', height = '100%', top = '0', left = '0';
      switch (rotate) {
        case 90:
          transform = 'rotate(90deg)'; width = '100vh'; height = '100vw';
          top = `calc((100vh - 100vw) / 2)`; left = `calc((100vw - 100vh) / 2)`;
          break;
        case 180:
          transform = 'rotate(180deg)';
          break;
        case 270:
          transform = 'rotate(270deg)'; width = '100vh'; height = '100vw';
          top = `calc((100vh - 100vw) / 2)`; left = `calc((100vw - 100vh) / 2)`;
          break;
        default:
          transform = 'rotate(0deg)';
      }
      canv.style.transform = transform;
      canv.style.width = width;
      canv.style.height = height;
      canv.style.top = top;
      canv.style.left = left;
    }

    // Ingress assigns a per-install path prefix dynamically (e.g.
    // /api/hassio_ingress/<token>), so derive it from wherever this page
    // itself was actually loaded rather than assuming any fixed value.
    function wsPrefix() {
      const parts = window.location.pathname.split('/');
      parts.pop(); // drop "preview"
      return parts.join('/');
    }

    function start() {
      applyRotation();
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${proto}//${window.location.host}${wsPrefix()}/ws/${wledId}`);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send("{'lv':true}");
      ws.addEventListener("message", event => {
        try {
          if (typeof event.data === "string") { onStateMessage(event.data); return; }
          if (Object.prototype.toString.call(event.data) !== "[object ArrayBuffer]") return;
          const bytes = new Uint8Array(event.data);
          if (bytes[0] !== 76) return;
          const offset = (bytes[1] === 2) ? 4 : 2;
          let frameMax = 0;
          for (let i = offset; i < bytes.length; i++) {
            if (bytes[i] > frameMax) frameMax = bytes[i];
          }
          notePeak(frameMax);
          recomputeFactor();

          let grad = "linear-gradient(90deg,";
          for (let i = offset; i < bytes.length; i += 3) {
            const [r, g, b] = scalePixel(bytes[i], bytes[i + 1], bytes[i + 2]);
            grad += `rgb(${r},${g},${b})`;
            if (i < bytes.length - 3) grad += ",";
          }
          grad += ")";
          document.getElementById("canv").style.background = grad;
        } catch (err) {
          console.error("WLED preview WS error:", err);
        }
      });
    }
  </script>
</head>
<body onload="start()">
  <div id="canv"></div>
</body>
</html>
"""

PREVIEW2D_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>WLED Live Preview</title>
  <style>
    html, body {
      margin: 0; overflow: hidden; width: 100%; height: 100%;
      background-color: rgba(15, 15, 15, 1);
      display: flex; align-items: center; justify-content: center;
    }
    #canv { display: block; }
  </style>
</head>
<body>
  <canvas id="canv" width="1600" height="300"></canvas>
  <script>
    function getUrlParameter(name, defaultVal = null) {
      const urlParams = new URLSearchParams(window.location.search);
      return urlParams.get(name) || defaultVal;
    }
    var wledId = getUrlParameter('wled', '1');
    const c = document.getElementById("canv");
    const ctx = c.getContext("2d");
    let throttled = false;

    // See the 1D preview: pixels arrive pre-scaled by master brightness, so
    // undo it unless this device (or the card URL) asks for the true look.
    var normalizeDefault = __NORMALIZE_DEFAULT__;
    var normalizeParam = getUrlParameter('normalize');
    var normalize = normalizeParam === null ? normalizeDefault : normalizeParam !== '0';
    var gain = parseFloat(getUrlParameter('gain', '1')) || 1;
    var fixedPercent = parseFloat(getUrlParameter('bright', '__FIXED_PERCENT__')) || 0;

    var BASE_GAIN = 1.75;
    var MAX_FACTOR = 24;   // see the 1D preview
    var NOISE_FLOOR = 2;
    var deviceBri = 255;
    var factor = BASE_GAIN * gain;

    // Rolling peak of what's actually arriving. WLED restores pixel colour when
    // it reads the strip back, so on some setups the live view is already at
    // full scale even when the device is dimmed. Dividing by brightness there
    // would double-correct and blow the picture out to white, so the boost is
    // also limited by what the data itself justifies: if frames already reach
    // 255, nothing is scaled up regardless of the brightness reported.
    var peak = 255, peakSetAt = 0;
    var PEAK_HOLD_MS = 2000;

    function notePeak(frameMax) {
      const now = Date.now();
      if (frameMax >= peak || now - peakSetAt > PEAK_HOLD_MS) {
        peak = frameMax;
        peakSetAt = now;
      }
    }

    function recomputeFactor() {
      if (fixedPercent > 0) {
        factor = Math.min(MAX_FACTOR, (fixedPercent / 100) * gain);
        return;
      }
      if (!normalize) { factor = BASE_GAIN * gain; return; }
      const byBrightness = 255 / Math.max(deviceBri, 1);
      const byData = 255 / Math.max(peak, 1);
      factor = Math.min(MAX_FACTOR, BASE_GAIN * gain * Math.min(byBrightness, byData));
    }
    recomputeFactor();

    // Whole-pixel scaling with a noise floor — see the 1D preview for why.
    function scalePixel(r, g, b) {
      if (factor === 1) return [r, g, b];
      const mx = Math.max(r, g, b);
      if (mx <= NOISE_FLOOR) return [0, 0, 0];
      const f = mx * factor > 255 ? 255 / mx : factor;
      return [Math.round(r * f), Math.round(g * f), Math.round(b * f)];
    }

    function onStateMessage(raw) {
      try {
        const msg = JSON.parse(raw);
        if (typeof msg.bri === 'number') {
          deviceBri = msg.bri;
          recomputeFactor();
        }
      } catch (err) { /* not a state message we care about */ }
    }

    function setCanvas() {
      c.width = 0.98 * window.innerWidth;
      c.height = 0.98 * window.innerHeight;
    }
    setCanvas();
    window.addEventListener("resize", () => {
      if (throttled) return;
      throttled = true;
      setCanvas();
      setTimeout(() => { throttled = false; }, 250);
    });

    function wsPrefix() {
      const parts = window.location.pathname.split('/');
      parts.pop(); // drop "preview2d"
      return parts.join('/');
    }

    if (ctx) {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${proto}//${window.location.host}${wsPrefix()}/ws/${wledId}`);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send("{'lv':true}");
      ws.addEventListener("message", event => {
        try {
          if (typeof event.data === "string") { onStateMessage(event.data); return; }
          if (Object.prototype.toString.call(event.data) !== "[object ArrayBuffer]") return;
          const bytes = new Uint8Array(event.data);
          if (bytes[0] !== 76 || bytes[1] !== 2) return;
          const cols = bytes[2], rows = bytes[3];
          let frameMax = 0;
          for (let k = 4; k < bytes.length; k++) {
            if (bytes[k] > frameMax) frameMax = bytes[k];
          }
          notePeak(frameMax);
          recomputeFactor();

          const scale = Math.min(c.width / cols, c.height / rows);
          const xOffset = Math.floor((c.width - scale * cols) / 2);
          let i = 4;
          for (let y = 0.5; y < rows; y++) {
            for (let x = 0.5; x < cols; x++) {
              const [pr, pg, pb] = scalePixel(bytes[i], bytes[i + 1], bytes[i + 2]);
              ctx.fillStyle = `rgb(${pr},${pg},${pb})`;
              ctx.beginPath();
              ctx.arc(x * scale + xOffset, y * scale, 0.4 * scale, 0, 2 * Math.PI);
              ctx.fill();
              i += 3;
            }
          }
        } catch (err) {
          console.error("WLED preview WS error:", err);
        }
      });
    }
  </script>
</body>
</html>
"""


async def ha_entity_exists(app, entity_id):
    """None means 'couldn't tell' — distinct from a definite 'not there', so a
    transient API failure never triggers a duplicate helper being created."""
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        async with app["session"].get(f"{HA_API}/states/{entity_id}", headers=headers, timeout=5) as resp:
            if resp.status == 200:
                return True
            if resp.status == 404:
                return False
            log.warning("unexpected %s from HA checking %s", resp.status, entity_id)
            return None
    except Exception as e:
        log.warning("could not ask HA about %s: %s", entity_id, e)
        return None


class HAWebSocketError(Exception):
    """A command came back as a failure result rather than a transport problem."""

    def __init__(self, error):
        super().__init__(error.get("message") or str(error))
        self.code = error.get("code")
        self.error = error


class HAWebSocket:
    """Thin request/response wrapper: HA's websocket API matches replies to
    commands by id, and several things here need more than one command."""

    def __init__(self, ws):
        self._ws = ws
        self._id = 0

    async def call(self, command_type, **payload):
        self._id += 1
        command_id = self._id
        await self._ws.send_json({"id": command_id, "type": command_type, **payload})
        while True:
            msg = await self._ws.receive_json(timeout=10)
            if msg.get("type") == "result" and msg.get("id") == command_id:
                if msg.get("success"):
                    return msg.get("result")
                raise HAWebSocketError(msg.get("error") or {})


@asynccontextmanager
async def ha_websocket(app):
    """Authenticated connection to Home Assistant. Several things the add-on
    does — creating helpers, registering the card — have no REST equivalent."""
    async with app["session"].ws_connect(HA_WS, timeout=10) as ws:
        hello = await ws.receive_json(timeout=10)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting: {hello.get('type')}")
        await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
        auth = await ws.receive_json(timeout=10)
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"auth failed: {auth}")
        yield HAWebSocket(ws)


async def ha_create_input_select(app, name, options):
    """Create an input_select helper. There's no REST endpoint for this — helper
    creation only exists on the WebSocket API, and it requires an admin token."""
    try:
        async with ha_websocket(app) as ha:
            await ha.call("input_select/create", name=name, options=options)
            return True
    except HAWebSocketError as e:
        if e.code == "unauthorized":
            log.warning(
                "not allowed to create helpers — create %r by hand, or set it "
                "explicitly per device with input_select:",
                name,
            )
        else:
            log.warning("HA refused to create helper %r: %s", name, e)
        return False
    except Exception as e:
        log.warning("could not create helper %r over the HA websocket: %s", name, e)
        return False


async def discover_wled_devices(app):
    """WLED devices Home Assistant already knows about.

    The WLED integration records each device's address as its configuration_url,
    so there's no need to scan the network or ask anyone to type an IP."""
    try:
        async with ha_websocket(app) as ha:
            registry = await ha.call("config/device_registry/list")
    except Exception as e:
        log.warning("could not read the device registry: %s", e)
        return []

    configured_ips = {dev["ip"].split(":")[0] for dev in DEVICES.values()}
    found = []
    for device in registry or []:
        if not any(str(d[0]) == "wled" for d in device.get("identifiers") or [] if len(d) >= 1):
            continue
        url = device.get("configuration_url") or ""
        host = url.split("//")[-1].strip("/")
        if not host:
            continue
        found.append(
            {
                "name": device.get("name_by_user") or device.get("name") or host,
                "host": host,
                "configured": host in configured_ips,
            }
        )
    found.sort(key=lambda d: d["name"].lower())
    return found


async def add_discovered_device(app, host, name):
    """Append a device to our own options and restart to pick it up.

    Supervisor lets an add-on rewrite its own options, which is the same thing
    the Configuration tab does — the restart is because the device list is read
    at startup."""
    devices = list(OPTIONS.get("devices", []))
    if any(str(d.get("ip", "")).split(":")[0] == host for d in devices):
        return False, "that device is already configured"

    used_ids = {str(d.get("id")) for d in devices}
    next_id = 1
    while str(next_id) in used_ids:
        next_id += 1
    devices.append({"id": str(next_id), "name": name or host, "ip": host})

    options = {**OPTIONS, "devices": devices}
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    async with app["session"].post(
        "http://supervisor/addons/self/options", json={"options": options}, headers=headers, timeout=10
    ) as resp:
        if resp.status >= 300:
            return False, f"Supervisor refused the change ({resp.status})"

    log.info("added device %s (%s) as id %s; restarting to pick it up", name, host, next_id)
    asyncio.create_task(restart_self(app))
    return True, f"added as device {next_id} — restarting"


async def restart_self(app):
    # Give the response a moment to reach the browser before we go down.
    await asyncio.sleep(1)
    try:
        async with app["session"].post(
            "http://supervisor/addons/self/restart",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=30,
        ) as resp:
            log.info("restart requested: %s", resp.status)
    except Exception as e:
        log.warning("could not restart automatically, restart by hand: %s", e)


async def handle_discover(request):
    return web.json_response(await discover_wled_devices(request.app))


async def handle_discover_add(request):
    body = await request.json()
    host = str(body.get("host", "")).strip()
    if not host:
        raise web.HTTPBadRequest(text="no host given")
    ok, detail = await add_discovered_device(request.app, host, str(body.get("name", "")).strip())
    return web.json_response({"ok": ok, "detail": detail})


CARD_RESOURCE_PATH = "/local/wled-gateway-card.js"


async def register_lovelace_resource(app):
    """Register the card as a dashboard resource, and keep its ?v= in step with
    the installed version so browsers pick up a new card after an update.

    Only possible on storage-mode dashboards: with Lovelace in YAML mode the
    resource list is part of your configuration.yaml and isn't ours to edit."""
    version = card_version() or "1"
    wanted = f"{CARD_RESOURCE_PATH}?v={version}"
    try:
        async with ha_websocket(app) as ha:
            existing = await ha.call("lovelace/resources/list")
            mine = next(
                (r for r in existing or [] if str(r.get("url", "")).split("?")[0] == CARD_RESOURCE_PATH),
                None,
            )
            if mine is None:
                await ha.call("lovelace/resources/create", res_type="module", url=wanted)
                card_status["resource"] = f"registered as {wanted}"
                log.info("registered lovelace resource %s", wanted)
            elif mine.get("url") != wanted:
                await ha.call("lovelace/resources/update", resource_id=mine["id"], url=wanted)
                card_status["resource"] = f"updated to {wanted}"
                log.info("updated lovelace resource to %s", wanted)
            else:
                card_status["resource"] = f"already registered as {wanted}"
    except HAWebSocketError as e:
        by_hand = f"add {wanted} as a JavaScript module by hand"
        if e.code == "unknown_command":
            card_status["resource"] = f"dashboards are in YAML mode, so {by_hand}"
        elif e.code == "unauthorized":
            card_status["resource"] = f"not allowed to edit dashboard resources, so {by_hand}"
        else:
            card_status["resource"] = f"could not register ({e}), so {by_hand}"
        log.warning("could not register the lovelace resource: %s", e)
    except Exception as e:
        card_status["resource"] = f"could not register: {e}"
        log.warning("could not register the lovelace resource: %s", e)


async def sync_effect_list(app, dev_id, ip, input_select_entity):
    """Push the device's real, current effect list into an HA input_select's
    options, so dashboard dropdowns never go stale. Skipped if no
    input_select was configured for this device."""
    if not input_select_entity:
        return
    if not SUPERVISOR_TOKEN:
        log.warning("no SUPERVISOR_TOKEN available; enable homeassistant_api to sync effect lists")
        return
    try:
        async with app["session"].get(f"http://{ip}/json/effects", timeout=5) as resp:
            effects = await resp.json()
    except Exception as e:
        log.warning("could not fetch effect list from %s (%s): %s", dev_id, ip, e)
        return

    # WLED pads unused effect IDs with placeholder "RSVD" (reserved) entries —
    # duplicates, which input_select.set_options rejects outright. Drop them
    # entirely (they aren't selectable effects) and dedupe whatever is left,
    # keeping first-occurrence order.
    effects = [e for e in dict.fromkeys(effects) if e != "RSVD"]
    if not effects:
        log.warning("device %s returned no usable effects; nothing to sync", dev_id)
        return

    # Create the helper on first sight rather than making the user pre-create one
    # per device. Only when it's definitely absent — "couldn't tell" is left alone.
    if AUTO_CREATE_HELPERS and await ha_entity_exists(app, input_select_entity) is False:
        name = helper_name(dev_id)
        if await ha_create_input_select(app, name, effects):
            log.info("created %s (%r) with %d effects", input_select_entity, name, len(effects))
            # Created with the right options already; nothing left to push.
            return
        return

    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    payload = {"entity_id": input_select_entity, "options": effects}
    try:
        async with app["session"].post(
            f"{HA_API}/services/input_select/set_options", json=payload, headers=headers, timeout=5
        ) as resp:
            if resp.status >= 300:
                text = await resp.text()
                log.warning("HA set_options failed for %s: %s %s", input_select_entity, resp.status, text)
            else:
                log.info("synced %d effects to %s", len(effects), input_select_entity)
    except Exception as e:
        log.warning("could not reach HA API to sync %s: %s", input_select_entity, e)


# Live sockets to the devices, so a viewer arriving or leaving can turn their
# live view on and off. Sending it forever would keep every device streaming
# frames over WiFi around the clock, whether or not anyone has a dashboard open.
upstream_ws = {}
idle_tasks = {}
live_view_on = {}
IDLE_GRACE_SECONDS = 10


async def set_live_view(dev_id, enabled, force=False):
    """force is for a fresh connection, where the device has forgotten whatever
    we last told it and our idea of the current state means nothing."""
    enabled = bool(enabled)
    if not force and live_view_on.get(dev_id) == enabled:
        return
    ws = upstream_ws.get(dev_id)
    if ws is None or ws.closed:
        return
    try:
        # WLED tracks a single live-view client: {"lv": false} clears it and it
        # stops sending frames entirely.
        await ws.send_str(json.dumps({"lv": enabled}))
        live_view_on[dev_id] = enabled
        log.info("live view %s for %s", "on" if enabled else "off", dev_id)
    except Exception as e:
        log.warning("could not toggle live view for %s: %s", dev_id, e)


async def _disable_after_grace(dev_id):
    """Wait before going idle: navigating between dashboards drops and remakes
    the connection, and that shouldn't stop and restart the device's stream."""
    try:
        await asyncio.sleep(IDLE_GRACE_SECONDS)
        if not subscribers[dev_id]:
            await set_live_view(dev_id, False)
    except asyncio.CancelledError:
        pass
    finally:
        idle_tasks.pop(dev_id, None)


def cancel_idle_timer(dev_id):
    task = idle_tasks.pop(dev_id, None)
    if task:
        task.cancel()


def extract_brightness(payload):
    """Pull master brightness out of a WLED state push. Live-preview pixel data
    arrives already scaled by it, so viewers need it to undo that scaling."""
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    state = data.get("state") if isinstance(data.get("state"), dict) else data
    bri = state.get("bri")
    return bri if isinstance(bri, int) and 0 <= bri <= 255 else None


async def fanout(dev_id, *, data=None, text=None):
    """Send to every viewer over a snapshot of the subscriber set — see the note
    in upstream_loop about why this must not iterate the live set."""
    for client in list(subscribers[dev_id]):
        try:
            if text is not None:
                await client.send_str(text)
            else:
                await client.send_bytes(data)
        except Exception:
            subscribers[dev_id].discard(client)


async def upstream_loop(app, dev_id, ip, input_select_entity):
    backoff = 1
    while True:
        try:
            async with app["session"].ws_connect(f"ws://{ip}/ws", heartbeat=20) as ws:
                log.info("upstream connected: %s (%s)", dev_id, ip)
                device_status[dev_id]["connected"] = True
                device_status[dev_id]["last_connected_at"] = time.time()
                upstream_ws[dev_id] = ws
                # Only ask for frames if someone is actually watching; the socket
                # stays open either way, for state updates like brightness.
                await set_live_view(dev_id, bool(subscribers[dev_id]), force=True)
                await sync_effect_list(app, dev_id, ip, input_select_entity)
                backoff = 1
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        # Diagnostic: the brightest channel in this frame. Read
                        # alongside "bri" it says whether the device scales the
                        # live view by brightness or sends it already restored.
                        payload = msg.data[4:] if len(msg.data) > 1 and msg.data[1] == 2 else msg.data[2:]
                        if payload:
                            device_status[dev_id]["frame_peak"] = max(payload)
                        device_status[dev_id]["frames"] += 1
                        # Iterate a snapshot: sending yields to the event loop, so a
                        # viewer connecting or disconnecting mid-frame would otherwise
                        # mutate this set while it's being iterated. That raises
                        # RuntimeError, which the handler below would treat as a dead
                        # upstream — dropping the feed for every viewer just because
                        # one of them opened or closed a tab.
                        await fanout(dev_id, data=msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        # WLED pushes its state on this same socket, on connect and
                        # whenever it changes. That's where brightness comes from.
                        bri = extract_brightness(msg.data)
                        if bri is not None and bri != device_status[dev_id]["bri"]:
                            device_status[dev_id]["bri"] = bri
                            await fanout(dev_id, text=json.dumps({"bri": bri}))
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
        except Exception as e:
            log.warning("upstream %s (%s) dropped: %s", dev_id, ip, e)
        upstream_ws.pop(dev_id, None)
        live_view_on.pop(dev_id, None)
        device_status[dev_id]["connected"] = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def handle_ws(request):
    dev_id = request.match_info["id"]
    if dev_id not in DEVICES:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    was_idle = not subscribers[dev_id]
    subscribers[dev_id].add(ws)
    log.info("client subscribed to %s (now %d)", dev_id, len(subscribers[dev_id]))
    cancel_idle_timer(dev_id)
    if was_idle:
        await set_live_view(dev_id, True)
    # Send what we already know, so a card opened between state changes doesn't
    # render un-normalised until the user next touches the brightness slider.
    bri = device_status[dev_id]["bri"]
    if bri is not None:
        try:
            await ws.send_str(json.dumps({"bri": bri}))
        except Exception:
            pass
    try:
        async for _ in ws:
            pass  # clients only ever send the initial {'lv':true}; nothing to act on
    finally:
        subscribers[dev_id].discard(ws)
        log.info("client unsubscribed from %s (now %d)", dev_id, len(subscribers[dev_id]))
        if not subscribers[dev_id] and dev_id not in idle_tasks:
            idle_tasks[dev_id] = asyncio.create_task(_disable_after_grace(dev_id))
    return ws


async def handle_json_live(request):
    dev_id = request.match_info["id"]
    dev = DEVICES.get(dev_id)
    if not dev:
        raise web.HTTPNotFound()
    async with request.app["session"].get(f"http://{dev['ip']}/json/live", timeout=5) as resp:
        body = await resp.read()
        return web.Response(body=body, content_type="application/json")


def render_preview(template, dev_id):
    """Bake this device's brightness setting into the page as the default. Plain
    substitution rather than str.format, since the page is full of CSS and JS
    braces that format() would choke on."""
    dev = DEVICES[dev_id]
    return (
        template
        .replace("__NORMALIZE_DEFAULT__", "true" if dev["full_brightness_preview"] else "false")
        .replace("__FIXED_PERCENT__", str(dev["preview_brightness"]))
    )


async def handle_preview(request):
    dev_id = request.query.get("wled", "1")
    if dev_id not in DEVICES:
        raise web.HTTPNotFound()
    return web.Response(text=render_preview(PREVIEW_HTML, dev_id), content_type="text/html")


async def handle_preview2d(request):
    dev_id = request.query.get("wled", "1")
    if dev_id not in DEVICES:
        raise web.HTTPNotFound()
    return web.Response(text=render_preview(PREVIEW2D_HTML, dev_id), content_type="text/html")


async def handle_devices(request):
    return web.json_response({
        dev_id: {
            **dev,
            "connected": device_status[dev_id]["connected"],
            "bri": device_status[dev_id]["bri"],
            "frame_peak": device_status[dev_id]["frame_peak"],
            "fps": device_status[dev_id]["fps"],
            "viewers": len(subscribers[dev_id]),
            "live_view": bool(live_view_on.get(dev_id)),
            "connected_for": (
                round(time.time() - device_status[dev_id]["last_connected_at"])
                if device_status[dev_id]["connected"] and device_status[dev_id]["last_connected_at"]
                else None
            ),
        }
        for dev_id, dev in DEVICES.items()
    })


HOP_BY_HOP_HEADERS = {"host", "content-length", "content-encoding", "transfer-encoding", "connection"}


async def handle_device_root_redirect(request):
    dev_id = request.match_info["id"]
    if dev_id not in DEVICES:
        raise web.HTTPNotFound()
    ingress_path = request.headers.get("X-Ingress-Path", "")
    raise web.HTTPFound(f"{ingress_path}/device/{dev_id}/")


async def proxy_device_websocket(request, ip, subpath):
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)
    try:
        async with request.app["session"].ws_connect(f"ws://{ip}/{subpath}") as ws_upstream:

            async def pump_upstream():
                async for msg in ws_upstream:
                    if msg.type == WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

            pump_task = asyncio.create_task(pump_upstream())
            try:
                async for msg in ws_client:
                    if msg.type == WSMsgType.TEXT:
                        await ws_upstream.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws_upstream.send_bytes(msg.data)
            finally:
                pump_task.cancel()
    except Exception as e:
        log.warning("device UI websocket proxy error for %s: %s", ip, e)
    return ws_client


async def handle_device_proxy(request):
    """Reverse-proxies a WLED device's own admin web UI, so it can be opened
    or embedded directly from Home Assistant for setup/debugging — no need
    to separately find and visit the device's raw IP.

    Best-effort: WLED's UI wasn't built to run under a URL sub-path, so if
    it hardcodes any absolute (leading-slash) asset or API paths, those
    specific requests will miss this proxy. Most of the UI is served from
    relative paths and works fine; live state updates over its own internal
    WebSocket are the most likely thing to not fully work through the proxy.
    """
    dev_id = request.match_info["id"]
    subpath = request.match_info.get("path", "")
    dev = DEVICES.get(dev_id)
    if not dev:
        raise web.HTTPNotFound()
    ip = dev["ip"]

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_device_websocket(request, ip, subpath)

    upstream_url = f"http://{ip}/{subpath}"
    if request.query_string:
        upstream_url += f"?{request.query_string}"

    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
    try:
        async with request.app["session"].request(
            request.method, upstream_url, data=body, headers=headers, timeout=15, allow_redirects=False
        ) as resp:
            resp_body = await resp.read()
            out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
            return web.Response(status=resp.status, body=resp_body, headers=out_headers)
    except Exception as e:
        log.warning("device UI proxy error for %s (%s): %s", dev_id, ip, e)
        raise web.HTTPBadGateway(text=f"Could not reach device at {ip}")


INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>WLED Gateway</title>
  <style>
    body { font-family: sans-serif; background: #111; color: #eee; margin: 2em; }
    h1 { font-weight: 500; }
    code, pre { background: #222; padding: 0.3em 0.5em; border-radius: 4px; }
    pre { overflow-x: auto; }
    table { border-collapse: collapse; margin-top: 1em; width: 100%; }
    th, td { text-align: left; padding: 0.4em 0.8em; border-bottom: 1px solid #333; }
    button { margin-left: 0.5em; cursor: pointer; }
    a { color: #7bc9ff; }
    .dot { display: inline-block; width: 0.7em; height: 0.7em; border-radius: 50%; margin-right: 0.4em; }
    .dot.on { background: #4caf50; }
    .dot.off { background: #b33; }
  </style>
</head>
<body>
  <h1>WLED Gateway</h1>
  <p>This is the base path your Lovelace iframe card URLs need. It's
  detected live from how this page itself was loaded, so it's always
  correct — even after a reinstall changes the Ingress token.</p>
  <pre id="base">__INGRESS_PATH__</pre>
  <button onclick="navigator.clipboard.writeText(document.getElementById('base').textContent)">Copy</button>

  <h2>Configured devices</h2>
  <table>
    <tr><th>Status</th><th>ID</th><th>Name</th><th>IP</th><th>Viewers</th><th>FPS</th><th>Preview card URL</th><th>Device Web UI</th></tr>
    __ROWS__
  </table>
  <p><small>FPS is 0 with no viewers by design — devices are only asked to
  stream while something is watching.</small></p>

  <h2>Add a device</h2>
  <p>WLED devices Home Assistant already knows about. Adding one writes it to
  this add-on's configuration and restarts it.</p>
  <div id="discovered">looking…</div>
  <script>
    const base = window.location.pathname.replace(/\/$/, '');
    async function loadDiscovered() {
      const box = document.getElementById('discovered');
      try {
        const found = await (await fetch(base + '/discover')).json();
        if (!found.length) { box.textContent = 'No WLED devices found in Home Assistant.'; return; }
        box.innerHTML = '<table><tr><th>Name</th><th>Address</th><th></th></tr>' + found.map(d =>
          `<tr><td>${d.name}</td><td><code>${d.host}</code></td><td>` +
          (d.configured ? 'already added'
            : `<button data-host="${d.host}" data-name="${d.name}">Add</button>`) +
          '</td></tr>').join('') + '</table>';
        box.querySelectorAll('button').forEach(b => b.addEventListener('click', async () => {
          b.disabled = true; b.textContent = 'adding…';
          const r = await (await fetch(base + '/discover/add', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({host: b.dataset.host, name: b.dataset.name})
          })).json();
          b.textContent = r.detail || (r.ok ? 'added' : 'failed');
        }));
      } catch (err) { box.textContent = 'Could not look for devices: ' + err.message; }
    }
    loadDiscovered();
  </script>

  <h2>Lovelace card</h2>
  __CARD_SECTION__
</body>
</html>
"""


CARD_SECTION_UNAVAILABLE = """<p>The custom card isn't installed. {detail}</p>
<p>You can still use iframe cards with the base path above — see the add-on
documentation.</p>"""

CARD_SECTION = """<p>A custom card is installed and kept in step with the add-on, so
previews survive switching between your local and remote URLs — an iframe on an
Ingress URL can't, and returns 401 until this page is opened by hand.</p>
<p>Add it once under <b>Settings &rarr; Dashboards &rarr; Resources</b> as a
<b>JavaScript module</b>:</p>
<pre id="resource">/local/wled-gateway-card.js?v={card_version}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('resource').textContent)">Copy</button>
<p>Then paste a card. This add-on's slug is filled in for you:</p>
<pre id="yaml">{yaml}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('yaml').textContent)">Copy</button>
<p><small>Installed at <code>{path}</code> ({detail}). Dashboard resource:
{resource}.</small></p>"""


async def handle_index(request):
    # Everything interpolated below is escaped: device names come from the
    # add-on's own config, so an ampersand or angle bracket in one would
    # otherwise render as broken markup (or worse) on this page.
    ingress_path = html.escape(request.headers.get("X-Ingress-Path", ""))

    def row(dev_id, dev):
        connected = device_status[dev_id]["connected"]
        dot_class = "on" if connected else "off"
        dot_title = "connected" if connected else "not connected"
        safe_id = html.escape(dev_id)
        return (
            f"<tr><td><span class='dot {dot_class}' title='{dot_title}'></span>{dot_title}</td>"
            f"<td>{safe_id}</td><td>{html.escape(dev['name'])}</td><td>{html.escape(dev['ip'])}</td>"
            f"<td>{len(subscribers[dev_id])}</td><td>{device_status[dev_id]['fps']}</td>"
            f"<td><code>{ingress_path}/preview?wled={safe_id}</code></td>"
            f"<td><a href='{ingress_path}/device/{safe_id}/' target='_blank'>Open</a></td></tr>"
        )

    rows = "".join(row(dev_id, dev) for dev_id, dev in DEVICES.items())

    if card_status["installed"]:
        slug = request.app.get("slug") or "YOUR_ADDON_SLUG"
        first = next(iter(DEVICES), "1")
        yaml_snippet = (
            "type: custom:wled-gateway-card\n"
            f"addon: {slug}\n"
            f'device: "{first}"'
        )
        card_section = CARD_SECTION.format(
            card_version=html.escape(card_version() or "1"),
            yaml=html.escape(yaml_snippet),
            path=html.escape(card_status["path"] or ""),
            detail=html.escape(card_status["detail"]),
            resource=html.escape(card_status["resource"] or "not registered by the add-on"),
        )
    else:
        card_section = CARD_SECTION_UNAVAILABLE.format(detail=html.escape(card_status["detail"]))

    # Plain substitution rather than str.format: the page embeds JavaScript, and
    # having to double every brace in it is a trap that has bitten this page
    # before.
    page = (
        INDEX_HTML.replace("__INGRESS_PATH__", ingress_path)
        .replace("__ROWS__", rows)
        .replace("__CARD_SECTION__", card_section)
    )
    return web.Response(text=page, content_type="text/html")


async def on_startup(app):
    app["session"] = ClientSession()
    install_lovelace_card()
    if card_status["installed"] and AUTO_REGISTER_CARD:
        await register_lovelace_resource(app)
    app["slug"] = await fetch_own_slug(app)
    app["fps_task"] = asyncio.create_task(measure_frame_rates())
    app["upstream_tasks"] = [
        asyncio.create_task(upstream_loop(app, dev_id, dev["ip"], dev.get("input_select")))
        for dev_id, dev in DEVICES.items()
    ]


async def on_cleanup(app):
    app["fps_task"].cancel()
    for t in app["upstream_tasks"]:
        t.cancel()
    await app["session"].close()


app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_get("/preview", handle_preview)
app.router.add_get("/preview2d", handle_preview2d)
app.router.add_get("/ws/{id}", handle_ws)
app.router.add_get("/json/{id}/live", handle_json_live)
app.router.add_get("/devices", handle_devices)
app.router.add_get("/discover", handle_discover)
app.router.add_post("/discover/add", handle_discover_add)
app.router.add_get("/device/{id}", handle_device_root_redirect)
app.router.add_route("*", "/device/{id}/{path:.*}", handle_device_proxy)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
