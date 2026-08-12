# samsungTVcontrol

Control a fleet of Samsung Tizen TVs over plain IP — power, and a photo slideshow
on every screen — from a small Python service running on one Windows PC.

Built for a 14-TV house. Anything that can open a socket can drive it: a browser,
`curl`, Loxone, Crestron, Q-SYS, a batch file.

```bash
curl "http://192.168.1.246:8899/home/on"          # every TV on, slideshow up
curl "http://192.168.1.246:8899/office/off"        # one room off
curl "http://192.168.1.246:8899/playlist/holiday"  # change what they all show
```

There is also a web dashboard at `http://<pc>:8899/dashboard` — live per-TV status,
group buttons, a per-TV on-screen remote, playlist picker, and add/remove/pair.

---

## How it works, and why

Samsung's local API lets you send remote-control keys and read power state. It does
**not** let you tell a TV to display a picture. So:

1. The PC serves a **full-screen slideshow web page** from a folder of photos.
2. Each TV's built-in browser is pointed at that page and left there.
3. Switching playlists **repoints one shared URL** rather than renavigating, so
   every screen follows within ~5 seconds without touching a TV.

That last point is the whole design. Every TV's browser homepage is the *same*
address — `/slideshow/live/all` — because a lot of Samsung firmware accepts a
"launch the browser at this URL" command and then silently ignores the URL. Give
the browser one fixed homepage and change what that address serves instead, and
the firmware limitation stops mattering.

**Requirements**

