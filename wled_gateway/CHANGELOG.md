# Changelog

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
