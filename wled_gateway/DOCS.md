# WLED Gateway — Documentation

## What this solves

WLED devices expose a live-preview WebSocket (`/ws`, with a `{'lv':true}`
handshake) that streams the current LED colors in real time. The catch:
each device only streams to whichever client asked most recently — open the
preview on a second tab or a second device and the first one goes stale.

This add-on sits between your WLED devices and your dashboard: it holds the
one connection each device allows, and re-broadcasts every frame to as many
viewers as are actually connected. It also serves the preview pages
themselves, so nothing needs to live in Home Assistant's own `www/` folder.

## Configuration

Go to the add-on's **Configuration** tab and list your devices:

```yaml
devices:
  - id: "1"
    name: Sasha
    ip: 192.168.111.161
  - id: "2"
    name: Matrix
    ip: 192.168.111.163
```

- `id` — whatever short string you want; it's what you'll reference from
  dashboard card URLs (`?wled=1`). Doesn't need to match anything in Home
  Assistant.
- `name` — for your own reference, not used anywhere functionally yet.
- `ip` — the WLED device's LAN IP.

Restart the add-on after changing the list.

## Finding your Ingress URL

Open the add-on's **Web UI** once. The address bar will show something like:

```
http://<your-ha-host>:8123/api/hassio_ingress/XNz5xsTmUGB2MDuohydZUuYi_FVGtRkiTQt3kFHstI8/
```

The `/api/hassio_ingress/<token>/` part is what your Lovelace cards need —
copy it as your base path for the examples below. This token is assigned
once per install; if you ever uninstall and reinstall the add-on, it
changes and your card URLs need updating.

Because it's a relative, same-origin path, it works identically whether
you're viewing the dashboard on your LAN, through a local domain, or from
outside via an external tunnel — no separate configuration per access
method.

## Endpoints

| Path | What it does |
|---|---|
| `/preview?wled=<id>` | Linear preview — a single gradient bar of all LEDs. Good for LED strips. Optional `&rotate=90\|180\|270` to rotate the bar. |
| `/preview2d?wled=<id>` | 2D preview — a dot-matrix canvas, for matrix/panel devices. |
| `/ws/<id>` | Raw relay WebSocket, if you're building your own frontend instead of using the built-in pages. |
| `/json/<id>/live` | Passthrough to the device's own `/json/live` HTTP endpoint. |
| `/devices` | JSON list of configured devices, for debugging. |

## Lovelace card examples

Replace `INGRESS` below with your actual `/api/hassio_ingress/<token>` path.

**Basic strip preview**, shown only while the light is on:

```yaml
type: conditional
conditions:
  - entity: light.wled
    state: "on"
card:
  type: iframe
  url: INGRESS/preview?wled=1
  aspect_ratio: 5%
  title: null
```

**2D matrix preview:**

```yaml
type: conditional
conditions:
  - entity: light.wled_matrix
    state: "on"
card:
  type: iframe
  url: INGRESS/preview2d?wled=3
  aspect_ratio: 50%
  title: null
```

**Rotated strip** (e.g. a vertical strip mounted on a wall, wired as device
id 4), sized for a tall narrow card via `grid_options`:

```yaml
type: horizontal-stack
cards:
  - type: iframe
    url: INGRESS/preview?wled=4&rotate=270
    title: null
grid_options:
  columns: 1
  rows: 7
```

**Full device card** combining the live preview with Home Assistant's own
WLED integration entities (chips for current draw / LED count / IP,
power/effect/palette controls) — this is the layout used for each device on
a "one card per WLED device" dashboard:

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-title-card
    title: W L E D - 1
    alignment: center
  - type: custom:mushroom-chips-card
    chips:
      - type: entity
        entity: sensor.wled_estimated_current
        icon: mdi:flash-triangle-outline
      - type: entity
        entity: sensor.wled_led_count
        icon: mdi:led-on
      - type: entity
        entity: sensor.wled_ip
        tap_action:
          action: url
          url_path: http://192.168.111.161
    alignment: center
  - type: conditional
    conditions:
      - entity: light.wled
        state: "on"
    card:
      type: iframe
      url: INGRESS/preview?wled=1
      aspect_ratio: 5%
      title: null
  - type: custom:mushroom-select-card
    entity: select.wled_color_palette
    icon: mdi:waveform
```

## Troubleshooting

- **Iframe shows a blank card / "unable to load iframes... http:"** — this
  happens if you hardcode an `http://` address instead of the relative
  Ingress path. Always use the `/api/hassio_ingress/<token>/...` form, never
  a raw container IP:port, so it works over both http and https.
- **Preview never lights up** — check the add-on's log for `upstream
  connected: <id> (<ip>)` on startup. If it's missing or retrying, the
  device's IP in Configuration is wrong or it's unreachable.
- **Card 404s after reinstalling the add-on** — the Ingress token changed;
  grab the new one from the Web UI and update your card URLs.
