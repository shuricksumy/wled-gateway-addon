# Changelog

## 1.13.1
- Fix cards losing their settings when the editor was opened — on a copy to
  another dashboard, or simply opening the same card a second time. Home
  Assistant can hand the editor its `hass` object before the card's config, and
  the editor was filling in the add-on against the empty config it starts with,
  emitting that as the card's new configuration. Everything else was already
  gone by the time the form appeared. The editor now does nothing at all until
  the real config arrives.

## 1.13.0
- Each device now has a `binary_sensor` showing whether it's reachable, named
  after the device — `binary_sensor.wled_gateway_sasha` — with viewers, frame
  rate, brightness and whether it's streaming as attributes. Enough to notify
  yourself when a strip drops off. `publish_status_entities` turns it off.
  These are set directly rather than by an integration, which is the only route
  open to an add-on: they're lost when Home Assistant restarts, so they're
  refreshed every minute.
- Card: frames are drawn once per animation frame rather than once per message.
  A device sending faster than the screen refreshes no longer causes work that
  is immediately thrown away — measuring and scaling now happen only for frames
  that actually get drawn.

## 1.12.0
- An unreachable device now looks unreachable. Previews sat on their last frame
  when a device dropped off, which is indistinguishable from a strip that
  simply isn't changing. The gateway tells viewers when a device connects or
  goes away — they can't tell otherwise, since their own connection is to the
  add-on and stays up either way — and the card dims the stale frame and says
  so. New viewers are told the current state as they connect.

## 1.11.1
- Fix a card added from the editor saving without its type, which Home
  Assistant then rejected as "No type provided". Filling in the add-on on a
  blank config rebuilt the config from the form's own fields, and the type
  isn't one of them.

## 1.11.0
- Add devices without typing an address. Home Assistant already knows every
  WLED device and where it lives, so the add-on's page now lists them, marks
  the ones already set up, and adds the rest in one click — writing its own
  configuration and restarting to pick them up.
- The page also shows what each device is doing: how many viewers are watching
  and the frame rate it's actually delivering. A device sitting at 0 FPS with
  no viewers is idle on purpose, not broken.
- Card: tapping a preview opens that device's own WLED page, with `tap_action`
  to change or disable it.
- Card: `from`/`to` to preview part of a strip, for a run split into segments.
- Card: reports its size to older Home Assistant versions too, so the native
  layout controls appear regardless of version.

## 1.10.1
- The editor fills in the add-on for any card that doesn't name one, not just
  cards added from the picker — so a card pasted as YAML, or written before the
  editor existed, no longer needs the slug looked up by hand. Configs that
  already name an add-on, or use `ingress_path`, are left alone.

## 1.10.0
- The card now has a visual editor, so it can be configured from the UI like
  any built-in card instead of only in YAML — with the shape, layout, ring and
  brightness settings grouped rather than presented as one long list.
- Picking it from the card list fills in the add-on and its first device
  automatically, so the preview shows something real straight away, and the
  device is chosen from a dropdown of your actual devices by name.

## 1.9.0
- The card is now registered as a dashboard resource for you, so there's
  nothing left to do by hand: install the add-on, and the card is available.
  An entry you added yourself is adopted rather than duplicated.
- The `?v=` on that resource is kept in step with the card, so an add-on update
  no longer needs a manual cache-bust to take effect.
- New `auto_register_card` option (default on) to turn both off. Registration
  needs storage-mode dashboards and an admin token; where neither holds, the
  add-on's page says which applies and what to add by hand, and changes
  nothing.

## 1.8.0
- The Lovelace card now ships with the add-on and is installed into
  `<config>/www` on startup, so it no longer has to be copied by hand and can't
  drift out of step with the add-on. If the config folder isn't writable the
  add-on says so on its page and carries on — nothing else depends on it.
- The add-on's page now has a **Lovelace card** section with the resource URL
  to register (versioned, so cache-busting is just copying it again) and a
  ready-made card with this add-on's slug already filled in — no more reading
  the slug out of a browser URL.

## 1.7.0
- Devices no longer stream their live view around the clock. Every configured
  device was asked for frames on connect and kept sending them forever, whether
  or not any dashboard was open — constant WiFi traffic and work for each ESP
  for nothing. Live view is now turned on when the first viewer arrives and off
  ten seconds after the last one leaves, with the socket kept open throughout
  for state updates. Moving between dashboards doesn't interrupt the stream.

## 1.6.3
- Fix dimmed previews washing out to pale, desaturated colour. The boost
  assumed the live view is always scaled by the device's brightness, but WLED
  restores pixel colour when it reads the strip back, so on some setups frames
  already arrive at full scale — dividing by brightness on top of that drove
  even the background to maximum. The boost is now limited by the peak
  actually present in recent frames, so a feed that already reaches 255 is left
  alone no matter what brightness is reported, while a feed that really is
  dimmed is still scaled back up. A genuinely dark scene at full brightness is
  left dark too.
- `/devices` reports `frame_peak` alongside `bri`, which is what makes the two
  cases distinguishable.

## 1.6.2
- Fix dimmed previews looking washed out, with the dark areas lifted and the
  whole image low-contrast. The boost was capped at 8x, but a strip at 10%
  needs about 18x to be scaled back to full — so the preview stayed dim, and a
  dim image reads as flat. The cap is now high enough to finish the job: a
  dimmed strip renders essentially identically to the same scene at full
  brightness. Speckle is still held off by the noise floor, which is what
  actually controls it.

## 1.6.1
- Fix the preview looking flat at high brightness. 1.6.0 dropped the 175%
  boost the preview had always applied, so at full brightness — where
  normalising multiplies by 1 — it rendered noticeably duller than before.
  That boost is back as the baseline.
