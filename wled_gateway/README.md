# WLED Gateway

WLED's own live-preview WebSocket only streams to whichever client asked for
it most recently — every new viewer steals the feed from the last one. This
add-on holds the one connection each WLED device allows and fans it out to
as many dashboard viewers as connect at once (multiple tabs, multiple
people, a wall tablet plus your phone — all at the same time).

Runs with Ingress enabled, so it's reachable through Home Assistant itself —
local IP, local domain, external tunnel — with no separate networking,
reverse proxy, or port forwarding to set up.

See **DOCS.md** (the Documentation tab) for configuration and Lovelace card
examples.
