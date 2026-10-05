#!/usr/bin/env python3
"""sophia_mi25_guard — a hard thermal trip for the MI25, so it can never cook again.

★ WHY (2026-10-05). Two failure shapes, measured:

  1. COOLING SATURATION under sustained load, read off the fan-curve service's own 5 s
     clock (a spot reading samples the cooled corpse and lies):
         12:25:41 -> 12:26:16 :  34 -> 62 -> 85 -> 105 -> 106 C -> device lost from bus
         12:56:20 -> 12:57:40 :  55 -> 72 -> 84 ->  87 -> 110 C -> device lost from bus
     Crit is 105 C. That is ~10-35 s of sustained DMA from cold. The heatsink + shroud +
     case-header fan cannot shed sustained work on this card.

  2. A WEDGE IS EXPENSIVE. After `amdgpu: device lost from bus!` / `GPU Recovery
     Failed: -19`, the device object is dead: a dock power-cycle does NOT re-probe it,
     `remove`+`rescan` does NOT rebind it (falsified 10-05). Only a FULL REBOOT recovers
     it. Only cutting the tower PSU silences the card's latched LED + buzzer.

Ryan's standing decision (2026-10-05): the MI25 stays put; we do not run the sustained
parameters; and we do not rely on being able to flip a switch afterwards. This service is
that decision written in code, because a rule that lives only in a note gets re-broken
(the 10-05 incident: the law was already written down and was violated anyway).

★ THE LOAD IS THE VARIABLE, NOT THE FAN.
The fan curve already ramps 4 duty/degree above 62 C and is at max well before 105 C. Past
TRIP the airflow is already maxed; the only thing that stops the climb is stopping the
WORK. So the trip acts on the client, never on the fan.

★ SCOPE — LEARNED THE HARD WAY IN THIS FILE'S OWN FIRST TEST.
The first version killed "any pid holding the MI25 render node". That set includes
kwin_wayland (the desktop compositor), which opens every DRM node with a ~28 KiB /
2 MiB PROBE handle and does no compute there. A trip must never take Ryan's display.
So a client is acted on only when BOTH hold:
    (a) it is not a known display server (by name), AND
    (b) it is actually RESIDENT on the card — drm-total-vram + drm-total-gtt above
        MIN_RESIDENT_B. An inference client holds gigabytes; a probe holds kibibytes.
A handle-holder is logged and left alone. The distinction is measured from
/proc/<pid>/fdinfo/<n> (drm-pdev + drm-total-vram/gtt), not guessed from the name.

★ ACT, THEN ESCALATE — DO NOT DESTROY ON FIRST CONTACT.
TRIP first SIGSTOPs the identified clients (reversible: the work parks, the card cools).
If the junction is still at/above TRIP after KILL_AFTER s, it escalates SIGTERM -> SIGKILL.
A guard should prefer the reversible act when the reversible act works.

SAFETY CONTRACT
  * Read-only on the GPU; sets no pwm; fights nobody.
  * Refuses to act if it cannot resolve the MI25 by IDENTITY (0x1002:0x6860).
  * A dead sensor channel (-1 / 0 / 511 C) is its own answer and is NEVER a trip.
  * --dry-run computes and logs the exact action list without signalling anything.

TESTS  (prove the path can go red, non-vacuously, without heat and without the desktop):
    sophia_mi25_guard --selftest   # synthetic decisions + classification + a live dummy
    sophia_mi25_guard --dry-run    # what WOULD it do right now, on the real holders
"""
import glob
import os
import re
import signal
import subprocess
import sys
import time

CONF = "/etc/sophia-mi25-guard.conf"
# >>> CONFIGURE ME: set your card's vendor:device (and the PCI-address fallback) to the
# >>> card you want to protect. Find it with:  lspci -nn | grep -i vga
MI25_ID = "0x1002:0x6860"                # Vega 10 MI25 — IDENTITY, never address/index
MI25_PCI_FALLBACK = "0000:09:00.0"
LOG = "/var/log/sophia-mi25-guard.log"

