# legacy

The first pass: single-TV scripts and probes written while working out what
Samsung's API would and would not do. Superseded by `windows/` (which runs the
house today) and then by `tvhub-app/`.

Kept because a few still document things nothing else records:

- `loxone-direct-config.txt` - the exact Loxone Virtual Output strings for
  driving TVs directly with no bridge: `wol://` for power on, and raw
  `tcp://<ip>:9197` UPnP SOAP for volume and mute. That approach was abandoned
  (Loxone cannot speak WebSocket, so power off and source selection are
  impossible), but the working command strings are here if it is ever wanted.
- `probe_1516.py`, `test_wol_cold.py`, `verify_token.py` - the probes that
  established MDC port 1515 is closed on these sets, how they behave waking
  from cold, and that pairing tokens are bound to the client name rather than
  the machine.

Nothing here is on the running path. Do not install from this folder.
