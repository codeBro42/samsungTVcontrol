#!/usr/bin/env python3
"""Feasibility probe: can we put our own photos into a Frame's art library?

Uploads ONE image, lists what the TV then reports, and deletes it again, so the
TV is left as it was. Usage: probe_art_upload.py <alias> [image]
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import tvbridge as T


def main() -> int:
    alias = sys.argv[1] if len(sys.argv) > 1 else "frame-75"
    cfg = T.Config(T.CONFIG_PATH)
    T.setup_logging(cfg, False)
    bridge = T.Bridge(cfg)
    tv = bridge.tvs[alias]

    if len(sys.argv) > 2:
        img = Path(sys.argv[2])
    else:
        folder = T.playlist_dir(cfg, "dream-home")
        img = next((p for p in sorted(folder.iterdir())
                    if p.suffix.lower() in (".jpg", ".jpeg")), None)
    if img is None or not img.is_file():
        print("no test image found")
        return 1

    data = img.read_bytes()
    print(f"{alias} {tv.ip}  test image {img.name} ({len(data)//1024} KB)")

    art = tv.connect(timeout=60).art()
    print(f"  supported     : {art.supported()}")
    print(f"  api version   : {art.get_api_version()}")
    try:
        before = art.available()
        print(f"  items before  : {len(before)}")
    except Exception as exc:
        print(f"  available()   : {type(exc).__name__}: {exc}")
        before = []

    print("  uploading ...")
    try:
        content_id = art.upload(data, file_type="JPEG", matte="none")
    except Exception as exc:
        print(f"  UPLOAD FAILED : {type(exc).__name__}: {exc}")
        return 1
    print(f"  UPLOAD OK     : content_id={content_id}")

    try:
        after = art.available()
        print(f"  items after   : {len(after)}")
    except Exception as exc:
        print(f"  available()   : {type(exc).__name__}: {exc}")

    # Leave the TV as we found it.
    try:
        art.delete(content_id)
        print(f"  cleaned up    : deleted {content_id}")
    except Exception as exc:
        print(f"  CLEANUP FAILED: {type(exc).__name__}: {exc} "
              f"(remove {content_id} by hand in My Photos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
