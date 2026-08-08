import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wled-gateway")

OPTIONS_PATH = Path("/data/options.json")
HA_API = "http://supervisor/core/api"
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


def load_devices():
    options = json.loads(OPTIONS_PATH.read_text())
    devices = {}
    for dev in options.get("devices", []):
        dev_id = str(dev["id"])
        devices[dev_id] = {
            "name": dev.get("name", dev_id),
            "ip": dev["ip"],
            "input_select": dev.get("input_select"),
        }
    return devices


DEVICES = load_devices()
subscribers = {dev_id: set() for dev_id in DEVICES}

PREVIEW_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1,minimum-scale=1">
  <meta charset="utf-8">
  <meta name="theme-color" content="#222222">
  <title>WLED Live Preview</title>
  <style>
  html, body { margin: 0; background: #000; overflow: hidden; width: 100%; height: 100%; }
  #canv { position: absolute; transform-origin: center center; background: #000; filter: brightness(175%); }
  * { box-sizing: border-box; }
  </style>
  <script>
    function getUrlParameter(name, defaultVal = null) {
      const urlParams = new URLSearchParams(window.location.search);
      return urlParams.get(name) || defaultVal;
    }
    var wledId = getUrlParameter('wled', '1');
    var rotate = parseInt(getUrlParameter('rotate', '0'));

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
          if (Object.prototype.toString.call(event.data) !== "[object ArrayBuffer]") return;
          const bytes = new Uint8Array(event.data);
          if (bytes[0] !== 76) return;
          let grad = "linear-gradient(90deg,";
          const offset = (bytes[1] === 2) ? 4 : 2;
          for (let i = offset; i < bytes.length; i += 3) {
            grad += `rgb(${bytes[i]},${bytes[i + 1]},${bytes[i + 2]})`;
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
          if (Object.prototype.toString.call(event.data) !== "[object ArrayBuffer]") return;
          const bytes = new Uint8Array(event.data);
          if (bytes[0] !== 76 || bytes[1] !== 2) return;
          const cols = bytes[2], rows = bytes[3];
          const scale = Math.min(c.width / cols, c.height / rows);
          const xOffset = Math.floor((c.width - scale * cols) / 2);
          let i = 4;
          for (let y = 0.5; y < rows; y++) {
            for (let x = 0.5; x < cols; x++) {
              ctx.fillStyle = `rgb(${bytes[i]},${bytes[i + 1]},${bytes[i + 2]})`;
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
    # duplicates, which input_select.set_options rejects outright. Dedupe,
    # keeping first-occurrence order.
    effects = list(dict.fromkeys(effects))

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


async def upstream_loop(app, dev_id, ip, input_select_entity):
    backoff = 1
    while True:
        try:
            async with app["session"].ws_connect(f"ws://{ip}/ws", heartbeat=20) as ws:
                log.info("upstream connected: %s (%s)", dev_id, ip)
                await ws.send_str("{'lv':true}")
                await sync_effect_list(app, dev_id, ip, input_select_entity)
                backoff = 1
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        dead = []
                        for client in subscribers[dev_id]:
                            try:
                                await client.send_bytes(msg.data)
                            except Exception:
                                dead.append(client)
                        for c in dead:
                            subscribers[dev_id].discard(c)
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
        except Exception as e:
            log.warning("upstream %s (%s) dropped: %s", dev_id, ip, e)
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


async def handle_preview(request):
    if request.query.get("wled", "1") not in DEVICES:
        raise web.HTTPNotFound()
    return web.Response(text=PREVIEW_HTML, content_type="text/html")


async def handle_preview2d(request):
    if request.query.get("wled", "1") not in DEVICES:
        raise web.HTTPNotFound()
    return web.Response(text=PREVIEW2D_HTML, content_type="text/html")


async def handle_devices(request):
    return web.json_response(DEVICES)


async def handle_index(request):
    raise web.HTTPFound("preview?wled=1")


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
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
