# Local development

Testing changes through Supervisor every time (install/update/restart) is
slow. Run the add-on directly against your real WLED devices instead:

```bash
cd wled_gateway
cp dev-options.example.json dev-options.json   # edit with your real device IPs
docker build -t wled-gateway-dev .
docker run --rm -p 8099:8099 \
  -v "$(pwd)/dev-options.json:/data/options.json:ro" \
  wled-gateway-dev
```

Then open `http://localhost:8099/` — same info page, same `/preview`,
`/preview2d`, `/device/<id>/` routes, all reachable directly without HA or
Ingress in the loop at all.

Notes:
- `homeassistant_api`-dependent behavior (effect list sync via
  `input_select.set_options`) won't work standalone — there's no
  `SUPERVISOR_TOKEN` outside a real Supervisor-managed container, and the
  code logs a warning and skips it rather than failing. Everything else
  (previews, device UI proxy, live status) works fully standalone.
- `dev-options.json` is gitignored — real IPs never need to touch a commit.

Once a change looks right locally, bump `version` in `config.json`, add a
`CHANGELOG.md` entry, commit, and push — CI validates and publishes the
real multi-arch image from there. The version bump isn't optional: images
are tagged with it, so CI fails if `CHANGELOG.md` has no entry for the
current version, rather than silently overwriting an already-published tag.

## Regenerating the icon and logo

`icon.png` and `logo.png` are generated, not hand-drawn, so the artwork can
be adjusted by editing values instead of an image editor:

```bash
pip install pillow
python3 tools/make_branding.py
```

Colors, LED count and proportions are constants near the top of
[`tools/make_branding.py`](tools/make_branding.py). Home Assistant requires
the icon to be square and picks both files up by filename from the add-on
folder.