WARN_C = 82.0
TRIP_C = 90.0
RESUME_C = 65.0
INTERVAL = 2.0
KILL_AFTER = 20.0
COOLDOWN = 300.0
MIN_RESIDENT_B = 256 * 1024 * 1024        # 256 MiB — separates a real client from a probe
DEAD_LO, DEAD_HI = 5.0, 150.0
FAN_STALL_DUTY = 50                       # a case fan commanded above this with 0 rpm is stalled

DISPLAY_SERVERS = {"kwin_wayland", "kwin_wayland_wrapper", "kwin_x11", "Xorg", "Xwayland",
                   "gnome-shell", "mutter", "plasmashell", "sway", "weston", "gdm",
                   "gdm-wayland-session", "sddm", "Hyprland", "wayfire"}


def load_conf():
    global WARN_C, TRIP_C, RESUME_C, INTERVAL, KILL_AFTER, COOLDOWN, MIN_RESIDENT_B
    try:
        with open(CONF) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, v = (s.strip() for s in line.split("=", 1))
                if k == "WARN_C":
                    WARN_C = float(v)
                elif k == "TRIP_C":
                    TRIP_C = float(v)
                elif k == "RESUME_C":
                    RESUME_C = float(v)
                elif k == "INTERVAL":
                    INTERVAL = float(v)
                elif k == "KILL_AFTER":
                    KILL_AFTER = float(v)
                elif k == "COOLDOWN":
                    COOLDOWN = float(v)
                elif k == "MIN_RESIDENT_MIB":
                    MIN_RESIDENT_B = int(float(v)) * 1024 * 1024
    except OSError:
        pass


def log(msg, also_print=True):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if also_print:
        print(line, flush=True)


def notify(title, body):
    if os.environ.get("SOPHIA_MI25_NOPOPUP"):
        return
    subprocess.run(
        f"command -v notify-send >/dev/null && notify-send -u critical '{title}' '{body}'",
        shell=True, capture_output=True)


# ---------------------------------------------------------------- device resolution
def mi25_pci():
    """The MI25's current PCI address, resolved by device IDENTITY."""
    for dev in glob.glob("/sys/bus/pci/devices/*"):
        try:
            v = open(dev + "/vendor").read().strip()
            d = open(dev + "/device").read().strip()
        except OSError:
            continue
        if f"0x{v}:0x{d}" == MI25_ID:
            return os.path.basename(dev)
    if os.path.isdir(f"/sys/bus/pci/devices/{MI25_PCI_FALLBACK}"):
        return MI25_PCI_FALLBACK
    return None


def mi25_junction(pci):
    """Junction temp in C, or None. Only a labelled 'junction' channel is read."""
    real = os.path.realpath(f"/sys/bus/pci/devices/{pci}")
    for h in glob.glob(real + "/hwmon/hwmon*/"):
        for lbl in glob.glob(h + "temp*_label"):
            try:
                if open(lbl).read().strip() != "junction":
                    continue
                return int(open(lbl.replace("_label", "_input")).read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
    return None


def render_node(pci):
    for node in glob.glob("/sys/class/drm/renderD*"):
        try:
            if f"PCI_SLOT_NAME={pci}" in open(node + "/device/uevent").read():
                return "/dev/dri/" + os.path.basename(node)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- client classification
_BYTES = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}


def parse_bytes(s):
    """'2052 KiB' -> int bytes. Anything unparseable -> 0 (silent, never a guess)."""
    m = re.match(r"\s*(\d+)\s*([KMGT]?i?B)\s*$", s or "", re.I)
    if not m:
        return 0
    return int(m.group(1)) * _BYTES.get(m.group(2).lower(), 1)


def classify_client(comm, vram_b, gtt_b):
    """'display' | 'handle' | 'load'. Order matters: a display server is never touched,
    and a real client must be RESIDENT on the card — a probe handle is not a load."""
    if (comm or "").strip() in DISPLAY_SERVERS:
        return "display"
    if (vram_b or 0) + (gtt_b or 0) >= MIN_RESIDENT_B:
        return "load"
    return "handle"


