# tvbridge — Samsung TV IP control for a Windows PC

A service that sits on a Windows PC on the TVs' subnet and turns simple IP
commands into Samsung Tizen control. Anything that can open a socket can drive
it — Loxone, Crestron, Q-SYS, a browser, a batch file, `curl`.

```
HTTP   http://<pc>:8899/<target>/<action>     GET or POST, plain-text reply
TCP    <pc>:8900       one command per line
UDP    <pc>:8900       one command per datagram
```

All three speak the same grammar, so `business/on`, `business on`, and
`GET /business/on` are the same command.

---

## Deployed — VideoWall PC, 192.168.1.246 (2026-08-03)

Already installed and running. Recorded here so it can be rebuilt or moved.

| | |
|---|---|
| Host | `VideoWall`, Windows 11 Pro, user `dream` (local admin) |
| Access | OpenSSH for Windows 9.5, key-based; SSH sessions come up **elevated** |
| Install dir | `C:\tvbridge` |
| Python | 3.12.10 at `C:\Program Files\Python312` (installed via `winget`; the pre-existing `python.exe` on PATH was a 0-byte Store stub) |
| Service | scheduled task `SamsungTVBridge`, `pythonw.exe`, **runs as SYSTEM at startup** |
| Tokens + log | `C:\Windows\System32\config\systemprofile\AppData\Local\SamsungTVControl\` (SYSTEM's profile, not `dream`'s) |
| Firewall | inbound allow 8899/TCP, 8900/TCP, 8900/UDP, all profiles |

Re-register or restart the service with:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File C:\tvbridge\setup_task.ps1
```

That script is idempotent: it unregisters the task, kills any stray
`python`/`pythonw` holding port 8899, re-registers as SYSTEM at startup, starts
it, and prints the listening PID and its owner. Use it rather than
`Start-Process` over SSH — a process started that way is a child of the SSH
session and dies when the session closes, and a stray one squatting on 8899 will
silently stop the real service from binding.

**`.246` is still a DHCP address.** Every TV's browser homepage embeds it, so it
needs a DHCP reservation before the homepages are set, or all of them break when
it moves.

## Install from scratch elsewhere

On the Windows PC, in an **Administrator** command prompt:

```bash
install.bat
```

That installs the Python packages, opens ports 8899/8900 in the firewall, and
registers a scheduled task (`SamsungTVBridge`) that starts the service at boot
with no console window. Then pair each TV once — the TV must be **on**, and you
press **Allow** on the screen with the remote:

```bash
py tvbridge.py pair business
```

The two office TVs already have tokens embedded in `config.json` and are seeded
automatically, so they should not need pairing. Check everything with:

```bash
py tvbridge.py doctor
```

`uninstall.bat` removes the task and firewall rules.

---

## Commands

| Command | Notes |
|---|---|
| `/<t>/on` | Wake-on-LAN + power key |
| `/<t>/off` | Standby |
| `/<t>/toggle` | |
| `/<t>/status` | Power, model, whether it's paired |
| `/<t>/photos` | Start the default picture playlist |
| `/<t>/photos/<name>` | Start a specific playlist |
| `/<t>/photos/off` | Stop it |
| `/<t>/volume/0-100` | Absolute, via UPnP — works unpaired |
| `/<t>/mute/on` · `/mute/off` | Via UPnP |
| `/<t>/key/KEY_VOLUP` | Any Samsung remote key |
| `/<t>/keys/KEY_UP,@500,KEY_ENTER` | Sequence: `@500` waits 500 ms, `KEY_X*3` repeats |
| `/<t>/source/hdmi1` … `hdmi4` | Key macro — **calibrate per model**, see below |
| `/<t>/macro/<name>` | Any macro from `config.json` |
| `/<t>/app/<app_id>` | e.g. `11101200001` = Netflix |
| `/<t>/url/<url>` | Open a URL in the TV browser |
| `/reload` | Re-read `config.json` — no restart |
| `/playlists` · `/health` · `/help` | |

`<t>` is a TV alias or a group. Groups fan out in parallel and each line of the
reply is prefixed with the alias.

**TVs:** `business` `crystal` (office) · `golf-right` `golf-left` `bar-side`
`back-bar` `over-bar` `bar-55` `lounge` `pavillion` `zach` `kitchen` (venue)

