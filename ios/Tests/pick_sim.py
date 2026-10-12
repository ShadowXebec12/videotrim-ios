"""Read `xcrun simctl list devices available -j` on stdin, print the UDID of an iPhone on the newest iOS runtime."""
import json
import sys

devices = json.load(sys.stdin)["devices"]
cands = []
for runtime, items in devices.items():
    if "iOS" not in runtime:
        continue
    # runtime looks like com.apple.CoreSimulator.SimRuntime.iOS-18-2
    try:
        ver = tuple(int(x) for x in runtime.rsplit("iOS-", 1)[1].split("-"))
    except (IndexError, ValueError):
        ver = (0,)
    for d in items:
        name = d.get("name", "")
        if d.get("isAvailable") and name.startswith("iPhone"):
            # prefer a plain current model over Pro Max / SE / mini
            rank = 2 if name in ("iPhone 16", "iPhone 15", "iPhone 17") else (1 if "Pro" not in name and "SE" not in name else 0)
            cands.append((ver, rank, name, d["udid"]))
if not cands:
    sys.exit("no available iPhone simulator")
cands.sort()
best = cands[-1]
sys.stderr.write("simulator: %s on iOS %s\n" % (best[2], ".".join(map(str, best[0]))))
print(best[3])
