# WLED Gateway

WLED's own live-preview WebSocket only streams to whichever client asked for
it most recently — every new viewer steals the feed from the last one. This
add-on holds the one connection each WLED device allows and fans it out to
as many dashboard viewers as connect at once.

Runs with Ingress enabled, so it's reachable through Home Assistant itself
(local IP, local domain, external tunnel) with no separate networking setup.

## Setup

1. Install the add-on, set your devices under **Configuration** (id, name,
   IP for each WLED device), start it.
2. Open its Web UI once to see the actual Ingress URL Supervisor assigned
   (`/api/hassio_ingress/<token>/...`) — that prefix is what your Lovelace
   iframe cards should point at, e.g.:
   `/api/hassio_ingress/<token>/preview?wled=1`
   `/api/hassio_ingress/<token>/preview2d?wled=3&rotate=270`