**Groups:** `office` `golf` `bar` `venue` `all`

```bash
curl http://192.168.1.50:8899/venue/on
curl http://192.168.1.50:8899/bar/volume/25
curl http://192.168.1.50:8899/business/photos/lobby
```

---

## Picture playlists

This is the part where Samsung's own limitations show, so it's worth reading.
Each TV picks a method with `photos.method` in `config.json`.

### `browser` — recommended, and the default

Drop images into `photos\<playlist>\` on the Windows PC. The service serves a
full-screen crossfading slideshow page, and `/<tv>/photos` points the TV's
browser at it. One command, no USB sticks, and you change what's playing by
changing files in a folder — new files are picked up within a minute without
restarting anything or touching the TV.

```
photos\default\   -> /<tv>/photos
photos\lobby\     -> /<tv>/photos/lobby
```

Per-TV options: `interval_seconds` (default 10), `fit` (`contain` letterboxes
the whole image, `cover` fills and crops), `base_url` (override if the PC has
several NICs), `wake_delay_seconds` (how long to wait after waking a TV before
sending the URL, default 8), `browser_app_id` (only if auto-detection picks
wrong).

Two things to check per TV: **Smart Hub must be signed in** or the TV reports no
apps at all and nothing launches, and some firmware launches the browser but
ignores the URL — see the hardware notes below for the one-time homepage fix.
The browser may also show its toolbar briefly before hiding.

### `art` — Frame TVs only

`kitchen` (the LS03) is set to this. It uses the real Art Mode slideshow API:
upload photos once, then the TV shuffles them natively — no browser, no PC
dependency, and it survives a reboot. The interval is in whole minutes, so
`interval_seconds: 600` becomes 10 minutes.

### `usb` — literal "switch to USB and play the slideshow"

Samsung exposes **no API for selecting the USB source**. Nothing does — not the
WebSocket API, not SmartThings, not the old MDC serial protocol on these sets.
The only route is a blind remote-key sequence, which depends on how many
sources that TV has and where the photo folder sits on the stick, so it has to
be recorded once per model:

```bash
py tvbridge.py learn business
```

Send keys one at a time while watching the screen; when it looks right, press
`d` and it prints a `"usb_macro": [...]` line to paste into that TV's `photos`
block, along with `"method": "usb"`. Typically it lands on something like
`KEY_SOURCE`, arrows to the USB tile, `KEY_ENTER`, arrows to the folder,
`KEY_ENTER`, `KEY_PLAY`.

Be aware of what this method is: it is dead reckoning. If someone unplugs the
stick, plugs in a different one, or a firmware update reorders the source list,
the macro walks into the wrong place and you re-record it. The `browser` method
exists specifically so you don't have to live with that. The same caveat
applies to the `source/hdmi*` macros — the ones shipped in `config.json` are
best guesses, not verified against these TVs.

---

## Wiring up a controller

**Loxone** — Virtual Output with address `http://<pc>:8899`, then a Virtual
Output Command per action with Command for ON set to e.g. `/venue/on`. Or a
Virtual Output at `tcp://<pc>:8900` sending `venue/on\r\n`.

**Crestron / Q-SYS / anything with a TCP client** — open `<pc>:8900`, send
`bar/off\r\n`. The connection stays open for as many commands as you like;
send `quit` to close it.

**Fire-and-forget** — a UDP datagram to `<pc>:8900`. The reply comes back to
the sender's port; ignore it if you don't need it.

Lock it down with `allow_from` in `config.json` — a list of controller IPs.
Loopback always keeps access so a wrong entry can't lock you out (fix it and
call `/reload` from the PC itself), and the slideshow pages stay readable by
the TVs regardless.

---

## Verified on hardware — `mini-led`, 192.168.1.203 (2026-08-03)

Tested against `UN50M70HAFXZA` / `26_KSUE_UB` ("50″ Mini LED"), wired, from a
machine at 192.168.1.211. Open ports: `8001`, `8002`, `8080`, `9110`.

**Works:**

- Pairing on `:8002` — token `11682040`, granted under client name `MacControl`
- `off` — `KEY_POWER` → `PowerState: standby` in 2 s
- `on` — WoL + power key → back to `on` in ~3 s from standby. (A slow ~25 s wake
  during testing was a manual power cycle, not standby. `wake_on_lan()` sends a
  burst over ~3 s anyway, which is the right thing for a cold set.)