- Fix "snow": a dimmed strip previewed as drifting speckle. Normalising with a
  CSS filter scaled every pixel, so 1-2 count quantisation remnants got
  amplified into visible dots. Scaling now happens per pixel with a noise
  floor, so near-black stays black.
- Scale each pixel as a whole rather than per channel, so boosting can't shift
  hue when one channel saturates before the others.
- New per-device **Fixed preview brightness %** (and `&bright=<pct>` per card)
  to pin the preview to a constant and ignore the device's brightness
  altogether — `100` shows the colours exactly as received.
- Boost cap lowered from 10x to 8x, past which dim frames were mostly
  amplified noise.

## 1.6.0
- Previews no longer dim along with the device. WLED sends the live view
  already scaled by master brightness, so a strip at 20% previewed at 20% —
  nearly invisible on a dashboard. The add-on now reads the device's reported
  brightness and scales the preview back up, preserving colour rather than
  just brightening everything.
- New per-device **Preview at full brightness** setting (on by default), with
  `&normalize=0|1` on a card URL to override it per card, and `&gain=<n>` for
  an extra multiplier.
- Brightness is read from the state WLED already pushes on the live-view
  socket — no extra polling — and is sent to viewers as they connect, so a
  card opened between changes renders correctly straight away.
- Removed the old hardcoded 175% brightness filter on the 1D preview, which
  boosted everything regardless of the device.
- `/devices` now reports each device's current brightness, for debugging.

## 1.5.1
- The Configuration tab now reads as English instead of raw schema keys:
  `auto_create_helpers` and every field in the Add-device dialog (ID, Name,
  IP address, Effect dropdown) has a proper label and an explanation.
- The effect dropdown field spells out that it should normally be left empty,
  since one is created for you from the device ID.

## 1.5.0
- Effect helpers are now created for you. Adding a device no longer means
  hand-creating a matching `input_select` and typing its entity id into the
  config: each device defaults to `input_select.wled_effect_<id>` (device `6`
  → `input_select.wled_effect_6`) and the helper is created, already populated
  with that device's real effect list, on first connect.
- `input_select` per device is now optional — set it only to point at a helper
  you already have under a different name. Existing helpers are never
  recreated or overwritten, just kept in sync as before.
- New `auto_create_helpers` option (default `true`) to turn helper creation
  off; with it off, only devices with an explicit `input_select` are synced.
- Skip syncing when a device reports no usable effects, instead of pushing an
  empty list that Home Assistant rejects.

## 1.4.2
- Maintenance: upgrade the CI actions, which had drifted a full major behind
  and were running on a deprecated Node runtime that GitHub was force-migrating
  on every build. No functional change to the add-on itself; the image is
  rebuilt, so it also picks up current base-image and dependency patches.

## 1.4.1
- Add a proper `icon.png` and `logo.png`, so the add-on shows its own
  artwork in the Supervisor panel and add-on store instead of the generic
  placeholder. Generated by `tools/make_branding.py` — edit a constant there
  and re-run rather than opening an image editor.
- Trim the Docker build context: only `app.py` is copied into the image, so
  screenshots, docs and branding no longer get uploaded on every build.

## 1.4.0
- Fix the live feed dropping for **everyone** whenever a viewer opened or
  closed a preview. Connecting or disconnecting mutated the subscriber set
  while a frame was mid-broadcast, which raised an error that looked like a
  dead device and tore down the upstream connection — so the add-on
  reconnected (and every other viewer froze for a second) on each tab open
  or close. Frames are now broadcast over a snapshot of the subscriber list.
- Add a Supervisor `watchdog` so the add-on is actually restarted when it
  stops responding. The Docker `HEALTHCHECK` added in 1.3.0 only marks the
  container unhealthy — Supervisor doesn't act on it, so it never delivered
  the automatic restart that entry described. The healthcheck is kept for
  running the image directly outside Home Assistant.
- Escape device names, IPs and the Ingress path on the info page, so a name
  containing `&` or `<` no longer breaks the page markup.
- Drop WLED's reserved `RSVD` placeholder from synced effect lists entirely
  instead of leaving one of them in the dropdown.
- Pin `aiohttp` below 4.0 so a future major release can't silently change
  behavior on the next image rebuild.

## 1.3.0
- Add a reverse proxy for each device's own admin web UI at `/device/<id>/`,
  so it can be opened or embedded directly from Home Assistant for setup
  and debugging — no need to separately find the device's raw IP. Linked
  from the info page; see the README for an iframe card example.
- Info page now shows live connected/not-connected status per device, not
  just static config.
- Add a Docker `HEALTHCHECK` so Supervisor can detect and restart the
  add-on if it stops responding.

## 1.2.1
- Fix a literal `\n` (backslash-n text, not an actual newline) leaking into
  the info page as visible text before the device table.

## 1.2.0
- Add a dynamic info page at `/` showing the current Ingress base path
  (with a copy button) and a ready-made preview URL per configured device.

## 1.1.1
- Fix effect list sync failing with `Duplicated options` — WLED pads unused
  effect IDs with repeated `"RSVD"` placeholder entries, which
  `input_select.set_options` rejects. Now deduplicated before syncing.

## 1.1.0
- Add-on now syncs each device's real, live effect list directly into a
  configured `input_select` via Home Assistant's own API
  (`homeassistant_api: true`), replacing the separate HA-side automation
  that used to do this.

## 1.0.0
- Initial release: Ingress-based live-preview fan-out for WLED devices.
  Holds the one live-view connection each device allows and re-broadcasts
  it to multiple simultaneous dashboard viewers.
