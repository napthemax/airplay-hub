#!/usr/bin/env python3
"""
Web interface for the phone — same rooms, same buttons, in your pocket.

    python webui.py            run in the foreground, stop with Ctrl-C
    python webui.py --port 80  a different port

The server listens on every interface but **only admits clients from private
networks** (10/8, 172.16/12, 192.168/16, link-local and localhost). It is meant
to be reached from the sofa, not from the internet. There is no login, just like
Volumio's and OwnTone's own web interfaces — the protection is that it cannot be
reached from outside. Never set up a port forward to this port.

Standard library only. Shares rooms.py with the window, so both can be used at
once: all state is read from PipeWire and OwnTone, nothing is held in memory.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import bridge
import pwhub
import rooms

PORT = 8730


def _private(address: str) -> bool:
    """Home network clients only. Everything else gets a 403."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def local_addresses() -> list[str]:
    """Addresses the server can be reached on, for printing at startup."""
    hits: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # does not connect, just picks a route
        hits.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return hits or ["127.0.0.1"]


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1b2733">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>AirPlay Hub</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
    background: #1b2733; color: #ecf0f1;
    font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header { padding: 18px 18px 10px; }
  h1 { margin: 0; font-size: 22px; }
  #status { color: #8fa3b8; font-size: 13px; margin-top: 4px; }
  .master {
    margin: 6px 18px 14px; padding: 12px 14px;
    background: #22303f; border-radius: 12px;
  }
  .master label { color: #8fa3b8; font-size: 12px; letter-spacing: .5px; }
  .section {
    color: #6f8299; font-size: 11px; font-weight: 700; letter-spacing: 1px;
    padding: 4px 18px;
  }
  ul { list-style: none; margin: 0; padding: 0 10px 24px; }
  li {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 8px; border-radius: 12px;
  }
  li + li { margin-top: 2px; }
  .speaker {
    flex: 0 0 auto; width: 46px; height: 40px; border: 0; border-radius: 9px;
    background: #2b3d4f; color: #7f95ab; font-size: 19px;
  }
  .speaker.on { background: #2f8fd8; color: #fff; }
  .speaker:disabled { background: #22303f; color: #3f5163; }
  .body { flex: 1 1 auto; min-width: 0; }
  .name { font-size: 16px; }
  li.gone .name { color: #55697d; }
  .tag { color: #55697d; font-size: 12px; }
  input[type=range] {
    -webkit-appearance: none; appearance: none; width: 100%;
    height: 30px; background: transparent; margin: 2px 0 0;
  }
  input[type=range]::-webkit-slider-runnable-track {
    height: 5px; border-radius: 3px; background: #2b3d4f;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 24px; height: 24px; margin-top: -10px;
    border-radius: 50%; background: #dbe6f0;
  }
  input[type=range]::-moz-range-track { height: 5px; border-radius: 3px; background: #2b3d4f; }
  input[type=range]::-moz-range-thumb { width: 24px; height: 24px; border: 0; border-radius: 50%; background: #dbe6f0; }
  input[type=range]:disabled::-webkit-slider-thumb { background: #55697d; }
  .pct { flex: 0 0 auto; width: 44px; text-align: right; color: #8fa3b8; font-size: 13px; }
  #offline {
    display: none; margin: 0 18px 14px; padding: 10px 14px;
    background: #4a2f2f; color: #f0c8c8; border-radius: 10px; font-size: 13px;
  }
</style>
</head>
<body>
<header>
  <h1>AirPlay Hub</h1>
  <div id="status">Loading…</div>
</header>
<div id="offline">No contact with the server. Is the machine awake?</div>
<div class="master">
  <label>ALL ROOMS</label>
  <input type="range" id="master" min="0" max="100" value="100">
</div>
<div class="section">ROOMS</div>
<ul id="rooms"></ul>

<script>
let dragging = false;

async function api(path, body) {
  const opt = body ? {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  } : {};
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

// Rows are built once and then updated in place. Tearing the list down and
// rebuilding it every two seconds meant a tap could land on an element that had
// just been replaced - the tap vanished, and on a phone that feels like the app
// is hanging.
const rows = new Map();

function makeRow(room) {
  const li = document.createElement("li");

  const btn = document.createElement("button");
  btn.className = "speaker";
  btn.textContent = "\u266a";
  btn.onclick = async () => {
    const row = rows.get(room.key);
    const wanted = !row.data.on;
    btn.classList.toggle("on", wanted);   // respond at once, confirm after
    try { render(await api(`/api/rooms/${encodeURIComponent(room.key)}/on`, {on: wanted})); }
    catch (e) { lost(); }
  };
  li.appendChild(btn);

  const body = document.createElement("div");
  body.className = "body";
  const name = document.createElement("div");
  name.className = "name";
  body.appendChild(name);

  const slider = document.createElement("input");
  slider.type = "range"; slider.min = 0; slider.max = 100;
  slider.oninput = () => { dragging = true; pct.textContent = slider.value + "%"; };
  slider.onchange = async () => {
    dragging = false;
    try { await api(`/api/rooms/${encodeURIComponent(room.key)}/volume`, {volume: +slider.value}); }
    catch (e) { lost(); }
  };
  body.appendChild(slider);

  const tag = document.createElement("div");
  tag.className = "tag";
  tag.textContent = "not answering";
  body.appendChild(tag);
  li.appendChild(body);

  const pct = document.createElement("div");
  pct.className = "pct";
  li.appendChild(pct);

  return {li, btn, name, slider, tag, pct, data: room};
}

function updateRow(row, room) {
  row.data = room;
  row.li.className = room.reachable ? "" : "gone";
  row.name.textContent = room.name;
  row.btn.classList.toggle("on", room.on);
  row.btn.disabled = !room.reachable;
  row.slider.style.display = room.reachable ? "" : "none";
  row.tag.style.display = room.reachable ? "none" : "";
  row.pct.textContent = room.reachable ? room.volume + "%" : "";
  if (!dragging && document.activeElement !== row.slider) {
    row.slider.value = room.volume;
  }
}

function render(data) {
  document.getElementById("offline").style.display = "none";
  const playing = data.rooms.filter(r => r.on).length;
  const gone = data.rooms.filter(r => !r.reachable).length;
  let text = `${data.rooms.length - gone} rooms · ${playing} playing`;
  if (gone) text += ` · ${gone} not answering`;
  if (data.warning) text += ` · ${data.warning}`;
  document.getElementById("status").textContent = text;

  const master = document.getElementById("master");
  if (!dragging && document.activeElement !== master) master.value = data.master;

  const ul = document.getElementById("rooms");
  const present = new Set();
  for (const room of data.rooms) {
    present.add(room.key);
    let row = rows.get(room.key);
    if (!row) {
      row = makeRow(room);
      rows.set(room.key, row);
      ul.appendChild(row.li);
    }
    updateRow(row, room);
  }
  // Rooms gone from the response entirely (never seen this session).
  for (const [key, row] of rows) {
    if (!present.has(key)) { row.li.remove(); rows.delete(key); }
  }
  // Keep the same order the server sent.
  data.rooms.forEach(room => ul.appendChild(rows.get(room.key).li));
}

function lost() {
  document.getElementById("offline").style.display = "block";
}

document.getElementById("master").onchange = async (e) => {
  dragging = false;
  try { await api("/api/master", {volume: +e.target.value}); } catch (err) { lost(); }
};
document.getElementById("master").oninput = () => { dragging = true; };

async function tick() {
  if (!dragging) {
    try { render(await api("/api/rooms")); } catch (e) { lost(); }
  }
  setTimeout(tick, 2000);
}
tick();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AirPlayHub"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass   # quiet; the journal should not fill with every two-second poll

    # --------------------------------------------------------------- helpers
    def _reject_outsider(self) -> bool:
        if _private(self.client_address[0]):
            return False
        self._send(403, {"error": "home network only"})
        return True

    def _send(self, code: int, data: dict | None = None, html: str | None = None) -> None:
        if html is not None:
            body = html.encode()
            kind = "text/html; charset=utf-8"
        else:
            body = json.dumps(data or {}).encode()
            kind = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return {}

    def _state(self) -> dict:
        current = rooms.list_rooms()
        warning = ""
        if rooms.any_owntone_on(current):
            if not bridge.is_running():
                warning = "NO AUDIO GOING OUT"
        try:
            master = next(
                (s.volume_pct for s in pwhub.list_sinks() if s.name == pwhub.HUB_SINK), 100
            )
        except pwhub.PactlError:
            master = 100
        return {
            "master": master,
            "warning": warning,
            "rooms": [
                {
                    "key": r.key,
                    "name": r.name,
                    "on": r.on,
                    "volume": r.volume,
                    "reachable": r.reachable,
                    "engine": r.engine,
                }
                for r in current
            ],
        }

    # --------------------------------------------------------------- routing
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self._reject_outsider():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, html=PAGE)
        elif path == "/api/rooms":
            self._send(200, self._state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if self._reject_outsider():
            return
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/master":
            try:
                pwhub.set_sink_volume(pwhub.HUB_SINK, int(body.get("volume", 100)))
            except (pwhub.PactlError, ValueError) as exc:
                self._send(500, {"error": str(exc)})
                return
            self._send(200, self._state())
            return

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms":
            # Room keys contain spaces ("homepod bedroom"), so the path arrives
            # percent-encoded from the browser and has to be decoded here.
            self._room(unquote(parts[2]), parts[3], body)
            return
        self._send(404, {"error": "not found"})

    def _room(self, key: str, action: str, body: dict) -> None:
        room = next((r for r in rooms.list_rooms() if r.key == key), None)
        if room is None:
            self._send(404, {"error": "unknown room"})
            return
        try:
            if action == "on":
                on = bool(body.get("on"))
                if on:
                    rooms.ensure_hub()
                rooms.set_on(room, on)
                rooms.sync_stream()
            elif action == "volume":
                rooms.set_volume(room, int(body.get("volume", 0)))
            else:
                self._send(404, {"error": "unknown action"})
                return
        except ConnectionError as exc:
            self._send(409, {"error": str(exc)})
            return
        except Exception as exc:   # a backend failure must not take the server down
            self._send(500, {"error": str(exc)})
            return
        self._send(200, self._state())


def main() -> int:
    p = argparse.ArgumentParser(description="Web interface for AirPlay Hub")
    p.add_argument("--port", type=int, default=PORT)
    args = p.parse_args()

    server = ThreadingHTTPServer(("", args.port), Handler)
    server.daemon_threads = True
    # flush, or the lines sit in the buffer when the server runs as a systemd
    # service and the journal looks empty until the process dies.
    print("AirPlay Hub — web interface", flush=True)
    for address in local_addresses():
        print(f"  http://{address}:{args.port}", flush=True)
    print("Home network clients only. Stop with Ctrl-C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