- `status`, and the slideshow served to the LAN — page + 4K JPEGs, 200 OK

**Smart Hub sign-in is a prerequisite for anything app-related.** Before the TV
was signed in, `smartHubAgreement` was `false`, it had no apps at all,
`GET /api/v2/applications/<id>` returned 404 for *every* id including Netflix,
and every launch was silently dropped. After sign-in, `smartHubAgreement: true`
and the apps appear. If app launch does nothing, check this first.

**Browser app ids differ by firmware year.** On this set `org.tizen.browser`
404s and the browser is `3202010022079` (`{"name":"Internet","version":
"9.1.04060"}`). `browser_app_id()` probes the candidates and caches whichever
the TV reports, so no per-TV configuration is needed — override with
`photos.browser_app_id` if a set uses something else.

**This firmware launches the browser but ignores the URL.** `rest_app_run` on
the detected id reliably starts it (`running: true`), but none of these got the
TV to request our page:

| Attempt | Result |
|---|---|
| `ed.apps.launch` + `metaTag`, `NATIVE_LAUNCH` and `DEEP_LINK`, all 3 app ids | acknowledged, URL ignored |
| same with `url:` instead of / alongside `metaTag`, and a JSON-encoded `metaTag` | ignored |
| `POST /api/v2/applications/<id>` with `{"url":…}`, `{"metaTag":…}`, `{"data":…}` | launches, URL ignored |
| `ms.application.start` | `ms.error: unrecognized method value` |
| `ed.installedApp.get` | ignored, no app list returned |

Retested after a firmware update on 2026-08-03: **unchanged.** The browser is
still build 9.1.04060 and still ignores the URL. `send_text` into the address bar
was also tried and does not land (one variant returns
`ms.remote.touchDisable`).

So `photos` tries the URL launch first, and if the TV never requests the page it
falls back to launching the browser with no URL — which lands on the browser's
**homepage**. Set that homepage once per TV, with the remote, and `/photos`
works over IP from then on.

### When the browser cannot be launched over the network

The 2026 Frame (`frame-75`, QN75LS03HWFXZA) has **no working network launch for its
browser**, proven by elimination:

| Route | Result |
|---|---|
| `GET/POST /api/v2/applications/<id>` | endpoint **works** — Netflix, YouTube, Prime Video all 200. Probed all 140 community-listed app ids: the browser is not among them |
| its actual app id | unknown. DIAL reports browser build `10.1.04230` vs the Mini LED's `9.1.04060`, so it is a new unpublished id |
| WS `ed.installedApp.get` | unanswered — the TV will not enumerate its apps |
| WS `ed.apps.launch` (all ids, both action types, metaTag/url) | accepted, no effect |
| DIAL `POST /ws/app/WebBrowser` | **not a launcher.** The 200 is `org.tizen.webserver` echoing the POST body back through CGI, complete with its env vars. YouTube via DIAL is 403 |
| token in query/header, HTTPS on 8002 | no change |
| `ms.application.start` | `unrecognized method value` |

Remote **keys** work fine there, so `photos` falls back to a recorded key sequence,
`photos.open_macro`. Record one with:

```
C:\tvbridge\learn.bat frame-75
```

Pauses in the macro are explicit: keys get recorded at human speed but replayed
fast, and the home menu needs time to draw.

So `photos` now tries four things in order, and only claims success when the TV
actually requests the page: **already on screen** → **URL launch** → **REST
launch** → **key macro**. Verified 3/3 full cycles on the Frame, which happened to
succeed by three different routes — the browser sometimes survives art mode, and
sometimes has to be reopened.

A key macro is dead reckoning: it will need re-recording whenever a firmware
update reshuffles the home screen. The Mini LED launches properly over the API, so
this only applies to the Frame.

### `on` is the whole wake-to-slideshow sequence

`on` does three things, in order, and reports each:

1. power on — or, on a Frame, leave Art Mode
2. open the browser on the slideshow (its homepage)
3. send one real `KEY_ENTER` so the page can go fullscreen

```bash
curl http://192.168.1.246:8899/mini-led/off
curl http://192.168.1.246:8899/mini-led/on
```

