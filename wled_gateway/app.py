import asyncio
import html
import json
import logging
import os
import re
import time
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
device_status = {dev_id: {"connected": False, "last_connected_at": None, "bri": None} for dev_id in DEVICES}

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

    function recomputeFactor() {
      if (fixedPercent > 0) {
        factor = Math.min(MAX_FACTOR, (fixedPercent / 100) * gain);
        return;
      }
      const norm = normalize ? 255 / Math.max(deviceBri, 1) : 1;
      factor = Math.min(MAX_FACTOR, BASE_GAIN * gain * norm);
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
          let grad = "linear-gradient(90deg,";
          const offset = (bytes[1] === 2) ? 4 : 2;
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

    function recomputeFactor() {
      if (fixedPercent > 0) {
        factor = Math.min(MAX_FACTOR, (fixedPercent / 100) * gain);
        return;
      }
      const norm = normalize ? 255 / Math.max(deviceBri, 1) : 1;
      factor = Math.min(MAX_FACTOR, BASE_GAIN * gain * norm);
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


async def ha_create_input_select(app, name, options):
    """Create an input_select helper. There's no REST endpoint for this — helper
    creation only exists on the WebSocket API, and it requires an admin token."""
    try:
        async with app["session"].ws_connect(HA_WS, timeout=10) as ws:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                log.warning("unexpected HA websocket greeting: %s", hello.get("type"))
                return False
            await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
            auth = await ws.receive_json(timeout=10)
            if auth.get("type") != "auth_ok":
                log.warning("HA websocket auth failed: %s", auth)
                return False

            await ws.send_json({"id": 1, "type": "input_select/create", "name": name, "options": options})
            while True:
                msg = await ws.receive_json(timeout=10)
                if msg.get("type") == "result" and msg.get("id") == 1:
                    if msg.get("success"):
                        return True
                    error = msg.get("error", {})
                    if error.get("code") == "unauthorized":
                        log.warning(
                            "not allowed to create helpers — create %r by hand, or set it "
                            "explicitly per device with input_select:",
                            name,
                        )
                    else:
                        log.warning("HA refused to create helper %r: %s", name, error)
                    return False
    except Exception as e:
        log.warning("could not create helper %r over the HA websocket: %s", name, e)
        return False


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
                await ws.send_str("{'lv':true}")
                await sync_effect_list(app, dev_id, ip, input_select_entity)
                backoff = 1
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
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
        device_status[dev_id]["connected"] = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def handle_ws(request):
    dev_id = request.match_info["id"]
    if dev_id not in DEVICES:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    subscribers[dev_id].add(ws)
    log.info("client subscribed to %s (now %d)", dev_id, len(subscribers[dev_id]))
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
    body {{ font-family: sans-serif; background: #111; color: #eee; margin: 2em; }}
    h1 {{ font-weight: 500; }}
    code, pre {{ background: #222; padding: 0.3em 0.5em; border-radius: 4px; }}
    pre {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; margin-top: 1em; width: 100%; }}
    th, td {{ text-align: left; padding: 0.4em 0.8em; border-bottom: 1px solid #333; }}
    button {{ margin-left: 0.5em; cursor: pointer; }}
    a {{ color: #7bc9ff; }}
    .dot {{ display: inline-block; width: 0.7em; height: 0.7em; border-radius: 50%; margin-right: 0.4em; }}
    .dot.on {{ background: #4caf50; }}
    .dot.off {{ background: #b33; }}
  </style>
</head>
<body>
  <h1>WLED Gateway</h1>
  <p>This is the base path your Lovelace iframe card URLs need. It's
  detected live from how this page itself was loaded, so it's always
  correct — even after a reinstall changes the Ingress token.</p>
  <pre id="base">{ingress_path}</pre>
  <button onclick="navigator.clipboard.writeText(document.getElementById('base').textContent)">Copy</button>

  <h2>Configured devices</h2>
  <table>
    <tr><th>Status</th><th>ID</th><th>Name</th><th>IP</th><th>Preview card URL</th><th>Device Web UI</th></tr>
    {rows}
  </table>
</body>
</html>
"""


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
            f"<td><code>{ingress_path}/preview?wled={safe_id}</code></td>"
            f"<td><a href='{ingress_path}/device/{safe_id}/' target='_blank'>Open</a></td></tr>"
        )

    rows = "".join(row(dev_id, dev) for dev_id, dev in DEVICES.items())
    return web.Response(
        text=INDEX_HTML.format(ingress_path=ingress_path, rows=rows),
        content_type="text/html",
    )


async def on_startup(app):
    app["session"] = ClientSession()
    app["upstream_tasks"] = [
        asyncio.create_task(upstream_loop(app, dev_id, dev["ip"], dev.get("input_select")))
        for dev_id, dev in DEVICES.items()
    ]


async def on_cleanup(app):
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
app.router.add_get("/device/{id}", handle_device_root_redirect)
app.router.add_route("*", "/device/{id}/{path:.*}", handle_device_proxy)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