def client_residency(pid, pdev):
    """(comm, vram_bytes, gtt_bytes) for this pid's DRM client on `pdev`, else None.

    Every fd of one DRM client reports the SAME client-wide totals, so take the MAX
    across fds — summing would multiply the residency by the fd count.
    """
    comm = "?"
    try:
        comm = open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        pass
    vram = gtt = 0
    seen = False
    for fd in glob.glob(f"/proc/{pid}/fdinfo/*"):
        try:
            txt = open(fd).read()
        except OSError:
            continue
        if pdev not in txt or "drm-pdev" not in txt:
            continue
        seen = True
        m = re.search(r"drm-total-vram:\s*(.+)", txt)
        if m:
            vram = max(vram, parse_bytes(m.group(1)))
        m = re.search(r"drm-total-gtt:\s*(.+)", txt)
        if m:
            gtt = max(gtt, parse_bytes(m.group(1)))
    return (comm, vram, gtt) if seen else None


def pids_on_node(node):
    """PIDs with an open fd on the DRM node. Root sees other users' fds."""
    if not node:
        return []
    try:
        target = os.stat(node).st_rdev
    except OSError:
        return []
    mine = os.getpid()
    out = []
    for p in glob.glob("/proc/[0-9]*"):
        pid = int(os.path.basename(p))
        if pid == mine:
            continue
        try:
            for fd in glob.glob(p + "/fd/*"):
                try:
                    if os.stat(fd).st_rdev == target:
                        out.append(pid)
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(out)


def action_list(pci, node):
    """What a trip would act on, and full evidence for the ones it will not touch."""
    acts, skips = [], []
    for pid in pids_on_node(node):
        r = client_residency(pid, pci)
        if r is None:
            skips.append((pid, "no fdinfo for this pdev", 0, 0))
            continue
        comm, vram, gtt = r
        kind = classify_client(comm, vram, gtt)
        if kind == "load":
            acts.append((pid, comm, vram, gtt))
        else:
            skips.append((pid, f"{kind} (not a compute client on this card)", vram, gtt))
    return acts, skips


# ---------------------------------------------------------------- decision (pure)
def nct_fan():
    """(fan_rpm, pwm_duty) for the case header fan that cools the MI25, or (None, None)."""
    for h in glob.glob("/sys/class/hwmon/hwmon*/"):
        try:
            if open(h + "name").read().strip() != "nct6799":
                continue
            rpm = int(open(h + "fan1_input").read().strip())
            pwm = int(open(h + "pwm1").read().strip())
            return rpm, pwm
        except (OSError, ValueError):
            continue
    return None, None


def decide_cooling(temp):
    """'ok' | 'warn' | 'trip' | 'dead'. A dead channel is NEVER 'trip'."""
    if temp is None:
        return "dead"
    if temp < DEAD_LO or temp > DEAD_HI:
        return "dead"
    if temp >= TRIP_C:
        return "trip"
    if temp >= WARN_C:
        return "warn"
    return "ok"


