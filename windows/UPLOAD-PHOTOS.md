# Task: upload a photo folder to the TV slideshow PC

You are uploading a folder of photos to a Windows PC that serves them as a
slideshow to Samsung TVs. Everything below is verified against the live system.

## Target

| | |
|---|---|
| Host | `192.168.1.246` — Windows 11 PC, hostname `VideoWall` |
| User | `dream` |
| Access | SSH (OpenSSH for Windows), **public-key auth**. Default shell is **PowerShell**. |
| Destination | `C:\tvbridge\photos\<playlist-name>\` |
| Service | a Python service already runs as SYSTEM and serves these files. **Do not restart, stop, or edit it.** |

One playlist = one folder under `C:\tvbridge\photos\`. Existing playlists:
`default` (the photos currently on screen) and `testcard` (test slides — leave alone).

## Before you start

Check SSH works:

```bash
ssh -o BatchMode=yes dream@192.168.1.246 "hostname"
```

Expect `VideoWall`. If you get *Permission denied (publickey...)*, your key is not
authorized — **stop and ask the human to run this once on the PC** (substitute your
own public key):

```powershell
Add-Content "$env:USERPROFILE\.ssh\authorized_keys" 'ssh-ed25519 AAAA... your-key'
```

Do not attempt password authentication.

## Hard requirements

These are enforced by the server. Violating them means photos silently don't show.

1. **Format must be JPEG, PNG, WebP, GIF, or BMP.** Anything else in the folder is
   ignored. **HEIC/HEIF will NOT display** — iPhone photos must be converted first.
   Also skip: RAW, TIFF, PDF, video.
2. **Playlist folder name: letters, digits, space, `_`, `.`, `-` only**, max 64 chars.
   Anything else is rejected as a bad playlist name. Prefer lowercase, no spaces.
3. **Filenames set the display order** — they are sorted alphabetically. Use a
   numeric prefix if order matters: `01-`, `02-`, …
4. **Resize to 3840 px on the long edge.** The TVs are 3840×2160; larger files just
   slow the TV's browser down.

## Steps

### 1. Convert and resize locally

On macOS (`sips` is built in — handles HEIC natively):

```bash
mkdir -p /tmp/upload
sips -s format jpeg -s formatOptions 88 -Z 3840 /path/to/photos/*.HEIC /path/to/photos/*.heic --out /tmp/upload/ 2>/dev/null
sips -s format jpeg -s formatOptions 88 -Z 3840 /path/to/photos/*.jpg /path/to/photos/*.png --out /tmp/upload/ 2>/dev/null
```

With ImageMagick, any platform:

```bash
mkdir -p /tmp/upload && magick mogrify -path /tmp/upload -format jpg -quality 88 -resize 3840x3840\> /path/to/photos/*
```

Confirm you have only image files, and note the count:

```bash
ls -la /tmp/upload/
```

### 2. Copy to the PC

Pick a playlist name (lowercase, no spaces), e.g. `lobby`:

```bash
ssh dream@192.168.1.246 "New-Item -ItemType Directory -Force C:\tvbridge\photos\lobby"
scp /tmp/upload/* dream@192.168.1.246:C:/tvbridge/photos/lobby/
```

Note the path style: **forward slashes** after the colon in `scp`, backslashes inside
PowerShell commands.

### 3. Verify the server sees them

```bash
curl http://192.168.1.246:8899/playlists
```

Your playlist must appear with the expected image count, e.g. `lobby: 24 image(s)`.
If the count is lower than the number of files you copied, some were the wrong
format — recheck requirement 1. Confirm the exact list and order:

```bash
curl http://192.168.1.246:8899/slideshow/live/mini-led/manifest.json
```

## Showing it on a TV

TVs at this site: **`mini-led`** (50″, working) and **`frame-85`** (85″ Frame, pairing
not yet completed — may not respond).

```bash
curl "http://192.168.1.246:8899/mini-led/photos/lobby"
```

Expected replies, both meaning success:

- `switched to lobby (slideshow already on screen)` — the TV was already showing a
  slideshow, so it just changed content. Takes about 5 seconds to appear on screen.
- `playing lobby (via the browser homepage)` — the TV wasn't showing it, so the
  browser was relaunched.

A reply starting `WARNING` or `ERROR` means it did **not** work — report the text
verbatim, don't try to fix it yourself.

To put the original photos back:

```bash
curl "http://192.168.1.246:8899/mini-led/photos/default"
```

## Notes and limits

- **No restart or reload needed.** A new folder is visible immediately. Files added
  to the playlist currently on screen appear within ~5 seconds on their own.
- Deleting a file mid-slideshow is safe — the page skips it.
- Only ever write inside `C:\tvbridge\photos\`. Do not touch `tvbridge.py`,
  `config.json`, the `SamsungTVBridge` scheduled task, or the `testcard` playlist.
- Don't send TV power commands unless asked; `photos` wakes the TV by itself.
- If `scp` prints a post-quantum key-exchange warning, ignore it — it's cosmetic.
