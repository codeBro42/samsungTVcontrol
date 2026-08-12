# TVHub

Drives a fleet of Samsung Tizen TVs as photo displays, and exposes them to a
home-automation controller over plain HTTP.

One machine on the TVs' subnet runs this server. It serves a full-screen
slideshow web page, and every TV's browser is pointed at that one page. Changing
playlist repoints what that address serves, so the screens follow within about
five seconds without anything being sent to them.

* Web interface for setup, playlists and per-TV control, phone-friendly.
* Plain-text controller API (`GET /tv/lounge/on`) for Loxone and friends: always
  HTTP 200 when the route exists, with the outcome in the body.
* Frame TVs, Wake-on-LAN, pairing, volume, an on-screen remote and a macro
  recorder for firmware that will not cooperate.

---

## The one thing to understand before installing

**Samsung's local API cannot tell a TV to display a picture, and it cannot
reliably tell a TV to open a URL.** Many firmwares accept a "launch the browser
at this URL" command, acknowledge it, and then ignore the URL. That was tested
exhaustively against real hardware, including after a firmware update.

So the architecture is:

1. Each TV's browser homepage is set **once, by hand, with the remote**, to one
   shared address — `http://<this-host>:8899/slideshow/live/all`.
2. That page polls the server every 5 seconds.
3. Switching playlist changes what that address serves. Every screen follows.

Step 1 cannot be automated. The setup wizard says so plainly and gives you the
URL with a copy button and a per-TV "done" checkbox.

Two consequences worth planning for:

* **This host needs a reserved / static IP address**, set *before* you visit the
  TVs. That address gets typed into every TV by hand; if the DHCP lease moves
  afterwards, every screen goes blank and you are walking around with a remote
  again.
* **This host must sit on the TVs' own subnet.** Wake-on-LAN broadcasts, pairing
  and the UPnP volume read-back do not route between subnets.

---

## Requirements

* Python **3.9** or newer.
* Two packages, and nothing else: `websocket-client>=1.6.0`, `requests>=2.31.0`
  (see `requirements.txt`).
* The TVs must have **Smart Hub signed in**. Signed out, a TV reports no apps at
  all and nothing can be launched. `doctor` and the wizard both surface this.

---

## Install: Linux (systemd)

From the directory containing this README:

```sh
sudo ./install.sh
```

That installs the two dependencies system-wide, writes
`/etc/systemd/system/tvhub.service`, enables it at boot, starts it, and then
prints a full diagnosis.

Equivalent by hand:

```sh
sudo python3 -m pip install -r requirements.txt
sudo TVHUB_HOME="$PWD" python3 -m tvhub install
```

The unit runs as **root** and sets `TVHUB_HOME` and `WorkingDirectory` to this
directory. Both matter:

* `WorkingDirectory` is what makes `python3 -m tvhub` importable — systemd would
  otherwise start in `/` and the package would not be on `sys.path`.
* Dependencies must be installed **system-wide, not with `pip install --user`**.
  The unit runs as root and cannot see your user site-packages; the service then
  restarts on `ImportError` every 5 seconds. `doctor` names this exact failure.

The installer does **not** touch your firewall. If firewalld or ufw is running:

```sh
sudo firewall-cmd --add-port=8899/tcp --permanent && sudo firewall-cmd --reload
# or
sudo ufw allow 8899/tcp
```

Day to day:

```sh
systemctl status tvhub
journalctl -u tvhub -f
sudo ./uninstall.sh        # removes the unit; keeps config, state and photos
```

---

## Install: Windows (scheduled task, runs as SYSTEM at boot)

Right-click **`install.bat`** and choose *Run as administrator*, or from an
Administrator PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

That installs the dependencies, registers a scheduled task named `TVHub`, opens
the inbound firewall port, kills any stray process squatting on it, and prints a
diagnosis.

Equivalent by hand, from an Administrator prompt:

```powershell
python -m pip install -r requirements.txt
set TVHUB_HOME=%CD%
python -m tvhub install
```

Notes specific to Windows:

* The task runs as **SYSTEM**, so dependencies installed with
  `pip install --user` under your login are **invisible to it**. Install them
  from an Administrator prompt. This is the failure that once hid fourteen valid
  pairing tokens from a running service.