# ---------------------------------------------------------------- the trip
def do_trip(pci, node, temp):
    acts, skips = action_list(pci, node)
    log(f"TRIP  MI25 junction {temp:.0f}C >= {TRIP_C:.0f}C on {node or '?'}")
    for pid, comm, v, g in acts:
        log(f"TRIP  LOAD   pid {pid} ({comm}) resident "
            f"vram={v // 1048576}MiB gtt={g // 1048576}MiB -> SIGSTOP")
    for pid, why, v, g in skips:
        log(f"TRIP  skip   pid {pid} — {why} "
            f"(vram={v // 1048576}MiB gtt={g // 1048576}MiB)")
    if not acts:
        log("TRIP  no compute client found on the MI25 — nothing to stop. The card is hot "
            "without a load: a COOLING fault, not a load fault. Check the fan and shroud.")
        notify("Sophia MI25 guard — THERMAL TRIP",
               f"MI25 at {temp:.0f}C with no compute client. Cooling fault — check the fan.")
        return

    notify("Sophia MI25 guard — THERMAL TRIP",
           f"MI25 hit {temp:.0f}C — pausing the load to save the card from the 105C latch.")

    for pid, comm, v, g in acts:
        try:
            os.kill(pid, signal.SIGSTOP)
        except OSError:
            pass

    t0 = time.time()
    while time.time() - t0 < KILL_AFTER:
        time.sleep(INTERVAL)
        now = mi25_junction(pci)
        if now is None or decide_cooling(now) != "trip":
            log(f"TRIP  junction fell to {now}C after SIGSTOP — load parked, card saved "
                f"({time.time() - t0:.0f}s)")
            return
    log(f"TRIP  junction stayed >= {TRIP_C:.0f}C for {KILL_AFTER:.0f}s with the load "
        f"STOPPED — escalating (the stopped clients were still holding heat).")
    for pid, comm, v, g in acts:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(3.0)
    for pid, comm, v, g in acts:
        if os.path.exists(f"/proc/{pid}"):
            log(f"TRIP  SIGKILL pid {pid} ({comm})")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    log(f"TRIP  post-abort junction: {mi25_junction(pci)}C")


# ---------------------------------------------------------------- tests
def selftest():
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {name}: got {got!r}, want {want!r}")

    MIB = 1024 * 1024
    print("sophia_mi25_guard --selftest")
    print(" (1) the decision can go red, non-vacuously:")
    check("60C -> ok", decide_cooling(60.0), "ok")
    check("83C -> warn", decide_cooling(83.0), "warn")
    check("91C -> trip", decide_cooling(91.0), "trip")
    check("106C -> trip", decide_cooling(106.0), "trip")
    check("-1C -> dead", decide_cooling(-1.0), "dead")
    check("511C -> dead, never trip", decide_cooling(511.0), "dead")
    check("unreadable -> dead", decide_cooling(None), "dead")

    print(" (2) the trip threshold is the tested knob:")
    global TRIP_C
    saved = TRIP_C
    TRIP_C = 70.0
    check("70C with TRIP=70 -> trip", decide_cooling(70.0), "trip")
    TRIP_C = saved

    print(" (3) client classification — the flaw that killed the desktop in v1:")
    check("kwin_wayland is never a target",
          classify_client("kwin_wayland", 28 * 1024, 2 * MIB), "display")
    check("Xorg is never a target",
          classify_client("Xorg", 8 * MIB, 300 * MIB), "display")
    check("an inference client (13 GiB vram) is a load",
          classify_client("llama-bench", 13 * 1024 * MIB, 4 * MIB), "load")
    check("a big GTT-only client is a load",
          classify_client("ollama_llm", 3 * MIB, 900 * MIB), "load")
    check("a 28 KiB probe on the node is a handle, NOT a load",
          classify_client("something", 28 * 1024, 2 * MIB), "handle")
    check("a 1 MiB holder is a handle", classify_client("x", 0, 1 * MIB), "handle")
    check("parse '2052 KiB'", parse_bytes("2052 KiB"), 2052 * 1024)
    check("parse '28 KiB'", parse_bytes("28 KiB"), 28 * 1024)
    check("parse garbage -> 0", parse_bytes("n/a"), 0)

    print(" (4) the signal path works, on a dummy (a plain file, never the GPU):")
    probe = "/tmp/mi25_guard_selftest.node"
    open(probe, "w").write("x")
    proc = subprocess.Popen(["sh", "-c", f"exec 9<{probe}; sleep 60"])
    try:
        time.sleep(0.4)
        check("holder found by fd scan", proc.pid in pids_on_node(probe), True)
        os.kill(proc.pid, signal.SIGSTOP)
        time.sleep(0.3)
        state = open(f"/proc/{proc.pid}/stat").read().split(") ", 1)[1].split()[0]
        check("SIGSTOP parks it (state T)", state, "T")
        os.kill(proc.pid, signal.SIGCONT)
        time.sleep(0.3)
        state = open(f"/proc/{proc.pid}/stat").read().split(") ", 1)[1].split()[0]
        check("SIGCONT resumes it", state in ("S", "R"), True)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            os.unlink(probe)
        except OSError:
            pass

    print(" (5) the real card, and the REAL action list right now (nothing signalled):")
    pci = mi25_pci()
    node = render_node(pci) if pci else None
    print(f"  MI25 pci : {pci}")
    print(f"  junction : {mi25_junction(pci) if pci else None} C")
    print(f"  node     : {node}")
    if pci:
        acts, skips = action_list(pci, node)
        print(f"  WOULD ACT ON : {[(p, c) for p, c, _, _ in acts] or 'nothing'}")
        for pid, why, v, g in skips:
            print(f"  WOULD SKIP   : pid {pid} — {why}")
        check("no display server in the action list",
              any(c in DISPLAY_SERVERS for _, c, _, _ in acts), False)

    print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