`on` → `on (confirmed in 28s); playing dream-home (via the browser homepage)`.
It resumes the **currently selected** playlist, not `default`. Set
`on_restores_slideshow: false` in config for power only.

### Frames: power toggles Art Mode, it does not power off

`PowerState` reads `"on"` in both states, so judging a Frame's power by it produced
a false "still reports on" when `off` had in fact worked. Confirmed with the art
API: `get_artmode` read `on`, then `off` after a keypress.

So on a Frame (detected from REST `FrameTVSupport`, no pairing needed) `off`/`on`
drive the art API explicitly — `set_artmode(True/False)`, an explicit set rather
than the toggling key — and confirm via `get_artmode()`. ~5 s each, 2/2 verified.

**Art Mode closes the browser, it does not merely cover it.** A Frame stopped
requesting the page for 15+ minutes across four art-mode cycles. So the slideshow
has to be relaunched after art mode, which is what `on` now does — and why a Frame
that cannot launch its browser over the network (see Smart Hub) will come back to
its home screen instead of the slideshow.

### Power-key findings on this model — measured, not assumed

| Finding | Evidence |
|---|---|
| **`KEY_POWEROFF` is silently ignored** | sent it and watched `PowerState` + port 8002 for **61 s**: no change, port never closed. Also no effect with the browser closed first. Do not use it. |
| **`KEY_POWER` is reliable** | 3/3 trials, TV reached standby in 3 s, 3 s, 5 s. It is a **toggle**, not "off". |
| **Never send `KEY_POWER` after a successful WoL** | it would toggle a just-woken TV straight back off. `on()` therefore confirms WoL worked *before* it will touch the remote. |
| **Waking takes ~30 s** | WoL alone did not wake it inside 20 s; the power key over port 8002 is what completes it (8002 stays open in normal standby). 3/3 at 28–34 s. |
| **Relaunching an open browser is a no-op** | it stays on whatever page it is on, so `photos` would not return to the homepage. `launch_browser()` closes the app and waits 2.5 s before relaunching. |

Because of the first two rows, power is judged by the **resulting `PowerState`**, never
by whether the WebSocket conversation ended tidily — the TV tears the channel
down mid-command as it changes power state, so an exception there says nothing
about whether the key landed. `off`/`on` poll and report what actually happened.

Verified 3 consecutive full cycles (`off` → `on` → `photos`): standby confirmed
3–5 s, on confirmed 28–34 s, slideshow back up every time.

### CONFIRMED END-TO-END on `mini-led` (2026-08-03)

With the homepage set on the TV and the service running on the PC at .246, the
whole chain works:

| Step | Result |
|---|---|
| `GET /mini-led/photos` | `playing default (via the browser homepage)` — TV fetched the page |
| TV's Internet app | `running: true, visible: true` |
| `GET /mini-led/photos/testcard` | `switched to testcard (slideshow already on screen)` — no relaunch |
| TV's live URL after switch | served the `testcard` manifest, then `default` again |
| `off` / `on` | standby in 2 s, WoL back on in ~3 s |

So after the one-time homepage entry, everything is IP-driven: starting the
slideshow, switching playlists with the page already up, and power.

### The browser's address bar

Samsung's own position is that the Internet app's address and menu bars are not
user-configurable, so there is no API to hide them. Three things help, in order:

1. **On the TV: hamburger menu → Settings → "Hide Tabs and Menu Bar" → Use.**
   Present on some models. This is the real fix where it exists.
2. **The page now provokes the auto-hide.** Those bars hide once a page can
   scroll, and the old page could not: it was `height:100%` with
   `overflow:hidden`, and it called `scrollTo(0,0)` every 30 s, which *re-showed*
   them. The body is now `min-height:130vh` with the image layers `position:fixed`
   (so scrolling moves no pixels) and it scrolls slightly down instead.
3. **It also attempts the Fullscreen API**, on load and on any keypress. Usually
   gated behind a user gesture, so `/<tv>/key/KEY_ENTER` can trigger it.
4. **On a Frame, native Art Mode has no browser chrome at all** — set
   `photos.method` to `art` for that TV. Trade-off: art mode is per-TV content, so
   the same photos must be uploaded to each Frame.

### Page updates reach the TVs by themselves