* `pythonw.exe` is used when present, so there is no console window.
* A leftover process holding the port silently prevents the real service from
  binding, which looks exactly like "the bridge answers nothing". The installer
  clears it and tells you it did.

Day to day:

```powershell
schtasks /Query /TN TVHub
schtasks /End   /TN TVHub          # stop now
schtasks /Run   /TN TVHub          # start now
.\uninstall.ps1                    # keeps config, state and photos
```

---

## Running without installing a service

```sh
python3 -m tvhub run
```

Prints the dashboard URL, the phone URL and the one homepage URL for the TVs.
Handy for a first look; use the service for anything permanent.

---

## First-run setup

Open `http://<this-host>:8899/ui/setup`. With no TVs configured, `/` redirects
there. Six resumable steps:

1. **Server address** — pin `server.base_url` to this host's reserved address.
2. **Find TVs** — scan the subnet, or add a TV by IP (needed for one in standby
   or on another subnet). Rows with Smart Hub signed out are flagged.
3. **Pair** — one TV at a time. Press Pair, then choose **ALLOW** on that TV
   within 90 seconds. Reported as paired only after the pairing is proven by
   effect (the volume is nudged and read back).
4. **Photos** — create a playlist and upload images. Filenames set the running
   order, so prefix `01-`, `02-` if it matters. HEIC/HEIF is rejected with an
   explanation: the TV browser cannot render it.
5. **Homepage** — the manual step described above.
6. **Groups**, and the macro recorder for any TV that failed step 5's test.

---

## Layout on disk

```
<root>/                     root = $TVHUB_HOME, else the folder holding tvhub/
  config.json               settings and the TV roster
  requirements.txt
  tvhub/                    the package
  photos/<playlist>/<image>
  state/
    state.json              pairing tokens, playlist pointers, learned facts
    tvhub.log               2 MB x 3, rotating
    tmp/                    upload staging, cleared at startup
```

State lives **next to the install**, machine-wide, never in `%LOCALAPPDATA%`,
`%APPDATA%` or `~/.config`: a service running as SYSTEM/root and a CLI run by a
logged-in user resolve those per-user paths differently, and that once hid
fourteen valid pairing tokens from the service.

`config.json` is safe to hand-edit. Any key starting with `_` is a comment and is
preserved. Unknown keys are preserved too. Then:

```sh
curl http://localhost:8899/reload
```

A broken edit is refused and the previous config stays live.

---

## Command line

```
python -m tvhub [-v] [--root <path>] <command | path> [args]

run          serve the web interface and the controller API
install      install as a service (needs Administrator / root)
uninstall    remove the service, keeping config, state and photos
doctor       full diagnosis, per TV; exits non-zero when something is wrong
pair         pair TVs one at a time (default: every unpaired one)
scan         find Samsung sets on the network
show         put the slideshow back on screen
playlist     switch the whole fleet, or list playlists
learn        probe one TV and cache what it can do
version, help
```

Any controller path also works as a one-shot, printing exactly what a controller
would receive:

```sh
python -m tvhub tv/lounge/on
python -m tvhub group/downstairs/off
python -m tvhub all/show/holiday
python -m tvhub tv/lounge/keys/KEY_UP,@500,KEY_ENTER
python -m tvhub status
```

**`doctor` is the command that answers "why is that TV blank?"** It reports, per
TV: power, model, Frame flag, pairing and how it was proven, the Smart Hub flag,
the browser app id, the DIAL browser state, the heartbeat age, the resolved
playlist and its image count, and the homepage URL to set.

---

## Controller API

Plain text, one line per TV. `<t>` is `/tv/<alias>`, `/group/<name>` or `/all`.

```
GET /health                     ok
GET <t>/on                      power on / leave art mode, restore the playlist
GET <t>/off                     standby, or Art Mode on a Frame
GET <t>/toggle
GET <t>/wake                    power only, no slideshow
GET <t>/status
GET <t>/show                     resume the current playlist
GET <t>/show/<playlist>
GET <t>/stop                     leave the slideshow
GET <t>/reopen                   force the browser back to its homepage
GET <t>/fullscreen
GET <t>/key/<KEY>                e.g. key/KEY_VOLUP
GET <t>/keys/<seq>               e.g. keys/KEY_UP,@500,KEY_ENTER*2
GET <t>/macro/<name>
GET <t>/app/<app_id>
GET <t>/volume/<0-100|up|down>
GET <t>/mute/<on|off>
GET /playlist/<name>             switch the fleet; returns instantly
GET /playlists
GET /identify/<on|off>           show a big number on each screen
GET /homepages                   the ONE URL to set on every TV
GET /reload
GET /x/<any path above>          fire-and-forget, returns a job id
```

