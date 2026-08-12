"""Entry point for `python -m tvhub`. Everything lives in tvhub.service.

Kept to almost nothing on purpose: the scheduled task and the systemd unit both
register `<python> -m tvhub run`, so this file is on the service's startup path
and anything that can fail here fails before logging exists.
"""

import sys


def _main() -> int:
    from tvhub.service import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_main())