def dry_run():
    load_conf()
    pci = mi25_pci()
    if not pci:
        print("MI25 not resolvable — the guard would refuse to arm.")
        return 1
    node = render_node(pci)
    temp = mi25_junction(pci)
    print(f"MI25 pci={pci} node={node} junction={temp}C "
          f"-> state={decide_cooling(temp)} (warn={WARN_C:.0f} trip={TRIP_C:.0f})")
    acts, skips = action_list(pci, node)
    print("would act on :")
    for pid, comm, v, g in acts:
        print(f"    pid {pid} ({comm}) vram={v // 1048576}MiB gtt={g // 1048576}MiB")
    if not acts:
        print("    (nothing — no compute client on the MI25)")
    print("would skip :")
    for pid, why, v, g in skips:
        print(f"    pid {pid} — {why} (vram={v // 1048576}MiB gtt={g // 1048576}MiB)")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--dry-run" in sys.argv:
        return dry_run()
    load_conf()
    pci = mi25_pci()
    if not pci:
        log("arm refused: MI25 not resolvable — silent (a guard must not act on a guess)")
        return 0
    node = render_node(pci)
    log(f"armed: MI25 pci={pci} node={node} warn={WARN_C:.0f}C trip={TRIP_C:.0f}C "
        f"interval={INTERVAL:.0f}s")
    last_notify = 0.0
    last_state = None
    parked = set()
    while True:
        temp = mi25_junction(pci)
        state = decide_cooling(temp)
        # the case fan that cools the MI25 is a SEPARATE failure mode: a stalled fan is a
        # warning even while the card is cool, because it is the only thing keeping it so.
        rpm, pwm = nct_fan()
        if state == "ok" and rpm == 0 and pwm is not None and pwm >= FAN_STALL_DUTY:
            state = "warn"
        if state == "trip":
            do_trip(pci, node, temp)
            parked |= {p for p, _, _, _ in action_list(pci, node)[0]}
        else:
            if state == "warn" and (time.time() - last_notify) > COOLDOWN:
                last_notify = time.time()
                log(f"WARN  MI25 junction {temp:.0f}C >= {WARN_C:.0f}C — a sustained load "
                    f"on this card saturates its cooling and latches at 105C")
                notify("Sophia MI25 guard — MI25 hot",
                       f"MI25 at {temp:.0f}C. A sustained load will latch it at 105C.")
            elif state == "dead" and last_state != "dead":
                log("NOTE  MI25 junction unreadable/out-of-range — dead channel, or the "
                    "card is off the bus. Not acting on it.")
            elif state == "ok" and last_state in ("warn", "dead", "trip"):
                log(f"ok    MI25 back to {temp:.0f}C")
            if parked and temp is not None and temp <= RESUME_C:
                for pid in sorted(parked):
                    if os.path.exists(f"/proc/{pid}"):
                        try:
                            os.kill(pid, signal.SIGCONT)
                            log(f"resume: SIGCONT pid {pid} (junction {temp:.0f}C)")
                        except OSError:
                            pass
                parked.clear()
        last_state = state
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
