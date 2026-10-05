# hotcard-guard — a hard thermal trip and a power cap for a GPU that dies under load

**The problem:** some GPUs have marginal sustained-load cooling. Run them hard and they do not merely
throttle — they climb to the hardware thermal limit in seconds, fall off the PCIe bus, and (on some
boards) latch a hardware alarm that no software can clear. Recovery is a full reboot. We hit this with
an AMD Instinct MI25: measured **105-110 C and device-lost in ~10 seconds** of sustained work at its
default 260 W power cap.

**The fix, in two parts, both measured:**

1. **Cap the power.** Power becomes heat; the watts-per-second *are* the heat-per-second. At **180 W**
   the exact workload that killed the card completed at **~59 C**, no bus-loss. Persisted as a systemd
   oneshot that runs every boot, so it does not depend on anyone remembering.
2. **Put a hard trip under it.** A guard that samples the card's junction every 2 seconds and, at a
   threshold, **stops the load** — because by the time a card is that hot the fan is already maxed and
   the only remaining variable is the work.

This is deliberately small and readable. Take it, point it at your card's PCI id, tune two numbers.

---

## Why "stop the load" and not "spin the fan"

Measured on our card: the case fan barely responds (1400 -> 1618 RPM over 36 -> 85 C; ~2000 RPM
ceiling). Raising fan duty buys ~1 C at high speed for a lot of noise. Meanwhile the fan *curve is
already at maximum* well before the card reaches its trip. So past the trip, airflow is no longer the
lever — **the load is.** The guard acts on the load.

## Why SIGSTOP first, not kill

The guard does not destroy work on first contact. At the trip threshold it **SIGSTOPs** the offending
compute clients — reversible: the work parks, the card cools, nothing is lost. Only if the junction is
still at the threshold after a grace period does it escalate (SIGTERM -> SIGKILL). A guard should prefer
the reversible act when the reversible act works.

## ★ The failure this design exists to prevent (and a trap in the obvious version)

The obvious implementation — "kill every process holding the GPU's render node" — **took down the
desktop** in our own first live test. The compositor (kwin_wayland) holds a **~28 KiB probe handle on
every DRM node** and does no compute there, so it matched "holds the node" and got killed. A GPU
thermal guard must never take the display.

The fix, which is the point of this repo: a process is a **compute client** only if it is
**(a) not a known display server AND (b) actually resident on the card** — `drm-total-vram` +
`drm-total-gtt` (read from `/proc/<pid>/fdinfo/<n>`, matched by `drm-pdev`) above a threshold
(default 256 MiB). A probe handle is kibibytes; an inference client is gigabytes. Measured: this
correctly identifies a real `llama-server` holding 4.6 GiB while leaving the compositor alone.

## Also: a dead sensor is not a hot card

When a card falls off the bus its telemetry reads garbage (`-1`, `0`, `511` C), not silence. A naive
guard sees `511 >= threshold` and acts on a corpse. This one treats an out-of-range reading as a **dead
channel** — never a trip. (The same trap applies to a fan curve: `511` must not ram a fan to full.)

## Usage

```
sudo install -m0755 hotcard_guard.py /usr/local/sbin/hotcard_guard
sudo install -m0755 hotcard_power_cap.py /usr/local/sbin/hotcard_power_cap
# edit the PCI id + thresholds in the .conf files, then install the systemd units in systemd/
```

Resolve the card **by identity** (`vendor:device`), never by index or PCI address — both change when
hardware moves or a card is absent at boot.

### Tests (prove the check can go red, without heat and without touching the desktop)

```
hotcard_guard.py --selftest   # synthetic decisions + client classification + a live SIGSTOP/SIGCONT
                              # on a dummy; asserts a display server is never a target
hotcard_guard.py --dry-run    # the real action list RIGHT NOW, signalling nothing
```

## Thresholds (defaults, and why)

- **WARN** — log + notify only, nothing killed.
- **TRIP** — park the card's compute clients. Set **below** the hardware crit with margin for one
  sample period: the runaway climb peaks at tens of degrees per few seconds, so one sample can
  overshoot.
- **MIN_RESIDENT_MIB = 256** — separates a real client from a probe handle. **Do not lower this to 0**;
  that is the desktop-killing configuration.
- **RESUME** — re-allow parked work once the card is genuinely cool again.

## License

Public domain / CC0.
