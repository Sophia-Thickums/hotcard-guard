#!/usr/bin/env python3
"""mi25_power_cap.py — hold the MI25's power cap at a safe value, IDEMPOTENTLY, every boot.

★ WHY (Ryan, 2026-10-05; MEASURED the same hour).
The MI25's failure mode is THERMAL: measured 105-110 C and off the bus in ~10 s of sustained
DMA at its default 260 W cap. Power converts to heat, so the watts per second ARE the heat
per second. Capping at 180 W (the value Ryan chose) makes the card *survive work it could not
survive before*:

    SAME config that latched the card this morning (llama-bench -p 2048 -ncmoe 12), at 180 W:
        pp2048 116 t/s · tg32 50 t/s · peak junction ~59 C · NO bus-loss   <- COMPLETED
    at 260 W (default): same run drove to 105-110 C and dropped off the bus.

This is "lever 1" of how we actually use the MI25 (see WORLD/work/MI25-INCIDENT-20261005/RECORD.md):
a power cap, a duty cycle, and better cooling. This script is the power cap, written down so it
does not rely on remembering to set it.

Resolves the card by PCI IDENTITY (1002:6860), never by address or index — the card's address and
its DRM index both move. Idempotent: sets the same value every run. If the card is absent or the
attribute is missing, it says so and exits 0 (never guesses).

Tuning: /etc/mi25-power-cap.conf  (CAP_W). Run once per boot by mi25-power-cap.service.
"""
import glob
import os
import re
import sys

CONF = "/etc/mi25-power-cap.conf"
MI25_ID = "0x1002:0x6860"
CAP_W_DEFAULT = 180


def load_cap():
    cap = CAP_W_DEFAULT
    try:
        for line in open(CONF):
            line = line.split("#", 1)[0].strip()
            if line.startswith("CAP_W"):
                cap = int(re.findall(r"\d+", line.split("=", 1)[1])[0])
    except OSError:
        pass
    return cap


def mi25_pci():
    for dev in glob.glob("/sys/bus/pci/devices/*"):
        try:
            v = open(dev + "/vendor").read().strip()
            d = open(dev + "/device").read().strip()
        except OSError:
            continue
        if f"0x{v}:0x{d}" == MI25_ID:
            return os.path.basename(dev)
    return None


def main():
    cap_w = load_cap()
    pci = mi25_pci()
    if not pci:
        print("MI25 not present on the bus — nothing to cap")
        return 0
    caps = glob.glob(f"/sys/bus/pci/devices/{pci}/hwmon/hwmon*/power1_cap")
    if not caps:
        print(f"{pci}: no power1_cap attribute (driver without power capping) — skipped")
        return 0
    path = caps[0]
    try:
        before = int(open(path).read().strip())
    except (OSError, ValueError):
        before = None
    target = cap_w * 1_000_000
    try:
        with open(path, "w") as fh:
            fh.write(str(target))
        after = int(open(path).read().strip())
    except (OSError, ValueError) as exc:
        print(f"{pci}: could not write {path}: {exc}")
        return 1
    print(f"MI25 {pci}: power cap {before and before // 1_000_000}W -> {after // 1_000_000}W "
          f"(target {cap_w}W)")
    return 0 if after == target else 1


if __name__ == "__main__":
    sys.exit(main())