- A Windows PC on the **same subnet** as the TVs, with a **reserved/static IP**
  (its address is baked into each TV's homepage). Python 3.9+.
- Samsung Tizen TVs, each **signed in to Smart Hub** — without that a TV reports
  no apps at all and nothing can be launched.

---

## Install

On the Windows PC, from an **Administrator** command prompt:

```
install.bat
```

That installs the Python packages, opens ports 8899/8900 in the firewall, and
registers a scheduled task (`SamsungTVBridge`) that runs the service as SYSTEM at
boot — so it comes up before anyone logs in.

Then edit `config.json` and set `base_url` to this PC's fixed address:

```json
"base_url": "http://192.168.1.246:8899"
```

Check it:

```
py tvbridge.py doctor
```

`uninstall.bat` removes the task and firewall rules.

> Deploying an update later: copy `tvbridge.py` over and re-run
> `setup_task.ps1`, which stops any old copy and restarts the service.

---

## Add and pair the TVs

**1. Find them.** Turn the TVs on first — a set in standby will not answer.

```
discover.bat
```

Lists every Samsung TV on the subnet, flags any not yet in `config.json`, and
prints a paste-ready config block. The dashboard does the same under
**Manage TVs → Scan**, with an **Add** button per new TV.

**2. Pair them, one at a time.**

```
pair.bat
```

Goes TV by TV, waiting for you to press Enter at each screen, then holds the Allow
prompt open while you accept it with the remote. It **verifies** each one by
sending a real command and confirming the TV obeyed — a stored token is not proof
of pairing, because a TV will complete the handshake with a bad token and then
answer `No Authorized` to everything.

Or from the dashboard: **Manage → 3 (pair now)**.

**3. Set the browser homepage on each TV.** This is the one step that cannot be
automated. On each TV, open **Internet**, go to:

```
http://<pc>:8899/slideshow/live/all
```

and set it as the homepage (browser menu → Settings → Homepage → Current Page).
`curl http://<pc>:8899/homepages` prints the exact URL.

It is genuinely one-off: the address never changes, and playlist switching works
by repointing it.

**4. Frames may need a key macro.** Some firmware will not launch its browser over
the network at all. If `on` reports `WARNING ran the key macro but the TV never
requested the page`, record the button presses that open Internet on that set:

```
learn.bat <alias>
```

Send keys one at a time while watching the screen, press `d` when it looks right,
and paste the printed macro into that TV's `photos.open_macro`.

---

## Photos

One folder per playlist under `windows\photos\`:

```
photos\default\      ->  /playlist/default
photos\dream-home\   ->  /playlist/dream-home
```

Drop in `.jpg`, `.png`, `.webp`, `.gif` or `.bmp`. **HEIC will not display** —
convert first. Filenames set the running order, so prefix with `01-`, `02-` if it
matters. Resize to 3840px on the long edge; anything larger only slows the TV's
browser down. New files are picked up within a minute, no restart.

See [UPLOAD-PHOTOS.md](windows/UPLOAD-PHOTOS.md) for a copy-paste procedure,
including HEIC conversion.

---

## Everyday use

| Command | What it does |
|---|---|
| `/<target>/on` | power on, open the browser, start the slideshow, go fullscreen |
| `/<target>/off` | standby — or Art Mode on a Frame, which is its off state |
| `/<target>/status` | power, model, whether it is paired |
| `/playlist/<name>` | change what **every** TV shows |
| `/<tv>/photos/<name>` | change one TV |
| `/<tv>/key/KEY_HOME` | any remote key |
| `/<tv>/reopen` | force the browser back to its homepage |
| `/fullscreen` | keypress every TV so its page goes fullscreen |
| `/identify/on` \| `/off` | put a big number on each screen, to map TVs to rooms |
| `/dashboard` | the web interface |
| `/api/status` | JSON status of every TV |
| `/homepages` `/playlists` `/reload` `/health` | |

`<target>` is a TV alias or a group. Prefix any path with `/x/` to fire it and
return immediately instead of waiting.

Same grammar over TCP or UDP on port 8900, one command per line — for controllers
that cannot do HTTP.

---

## Configuration

`config.json`, reloadable with `/reload` — no restart. Keys beginning `_` are
comments.

| Key | Meaning |
|---|---|
| `base_url` | how the TVs reach this PC. **Must not change** — it is in every homepage |
| `client_name` | tokens are bound to this string; changing it re-pairs every TV |
| `allow_from` | list of controller IPs allowed to send commands (empty = any). Loopback and the dashboard are always allowed |
| `auto_heal` | after an `on`, rescan and retry anything that did not land |
| `auto_heal_minutes` | periodic sweep for TVs that are on but blank; `0` disables |
| `tvs.<alias>` | `ip`, `mac`, `label`, `token`, and a `photos` block |
| `photos.open_with` | `auto` / `macro` / `api` — how to open the browser |
| `photos.open_macro` | recorded key sequence for this TV |
| `groups` | named sets of aliases, e.g. `home`, `frames`, `bedrooms` |

State that must survive a restart — pairing tokens, the selected playlist — lives
in `state\` next to the code, deliberately **not** in a per-user folder: the SYSTEM
service and a user-run CLI resolve `%LOCALAPPDATA%` differently, which silently
hides tokens from the service.

---

## Known limitations

- **`off` is verified across the whole fleet.** `on` is less reliable: it lands
  most TVs but can leave one or two with the browser running in the background and
  nothing on screen. `auto_heal` retries those; the **Fix me** button on the
  dashboard does the same on demand.
- **`/<tv>/photos/<name>` does not validate the playlist name.** A typo is
  accepted, persisted, and blanks every TV until corrected. Use `/playlist/<name>`,
  which validates.
- **Frames never really power off** — the power key toggles Art Mode and
  `PowerState` stays `on` in both states.
- Wake-on-LAN does not wake these sets reliably; power-on generally works via the
  paired control channel instead, so a TV must be paired before it can be woken.
- `volume`/`mute` need UPnP on port 9197, which some models do not expose. Use
  `key/KEY_VOLUP` there.

[windows/README.md](windows/README.md) documents the hardware quirks in detail,
with the measurements behind each one — worth reading before changing the power or
browser-launch logic.

---

## Layout

```
windows/
  tvbridge.py        the service: HTTP/TCP/UDP, slideshow, dashboard
  config.json        TVs, groups, macros, settings
  install.bat        one-shot install (deps, firewall, boot task)
  setup_task.ps1     (re)register and restart the service
  discover.bat/.py   find Samsung TVs on the subnet
  pair.bat/pair_all.py   pair one at a time, with verification
  learn.bat          record a remote-key macro for one TV
  photos/<name>/     one folder per playlist
  README.md          hardware quirks and measurements
  UPLOAD-PHOTOS.md   how to get photos onto the PC
```

The files in the repo root are earlier single-TV experiments, kept for reference.
