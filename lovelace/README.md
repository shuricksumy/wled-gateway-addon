# WLED Gateway card

A Lovelace card that shows the live preview directly, instead of embedding it
in an iframe.

Worth using because of one specific problem: **an iframe on an Ingress URL
can't authenticate itself.** Supervisor requires an `ingress_session` cookie,
only Home Assistant's frontend can create one, and the cookie belongs to a
single origin. So when the Companion app switches between your external URL
and your local IP, every preview card returns `401` until you open the add-on
panel by hand to mint a fresh session.

This card runs inside the frontend, so it creates that session itself and keeps
it alive. Switching networks just re-mints for the new origin. It also looks up
the add-on's Ingress URL, so no token is hardcoded and reinstalling the add-on
doesn't break your cards — which retires the `input_text` +
`config-template-card` workaround entirely.

Rendering into a canvas rather than an iframe also means it can be rotated and
resized like any other card.

---

## Install

1. Copy [`wled-gateway-card.js`](wled-gateway-card.js) to `<config>/www/wled-gateway-card.js`
2. **Settings → Dashboards → Resources → Add resource**
   - URL: `/local/wled-gateway-card.js`
   - Type: **JavaScript module**
3. Reload the page

```yaml
type: custom:wled-gateway-card
addon: 71966d0e_wled_gateway
device: "1"
```

### Finding the add-on slug

It's in the URL of the add-on's page in Home Assistant:

```
http://homeassistant.local:8123/hassio/addon/71966d0e_wled_gateway/info
                                             ^^^^^^^^^^^^^^^^^^^^^
```

The dashed form (`71966d0e-wled-gateway`) is the add-on's *hostname*, and is
easy to grab by mistake — the card accepts either and tries both.

### Updating the card

Browsers cache the file by URL, so replacing it isn't enough. Bump a version
query on the resource each time:

```
/local/wled-gateway-card.js?v=2
```

The loaded version is logged to the browser console at startup, so you can
check which copy you actually got.

---

## Options

| Option | Default | Description |
| --- | --- | --- |
| `addon` | — | Add-on slug. Required, unless you set `ingress_path`. |
| `ingress_path` | — | Use a fixed `/api/hassio_ingress/<token>` instead of looking it up. Only for testing — it breaks when the add-on is reinstalled. |
| `device` | `"1"` | Which device, matching the `id` in the add-on's configuration. |
| `view` | `auto` | `auto` picks per frame, `strip` forces the bar, `matrix` forces the dot grid. |
| `rotate` | `0` | `0`, `90`, `180`, `270`. See [Rotation](#rotation). |
| `fill` | `true` | Fill the card. Combine with `height`/`aspect_ratio` to use those as a *preferred* size that still can't overflow the card. Set `false` for a plain fixed height. |
| `height` | — | Explicit CSS height, e.g. `40px`. Wins over `aspect_ratio` and `fill`. |
| `width` | — | Narrows the preview inside the card, e.g. `10px` for a thin vertical strip. The card keeps its grid size; only the drawing is constrained. |
| `align` | `center` | `center`, `left` or `right`, when `width` is narrower than the card. |
| `aspect_ratio` | — | `16:9`, `2/1`, or a percentage like `5%` (height as a share of width, as the built-in iframe card uses). |
| `title` | — | Card header. Omit for no header. |
| `normalize` | `true` | Scale the preview up as the device dims. See [Brightness](#brightness). |
| `bright` | `0` | Fixed percentage, ignoring the device's brightness. `100` = colours exactly as received. `0` follows the device. |
| `gain` | `1` | Extra multiplier on top of whichever mode is active. |

---

## Rotation

Applied while drawing, not as a CSS transform — so a vertical strip is a
genuinely tall card, rather than a wide one tipped on its side and overlapping
its neighbours.

| `rotate` | Strip | Matrix |
| --- | --- | --- |
| `0` | left → right | as sent |
| `90` | top → bottom | turned clockwise |
| `180` | right → left | upside down |
| `270` | bottom → top | turned anticlockwise |

At `90`/`270` a matrix swaps its width and height, so a 32×16 panel becomes
16×32 and still fits the card.

---

## Sizing

By default the card fills whatever space it's given, so in a sections dashboard
you can just drag its resize handle.

It also suggests a starting size: 1 row for a horizontal strip, 6 rows and 3
columns for a vertical one, 4 rows for a matrix. Anything you set under
`grid_options` in the card config overrides that.

For a fixed shape instead, use `aspect_ratio` (scales with width) or `height`
(absolute). With `fill` left on, those act as a preferred size that's capped at
the card, so a card smaller than the height you asked for shrinks the preview
rather than clipping it — which on a rotated strip would otherwise hide the lit
end and look like a dead card.

---

## Brightness

WLED can send the live view already scaled by the device's brightness, so a
dimmed strip previews dim. By default the card scales it back up, limited by
what the frames actually contain — some setups already send at full scale, and
boosting those would wash the picture out.

If following the device looks uneven, pin it to a constant:

```yaml
bright: 175   # always this, whatever the device is set to
```

`100` renders the colours exactly as received; `175` matches the boost the
preview has always used. Very low brightness will still look grainy — the
device discards that detail before the add-on ever sees it.

---

## Examples

### Strip

```yaml
type: custom:wled-gateway-card
addon: 71966d0e_wled_gateway
device: "1"
height: 10px
```

### Vertical strip

```yaml
type: custom:wled-gateway-card
addon: 71966d0e_wled_gateway
device: "5"
rotate: 270      # first LED at the bottom
height: 400px
width: 10px      # a thin bar rather than the full card width
align: center
```

### Matrix

```yaml
type: custom:wled-gateway-card
addon: 71966d0e_wled_gateway
device: "3"
view: matrix
```

### Two strips around a matrix

```yaml
square: false
type: grid
columns: 1
cards:
  - type: custom:wled-gateway-card
    addon: 71966d0e_wled_gateway
    device: "5"
    height: 10px
  - type: custom:wled-gateway-card
    addon: 71966d0e_wled_gateway
    device: "3"
    view: matrix
  - type: custom:wled-gateway-card
    addon: 71966d0e_wled_gateway
    device: "4"
    height: 10px
```

### Only while the light is on

```yaml
type: conditional
conditions:
  - entity: light.wled_living_room
    state: "on"
card:
  type: custom:wled-gateway-card
  addon: 71966d0e_wled_gateway
  device: "1"
  height: 40px
```

---

## Troubleshooting

- **"Custom element doesn't exist"** — the resource isn't loaded. Check the URL
  under Settings → Dashboards → Resources, that the type is *JavaScript module*,
  and hard-refresh.
- **"Cannot reach the add-on: … does not exist"** — wrong slug. Take it from the
  add-on's page URL.
- **Card is blank, no error** — the `device` id doesn't match any device in the
  add-on's configuration.
- **Changes to the file don't show up** — bump `?v=` on the resource. The
  console logs the version that actually loaded.
- **Stuck or blank in the Companion app** — its webview caches hard, and a
  half-loaded module can wedge the card in a way a page reload won't clear.
  Force-close and reopen the app.
- **`401` in the add-on log** — an iframe card is still pointing at the Ingress
  path somewhere. This card doesn't hit that, so it's a leftover card.