The manifest carries `PAGE_VERSION`; the running page compares it on each 5 s poll
and reloads itself when it differs. **Bump `PAGE_VERSION` whenever you edit
`SLIDESHOW_HTML`** and every TV picks the change up within seconds, untouched.

To force it now — for a TV on a page older than this mechanism, or a stuck browser:

```bash
curl http://192.168.1.246:8899/mini-led/reopen
```

That closes and relaunches the browser, landing it back on the homepage.

### The chosen playlist survives a restart

The shared and per-TV playlist pointers are written to `state.json` in the state
directory and reloaded at startup, so a service restart or power cut comes back to
what was selected rather than to `default`. Verified: set `dream-home`, restarted
the service, still `dream-home`.

### One URL for every TV

Set this as the browser homepage on **every** TV:

```
http://192.168.1.246:8899/slideshow/live/all
```

All of them then show the same playlist and switch together — any `photos`
command moves the shared pointer, so one command drives the whole fleet:

```bash
curl http://192.168.1.246:8899/mini-led/photos/lobby     # every TV follows
```

Per-TV URLs (`/slideshow/live/<alias>`) still exist for the case where one screen
should show something different from the rest. Those follow only commands aimed at
that TV — or at a group, since a group command sets each member's pointer, so
`all/photos/lobby` keeps per-TV URLs in sync too. `curl /homepages` prints both.

### The live URL — why the homepage step is one-off

Each TV has a stable address that never changes:

```
http://<pc>:8899/slideshow/live/<tv-alias>
```

`photos/<playlist>` repoints that URL rather than navigating to a new one, and
the page re-checks every 5 s, so switching playlists works **on a page that is
already open** — no relaunch, no reload, and nothing to re-enter on the TV. If
the slideshow is already up, `photos` says `switched to <playlist>` and doesn't
touch the browser at all. Verified in a browser: `default` → `testcard` followed
the pointer live without a reload.

That is what makes the homepage a genuine one-time setup rather than a recurring
annoyance: one URL per TV, set once, then everything else is IP-driven.
`/slideshow/<playlist>` still works as a direct address for spot checks.

**`volume` and `mute` do not work on this model** — UPnP RenderingControl on
`:9197` is closed. `:9110` advertises Samsung `IPControlService` but its SCPD
declares zero actions, and there is no MediaRenderer in its SSDP announcements,
so there is no DLNA push either. Use `key/KEY_VOLUP` style commands instead.

The other 12 TVs are 2024/2025 firmware and have not been retested against any
of this; `org.tizen.browser` and UPnP volume may well work on them.

## Things worth knowing

**Power-on needs the same subnet.** A fully-asleep Tizen set has no open ports,
so the only way in is a Wake-on-LAN broadcast, and broadcasts don't route. This
config lists both sites, but a PC on `192.168.1.x` can only *wake* the venue
TVs. Everything else — off, status, keys, volume, photos — works routed across
subnets. To power on both sites you need a PC (or a second instance) on each,
or directed broadcast forwarding enabled on the router.

**Pairing is subnet-sensitive too.** Granting a token only worked from a
controller on the TV's own subnet; from another subnet the TV instantly
rejected it.

**Tokens follow the client name, not the machine.** They're bound to
`client_name` (`MacControl`), which is why the office tokens transplant from the
Mac to this PC without re-pairing. Change that string and every TV re-prompts.

**Volume and mute don't need pairing** — they go over UPnP on port 9197, so
they work on a TV you've never paired.

**Log file:** `%LOCALAPPDATA%\SamsungTVControl\tvbridge.log` (tokens live there
too). Run `py tvbridge.py run -v` in a console for live debug output.

**Any command also works from the command line**, same grammar:
`py tvbridge.py venue/status`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `not paired or token rejected` | Run `pair` for that TV from a PC on its subnet |
| `TV unreachable or in standby` | Normal for an off TV. If it's on, check the IP — none of these TVs have confirmed DHCP reservations, so addresses can drift |
| `on` does nothing after a long standby | WoL isn't reaching it — PC must be on the TV's subnet, and the wired MAC must match `config.json` |
| Slideshow shows "No images" | Empty `photos\<playlist>\`, or check `/playlists` |
| Slideshow never appears | TV's Internet app missing or blocked; try `py tvbridge.py doctor` and open the printed URL in a desktop browser first |
| Reply says previous config still active | JSON syntax error in `config.json`; the error text names the line |