A route that exists returns **200 even when the action failed**, with
`ERROR ...` or `WARNING ...` in the body. Controllers treat a non-200 as a comms
fault and retry, which turns one failed TV into a retry storm. 404 means the
route or the TV does not exist; 400 means a malformed argument.

Group replies are one `[alias] ...` line per member, in alias order.

Access can be restricted with `server.allow_from` (controller routes) and
`server.admin_from` (the web interface and the JSON API). Empty means any source.
Loopback always passes, so a wrong entry can be fixed with a local `/reload`
rather than a service restart. Slideshow routes are never gated — locking the
bridge to a controller must not blank the screens.

There is also a JSON API under `/api/` used by the web interface;
`GET /api/routes` documents it at runtime.

---

## Troubleshooting

Start with `python -m tvhub doctor`.

**A TV reads "not paired" though it was paired before.** Pairing tokens are bound
to `server.client_name`, not to this machine. Changing that string invalidates
every token at once. The tokens are kept, not deleted, so setting the name back
restores them; `doctor` and `/api/status` say which name they were granted under.

**A TV is "on" but nothing is on screen.** "Playing" requires the TV to have
*recently fetched the page from us* — that is the only real evidence. Tizen
freezes a backgrounded page's timers while still reporting the browser as
running, so "browser running" proves nothing. Try `<t>/reopen`, then check the
homepage really is set on that TV.

**A Frame TV reports "on" when it looks off.** It is. A Frame does not power off:
`PowerState` reads `on` in both art mode and normal use. Its "off" is Art Mode,
which this server sets explicitly and confirms.

**Wake-on-LAN does nothing.** It does not route between subnets and is unreliable
over Wi-Fi. The TV also needs its MAC in `config.json`. In normal standby, power
on usually works through the paired control channel instead — but a TV has to be
paired first.

**The browser will not open on one TV.** Some firmware exposes no launchable
browser at all. Record an open macro on that TV's page (`/ui/tv/<alias>`). A
macro is dead reckoning across the TV's home screen: it differs between model
generations and needs re-recording after a firmware update that reshuffles the
menus.

**Photos rejected on upload.** HEIC/HEIF cannot render in the TV browser —
convert to JPEG. Detection is by content, so renaming a `.heic` to `.jpg` does not
help.

---

## Development notes

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
TVHUB_HOME=/tmp/tvhub-dev python -m tvhub run
```

Module layout and the strictly one-way dependency direction:

```
store  <-  samsung  <-  fleet
store  <-  ui  <-  slideshow
store, samsung, fleet, slideshow, ui  <-  webapp  <-  service
```

* `store` — paths, config, state, heartbeat, activity, jobs. Imports nothing
  from the package and no third-party module.
* `samsung` — the wire protocols, and the **only** module allowed to import
  `requests` or `websocket-client`.
* `fleet` — the product logic: one `Tv` per display, plus the roster.
* `slideshow` — photo library, playlist pointers, manifest, and the TV page.
* `ui` — templates and assets under `tvhub/web/`.
* `webapp` — the HTTP surface. `App.handle(Request) -> Response` touches no
  socket, so the whole route table is testable without a network.
* `service` — the CLI, the service installers, and the wiring.

Two version markers must be bumped by hand when the relevant files change:
`ui.ASSET_VERSION` for anything under `tvhub/web/`, and
`slideshow.PAGE_VERSION` for `slideshow.html` — the latter is what makes a TV
already sitting on the page reload itself. `page_version()` also mixes in a hash
of the on-disk template, so an edit still propagates if the constant is
forgotten.

The comments in these files record measurements against real hardware, not
opinions. A "simplification" that removes one generally reintroduces a bug that
took days to find. `samsung.py` and `fleet.py` are the two worth reading before
changing anything.
