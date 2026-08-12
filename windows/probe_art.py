#!/usr/bin/env python3
"""Report a Frame's art-mode state. Usage: probe_art.py <alias>

Exists to answer one question empirically: when KEY_POWER leaves a Frame still
reporting PowerState=on, has it actually switched to Art Mode?
"""
import sys
import warnings

warnings.filterwarnings("ignore")

import tvbridge as T


def main() -> int:
    alias = sys.argv[1] if len(sys.argv) > 1 else "frame-75"
    cfg = T.Config(T.CONFIG_PATH)
    T.setup_logging(cfg, False)
    bridge = T.Bridge(cfg)
    tv = bridge.tvs.get(alias)
    if tv is None:
        print(f"unknown alias {alias}; have: {' '.join(sorted(bridge.tvs))}")
        return 1

    print(f"{alias} {tv.ip}")
    print(f"  PowerState : {tv.power_state()}")
    try:
        art = tv.connect(timeout=20).art()
    except Exception as exc:
        print(f"  art(): {type(exc).__name__}: {exc}")
        return 1
    for name in ("supported", "get_artmode", "get_api_version"):
        fn = getattr(art, name, None)
        if fn is None:
            continue
        try:
            print(f"  {name:<16}: {fn()}")
        except Exception as exc:
            print(f"  {name:<16}: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
