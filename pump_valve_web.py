#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Multi-pump control web page for Fluigent Flow EZ pumps (1 to 4 pumps).

Run:   python pump_web.py                 (real pumps, count auto-detected)
       python pump_web.py --pumps 2       (use exactly 2 pumps)
       python pump_web.py --sim --pumps 4 (fake pumps, to try the page without hardware)
Then open http://127.0.0.1:8000 (it opens automatically).

Pump 1..4 map to Fluigent pressure channels 0..3 in the order the SDK detects them.
Every run is logged to pump_logs/ as one CSV with a column pair per pump.
"""
import argparse, csv, io, json, math, os, threading, time, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DT = 0.1          # control loop period in seconds (10 Hz)
MAX_PUMPS = 4


# ---------------------------------------------------------------- hardware
class SimHardware:
    """Fake pumps (first-order lag) so the page can be tried without hardware."""
    device = "Simulated pumps"
    def __init__(self, n): self.count = n; self.p = [0.0] * n; self.sp = [0.0] * n
    def init(self): pass
    def set(self, i, v): self.sp[i] = v
    def get(self, i):
        self.p[i] += (self.sp[i] - self.p[i]) * (0.25 - 0.04 * i)
        if self.sp[i] == 0 and abs(self.p[i]) < 0.05: self.p[i] = 0.0
        return self.p[i]
    def range(self, i): return (-800.0, 1000.0) if i == 0 else (0.0, 7000.0)   # pump 1 = push-pull
    def close(self): pass


class FluigentHardware:
    def __init__(self):
        import Fluigent.SDK as sdk
        self.sdk, self.count = sdk, 0
    def init(self):
        self.sdk.fgt_init()
        self.count = self.sdk.fgt_get_pressureChannelCount()
        self.device = "%d Fluigent pressure channel(s) found" % self.count
    def set(self, i, v): self.sdk.fgt_set_pressure(i, float(v))
    def get(self, i): return float(self.sdk.fgt_get_pressure(i))
    def range(self, i): return tuple(self.sdk.fgt_get_pressureRange(i))
    def close(self):
        try:
            for i in range(self.count): self.sdk.fgt_set_pressure(i, 0)
        finally: self.sdk.fgt_close()


# ---------------------------------------------------------------- valve box
VALVES = {1: "Fluid A", 2: "Fluid B"}   # valve number -> label
INVERT_VALVES = True   # found by testing this box: command "OFF" opens the valve, "ON" closes it
ZERO_CONFIRM_S, ZERO_TIMEOUT_S, VALVE_DEAD_S, SETTLE_TIMEOUT_S = 2.0, 120.0, 0.5, 30.0   # s
# Memetis datasheet, normally closed "negative pressure" valve: inlet -0.7 to +2.0 bar, outlet max 2.0 bar
# (no minimum is listed for the outlet: ask Memetis before applying negative pressure to the outlet port)
VALVE_MIN_MBAR, VALVE_MAX_MBAR = -700.0, 2000.0
ROLE_NAMES = {"pp": "Push-pull pump", "ps": "Push pump"}


class Stopped(Exception): pass
class Aborted(Exception): pass


def _f(key, label, kind, role=None, sign="+", default=10.0):
    # kind "p" = pressure (sign "+" must be positive, "-" negative), kind "r" = ramp rate
    return dict(key=key, label=label, kind=kind, role=role, sign=sign, default=default)


TASKS = {
 "t1": dict(title="Task 1: one pump, two valves", roles=["pp"], valves=[1, 2],
  summary="One push-pull pump feeds V1 and V2 through an always-open three-way valve. Each stage holds until you press the next button.",
  stages=[
   dict(title="Stage 1", info="Open V1, ramp to P1",
        fields=[_f("P1", "P1: positive pressure", "p", "pp", "+", 100.0), _f("r1", "Ramp 0 to P1", "r")],
        ops=[("open", [1]), ("ramp", {"pp": ("P1", "r1")})]),
   dict(title="Stage 2", info="Ramp to P2",
        fields=[_f("P2", "P2: negative pressure", "p", "pp", "-", -100.0), _f("r2", "Ramp P1 to P2", "r")],
        ops=[("ramp", {"pp": ("P2", "r2")})]),
   dict(title="Stage 3", info="Ramp to 0, switch V1 to V2, ramp to P3",
        fields=[_f("r3", "Ramp P2 to 0", "r"), _f("P3", "P3: negative pressure", "p", "pp", "-", -100.0), _f("r4", "Ramp 0 to P3", "r")],
        ops=[("ramp", {"pp": (0, "r3")}), ("zero", ["pp"]), ("open", [2]), ("ramp", {"pp": ("P3", "r4")})]),
   dict(title="Stage 4", info="Ramp to 0, close all valves",
        fields=[_f("r5", "Ramp P3 to 0", "r")],
        ops=[("ramp", {"pp": (0, "r5")}), ("zero", ["pp"]), ("close",)])]),
 "t2": dict(title="Task 2: two pumps, four valves", roles=["pp", "ps"], valves=[1, 2, 3, 4],
  summary="The push-pull pump feeds V1 and V2 and the push pump feeds V3 and V4, each through an always-open three-way valve. Each stage holds until you press the next button.",
  stages=[
   dict(title="Stage 1", info="Open V2 and V4, ramp to P1 and P2",
        fields=[_f("P1", "P1: push-pull, positive", "p", "pp", "+", 100.0), _f("r1", "Push-pull ramp 0 to P1", "r"),
                _f("P2", "P2: push, positive", "p", "ps", "+", 100.0), _f("r2", "Push ramp 0 to P2", "r")],
        ops=[("open", [2, 4]), ("ramp", {"pp": ("P1", "r1"), "ps": ("P2", "r2")})]),
   dict(title="Stage 2", info="Ramp to P3 and P4",
        fields=[_f("P3", "P3: push-pull, negative", "p", "pp", "-", -100.0), _f("r3", "Push-pull ramp P1 to P3", "r"),
                _f("P4", "P4: push, positive", "p", "ps", "+", 100.0), _f("r4", "Push ramp P2 to P4", "r")],
        ops=[("ramp", {"pp": ("P3", "r3"), "ps": ("P4", "r4")})]),
   dict(title="Stage 3", info="Ramp to 0, switch to V1 and V3, ramp to P5 and P6",
        fields=[_f("r5", "Push-pull ramp P3 to 0", "r"), _f("r6", "Push ramp P4 to 0", "r"),
                _f("P5", "P5: push-pull, negative", "p", "pp", "-", -100.0), _f("r7", "Push-pull ramp 0 to P5", "r"),
                _f("P6", "P6: push, positive", "p", "ps", "+", 100.0), _f("r8", "Push ramp 0 to P6", "r")],
        ops=[("ramp", {"pp": (0, "r5"), "ps": (0, "r6")}), ("zero", ["pp", "ps"]), ("open", [1, 3]),
             ("ramp", {"pp": ("P5", "r7"), "ps": ("P6", "r8")})]),
   dict(title="Stage 4", info="Ramp to 0, close all valves",
        fields=[_f("r9", "Push-pull ramp P5 to 0", "r"), _f("r10", "Push ramp P6 to 0", "r")],
        ops=[("ramp", {"pp": (0, "r9"), "ps": (0, "r10")}), ("zero", ["pp", "ps"]), ("close",)])]),
}


def tasks_public():
    return {k: dict(title=t["title"], summary=t["summary"], roles=t["roles"], valves=t["valves"], role_names=ROLE_NAMES,
                    stages=[dict(title=g["title"], info=g["info"], fields=g["fields"]) for g in t["stages"]])
            for k, t in TASKS.items()}


class BoxLink:
    # Valve box (Arduino, existing firmware) over USB serial. state[n] is True when valve n is OPEN.
    def __init__(self, sim=False):
        self.sim, self.ser, self.port, self.reply = sim, None, "", ""
        self.state = {n: False for n in VALVES}

    @property
    def connected(self): return self.ser is not None or (self.sim and bool(self.port))

    def port_info(self):
        info = dict(ports=[], port=self.port, connected=self.connected, error=None)
        if self.sim:
            info["ports"] = [dict(port="SIM", desc="Simulated valve box")]
            return info
        try:
            from serial.tools import list_ports
            info["ports"] = [dict(port=q.device, desc=q.description)
                             for q in sorted(list_ports.comports(), key=lambda q: q.device)]
        except Exception as e:
            info["error"] = "Cannot list ports (run: pip install pyserial): %s" % e
        return info

    def connect(self, port):
        if not port: raise RuntimeError("Choose a port first")
        self.disconnect()
        if not self.sim:
            import serial
            self.ser = serial.Serial(port, 115200, timeout=0.1)
            threading.Thread(target=self._reader, args=(self.ser,), daemon=True).start()
            time.sleep(2.0)                      # the Uno resets when the port opens
        self.port = port
        self.close_all()

    def disconnect(self):
        if self.connected:
            try: self.close_all()
            except Exception: pass
        if self.ser:
            try: self.ser.close()
            except Exception: pass
        self.ser, self.port, self.reply = None, "", ""
        self.state = {n: False for n in VALVES}

    def _reader(self, ser):
        buf = b""
        while ser is self.ser:
            try: buf += ser.read(64)
            except Exception:
                self.reply = "serial error"
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.reply = line.decode(errors="replace").strip()

    def set(self, n, is_open):
        if not self.connected: raise RuntimeError("Valve box not connected")
        if not self.sim:
            on = (not is_open) if INVERT_VALVES else is_open
            self.ser.write(("V;%d;%s\n" % (n, "ON" if on else "OFF")).encode())
        self.state[n] = is_open

    def close_all(self):
        err = None
        for n in VALVES:
            try: self.set(n, False)
            except Exception as e: err = e
        if err: raise err


# ---------------------------------------------------------------- controller
class Pump:
    def __init__(self, rng):
        self.sp = self.meas = self.target = 0.0
        self.up, self.dn, self.hold = 10.0, 10.0, 0.0
        self.cycle, self.hold_until, self.state, self.rng = False, None, "idle", list(rng)


class Controller:
    def __init__(self, hw, n, max_mbar, box):
        self.hw, self.n, self.max, self.box = hw, n, max_mbar, box
        self.ps = [Pump(hw.range(i)) for i in range(n)]
        self.lock, self.halt = threading.Lock(), threading.Event()
        self.t0, self.log, self.error = time.time(), [], None   # log rows: (t, [sp], [meas])
        os.makedirs("pump_logs", exist_ok=True)
        self.path = os.path.join("pump_logs", "pump_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
        self.f = open(self.path, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(self.header())
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.proc = dict(task=None, running=False, stage=0, busy=False, aborting=False, done=False, error=None, msg="",
                         pumps=[], roles={}, base={}, tol=2.0)
        self.events, self.pstop, self.pabort, self.plock = [], threading.Event(), threading.Event(), threading.Lock()
        self.evpath = os.path.join("pump_logs", "events_" + os.path.basename(self.path)[5:])
        with open(self.evpath, "w", newline="") as f: f.write("time,event\n")

    def header(self):
        h = ["time_s", "clock"]
        for i in range(self.n): h += ["pump%d_setpoint_mbar" % (i + 1), "pump%d_measured_mbar" % (i + 1)]
        h += ["valve%d_open" % v for v in sorted(VALVES)]      # 1 = open (as commanded)
        return h

    def row(self, t, clock, sps, ms, vs=()):
        r = ["%.2f" % t, datetime.fromtimestamp(clock).isoformat(timespec="milliseconds")]
        for s, m in zip(sps, ms): r += ["%.2f" % s, "%.2f" % m]
        return r + [str(v) for v in vs]

    def command(self, d):
        a, which = d.get("action"), d.get("pump")
        if a == "stage": return self.stage_start(d)
        if a == "abort": return self.abort_start(d)
        if self.proc["running"] and a in ("go", "cycle", "depressurize", "valve"):
            return False, "A process is running. Use Abort or STOP."
        if a == "valve":
            try: n = int(d.get("valve"))
            except (TypeError, ValueError): return False, "Unknown valve."
            if n not in VALVES: return False, "Unknown valve."
            try: self.box.set(n, bool(d.get("open")))
            except Exception as e: return False, "Valve box: %s" % e
            return True, ""
        with self.lock:
            if a == "stop":                          # vent immediately: one pump or all
                try: idx = range(self.n) if which == "all" else [self.ps.index(self.ps[int(which)])]
                except (TypeError, ValueError, IndexError): return False, "Unknown pump."
                for i in idx:
                    p = self.ps[i]; p.sp = p.target = 0.0; p.cycle, p.hold_until = False, None
                if self.proc["running"] and any(i in idx for i in self.proc["pumps"]):
                    if self.proc["busy"]: self.pstop.set()        # the running stage stops itself
                    else:
                        self.proc.update(running=False, error="Stopped. Pressure vented and valves closed.")
                        self.close_valves()
                if which == "all": self.close_valves()      # STOP ALL also stops flow
                return True, ""
            if self.error: return False, "Pump error. Fix it and restart the program."
            try:
                p = self.ps[int(which)]
                tgt, up, dn, hold = (float(d.get(k, 0)) for k in ("target", "ramp", "deramp", "hold"))
            except IndexError: return False, "Unknown pump."
            except (TypeError, ValueError): return False, "Enter numbers in every field."
            if not all(map(math.isfinite, (tgt, up, dn, hold))): return False, "Enter numbers in every field."
            if a == "depressurize":
                if dn <= 0: return False, "De-ramp rate must be above 0."
                p.dn, p.target, p.cycle, p.hold_until = dn, 0.0, False, None
                return True, ""
            if a in ("go", "cycle"):
                if not 0 <= tgt <= self.max: return False, "Target must be between 0 and %g mbar." % self.max
                if up <= 0 or dn <= 0: return False, "Ramp and de-ramp rates must be above 0."
                if hold < 0: return False, "Hold time cannot be negative."
                p.up, p.dn, p.target, p.hold = up, dn, tgt, hold
                p.cycle, p.hold_until = (a == "cycle"), None
                return True, ""
        return False, "Unknown action."

    def event(self, text):
        now = datetime.now()
        self.events.append((now.strftime("%H:%M:%S"), text)); del self.events[:-30]
        with open(self.evpath, "a", newline="") as f: csv.writer(f).writerow([now.isoformat(timespec="milliseconds"), text])

    def stage_start(self, d):
        tk, k = d.get("task"), d.get("stage")
        task = TASKS.get(tk)
        if task is None or k not in (1, 2, 3, 4): return False, "Unknown task or stage."
        if self.error: return False, "Pump error. Fix it and restart the program."
        if any(v not in VALVES for v in task["valves"]):
            return False, "This task needs valves %s but only valves %s exist: simulation only for now." % (task["valves"], sorted(VALVES))
        if self.n < len(task["roles"]): return False, "This task needs %d pump(s); %d found." % (len(task["roles"]), self.n)
        if not self.box.connected: return False, "Connect the valve box first (Manual tab, Valves panel)."
        pr = self.proc
        if k > 1:
            if not (pr["running"] and pr["task"] == tk and pr["stage"] == k - 1 and not pr["busy"]):
                return False, "Finish the previous stage first."
            roles = pr["roles"]
        else:
            try: roles = {r: int(d["roles"][r]) for r in task["roles"]}
            except (KeyError, TypeError, ValueError): return False, "Choose the pumps."
            if len(set(roles.values())) < len(roles) or not all(0 <= i < self.n for i in roles.values()):
                return False, "Choose a different, existing pump for each role."
        try:
            vals = {f["key"]: float(d["vals"][f["key"]]) for g in task["stages"] for f in g["fields"]}
            tol, rabort = float(d["tol"]), float(d["rabort"])
        except (KeyError, TypeError, ValueError): return False, "Enter numbers in every field."
        if not all(map(math.isfinite, [*vals.values(), tol, rabort])): return False, "Enter numbers in every field."
        if not 0 < tol <= 10: return False, "Zero tolerance must be above 0 and at most 10 mbar."
        if rabort <= 0: return False, "Abort ramp rate must be above 0."
        for g in task["stages"]:
            for f in g["fields"]:
                v = vals[f["key"]]
                if f["kind"] == "r":
                    if v <= 0: return False, "%s must be above 0." % f["label"]
                    continue
                rng = self.ps[roles[f["role"]]].rng
                lo, hi = max(rng[0], VALVE_MIN_MBAR), min(rng[1], VALVE_MAX_MBAR, self.max)
                if f["sign"] == "+" and not 0 < v <= hi: return False, "%s must be above 0 and at most %g mbar." % (f["label"], hi)
                if f["sign"] == "-":
                    if lo >= 0: return False, "Pump %d cannot do negative pressure (%s)." % (roles[f["role"]] + 1, f["label"])
                    if not lo <= v < 0: return False, "%s must be between %g and 0 mbar." % (f["label"], lo)
        with self.plock:
            pr = self.proc
            if pr["busy"]: return False, "A stage is still running."
            if k == 1:
                if pr["running"]: return False, "A process is already running."
                for i in roles.values():
                    if self.ps[i].target != 0 or self.ps[i].sp != 0: return False, "Pump %d must be at 0 mbar to start." % (i + 1)
                self.proc = dict(task=tk, running=True, stage=0, busy=True, aborting=False, done=False, error=None,
                                 msg="Starting", pumps=list(roles.values()), roles=roles, base={}, tol=tol)
                self.pstop.clear(); self.pabort.clear()
            elif not pr["running"] or pr["stage"] != k - 1: return False, "Finish the previous stage first."
            else: pr.update(busy=True, tol=tol)
        threading.Thread(target=self._stage, args=(task, k, roles, vals, tol, rabort), daemon=True).start()
        return True, ""

    def abort_start(self, d):
        try: rabort = float(d["rabort"])
        except (KeyError, TypeError, ValueError): return False, "Enter the abort ramp rate."
        if not (math.isfinite(rabort) and rabort > 0): return False, "Abort ramp rate must be above 0."
        with self.plock:
            pr = self.proc
            if not pr["running"]: return False, "No process is running."
            if pr["aborting"]: return True, ""
            self.pabort.set()
            if pr["busy"]:
                pr["abort_rate"] = rabort                  # the running stage hands over to the abort sequence
                return True, ""
            pr.update(busy=True, aborting=True)
        threading.Thread(target=self._abort_run, args=(pr["roles"], pr["tol"], rabort), daemon=True).start()
        return True, ""

    def _wait(self, cond, timeout=None):
        t0 = time.time()
        while not self.pstop.is_set():
            if self.error: raise RuntimeError("Pump error: %s" % self.error)
            if self.pabort.is_set() and not self.proc["aborting"]: raise Aborted()
            if cond(): return True
            if timeout and time.time() - t0 > timeout: return False
            time.sleep(0.05)
        raise Stopped()

    def _sleep(self, secs): self._wait(lambda: False, secs)

    def _baseline(self, roles, tol):
        samples = {r: [] for r in roles}                    # idle reading = each sensor's "zero" (it can sit below 0)
        for _ in range(10):
            for r, i in roles.items(): samples[r].append(self.ps[i].meas)
            self._sleep(0.1)
        for r, sm in samples.items():
            b = sorted(sm)[len(sm) // 2]
            if max(sm) - min(sm) > max(2.0, tol):
                raise RuntimeError("Pump %d pressure is still changing (%.1f to %.1f mbar): wait for it to settle, then start again" % (roles[r] + 1, min(sm), max(sm)))
            if abs(b) > 20: raise RuntimeError("Pump %d idle reading %.1f mbar is too far from zero" % (roles[r] + 1, b))
            self.proc["base"][r] = b
            self.event("Zero reference, pump %d: %.1f mbar" % (roles[r] + 1, b))

    def _ramp(self, goals, roles):
        pr = self.proc
        pr["msg"] = "Ramping: " + "; ".join("pump %d to %g mbar at %g mbar/s" % (roles[r] + 1, tg, rate) for r, (tg, rate) in goals.items())
        if not pr["aborting"]: self.event(pr["msg"])
        with self.lock:
            for r, (tg, rate) in goals.items():
                q = self.ps[roles[r]]; q.up = q.dn = rate; q.target, q.cycle, q.hold_until = tg, False, None
        self._wait(lambda: all(self.ps[roles[r]].sp == tg for r, (tg, _) in goals.items()))
        for r, (tg, _) in goals.items():
            if tg == 0: continue                            # zero is confirmed by a separate "zero" step
            q, b = self.ps[roles[r]], pr["base"].get(r, 0.0)
            if not self._wait(lambda: abs(q.meas - b - tg) <= max(2.0, 0.03 * abs(tg)), SETTLE_TIMEOUT_S):
                raise RuntimeError("Pump %d did not reach %g mbar (reads %.1f, zero reference %.1f)" % (roles[r] + 1, tg, q.meas, b))

    def _zero(self, rs, roles, tol):
        pr = self.proc
        pr["msg"] = "Waiting for zero pressure (within %g mbar of the idle reading for %g s)" % (tol, ZERO_CONFIRM_S)
        since = {}
        def ok():
            now, good = time.time(), True
            for r in rs:
                if abs(self.ps[roles[r]].meas - pr["base"].get(r, 0.0)) <= tol: since.setdefault(r, now)
                else: since.pop(r, None); good = False
            return good and all(now - since[r] >= ZERO_CONFIRM_S for r in rs)
        if not self._wait(ok, ZERO_TIMEOUT_S):
            raise RuntimeError("Zero not confirmed within %d s (pump %s)" % (ZERO_TIMEOUT_S, ", ".join(str(roles[r] + 1) for r in rs)))

    def _op(self, op, roles, vals, tol):
        pr = self.proc
        if op[0] == "open":
            names = ", ".join("V%d" % v for v in op[1])
            pr["msg"] = "Switching valves: closing all, then opening " + names
            self.box.close_all(); self._sleep(VALVE_DEAD_S)
            for v in op[1]: self.box.set(v, True)
            self._sleep(0.3); self.event("Valves open: " + names)
        elif op[0] == "close":
            self.box.close_all(); self.event("All valves closed")
        elif op[0] == "ramp":
            self._ramp({r: (vals[t] if isinstance(t, str) else float(t), vals[rk]) for r, (t, rk) in op[1].items()}, roles)
        elif op[0] == "zero":
            self._zero(op[1], roles, tol)

    def _stage(self, task, k, roles, vals, tol, rabort):
        pr, g = self.proc, task["stages"][k - 1]
        try:
            if k == 1: self._baseline(roles, tol)
            self.event("%s: %s | %s" % (g["title"], g["info"], ", ".join("%s=%g" % (f["key"], vals[f["key"]]) for f in g["fields"])))
            for op in g["ops"]: self._op(op, roles, vals, tol)
            if k == 4:
                self.event("Process complete")
                pr.update(stage=4, running=False, busy=False, done=True, msg="Process complete. All valves closed.")
            else:
                pr.update(stage=k, busy=False, msg="Stage %d complete. Holding until you press Stage %d." % (k, k + 1))
        except Aborted: self._abort_run(roles, tol, pr.get("abort_rate", rabort))
        except Stopped: self._stopped()
        except Exception as e: self._fault(roles, e)

    def _abort_run(self, roles, tol, rabort):
        pr = self.proc
        try:
            pr.update(busy=True, aborting=True, msg="Aborting: ramping to 0, then closing all valves")
            self.event("ABORT: all pumps to 0 at %g mbar/s, then close valves" % rabort)
            self._ramp({r: (0.0, rabort) for r in roles}, roles)
            self._zero(list(roles), roles, tol)
            self.box.close_all(); self.event("Aborted: pressure at 0, all valves closed")
            pr.update(running=False, busy=False, aborting=False, msg="Aborted. Pressure ramped to 0 and valves closed.")
        except Stopped: self._stopped()
        except Exception as e: self._fault(roles, e)

    def _stopped(self):
        self.close_valves()
        self.proc.update(running=False, busy=False, aborting=False, error="Stopped. Pressure vented and valves closed.")
        self.event("Process stopped (STOP)")

    def _fault(self, roles, e):
        with self.lock:
            for i in roles.values(): self.ps[i].sp = self.ps[i].target = 0.0
        self.close_valves()
        self.proc.update(running=False, busy=False, aborting=False, error="%s. Pressure vented and valves closed." % e)
        self.event("Process fault: %s" % e)

    def close_valves(self):
        try: self.box.close_all()
        except Exception: pass

    def box_op(self, d):
        try:
            if d.get("op") == "connect": self.box.connect(str(d.get("port", "")))
            elif d.get("op") == "disconnect": self.box.disconnect()
            else: return False, "Unknown action."
        except Exception as e:
            return False, "Valve box: %s" % e
        return True, ""

    def run(self):
        nxt = time.time()
        while not self.halt.is_set():
            try:
                ms = [self.hw.get(i) for i in range(self.n)]
                now = time.time()
                with self.lock:
                    for p, m in zip(self.ps, ms):
                        p.meas = m
                        diff = p.target - p.sp
                        if diff != 0:
                            step = (p.up if diff > 0 else p.dn) * DT
                            p.sp = p.target if abs(diff) <= step else p.sp + math.copysign(step, diff)
                            p.state = "ramping up" if diff > 0 else "ramping down"
                        elif p.cycle:
                            if p.hold_until is None: p.hold_until = now + p.hold
                            p.state = "holding"
                            if now >= p.hold_until: p.cycle, p.hold_until, p.target = False, None, 0.0
                        else:
                            p.state = "idle" if p.sp == 0 else "holding"
                    sps, t = [p.sp for p in self.ps], now - self.t0
                    vs = [int(self.box.state.get(v, False)) for v in sorted(VALVES)]
                    self.log.append((t, sps, ms, vs))
                for i, s in enumerate(sps): self.hw.set(i, s)
                self.w.writerow(self.row(t, now, sps, ms, vs))
                if len(self.log) % 10 == 0: self.f.flush()
            except Exception as e:
                with self.lock:
                    self.error = str(e)
                    for p in self.ps: p.state, p.sp, p.target = "error", 0.0, 0.0
                for i in range(self.n):
                    try: self.hw.set(i, 0)
                    except Exception: pass
                self.close_valves()
                return
            nxt += DT
            time.sleep(max(0, nxt - time.time()))

    def snapshot(self, after):
        with self.lock:
            n, now = len(self.log), time.time()
            pts = []
            for t, sps, ms, vs in self.log[max(after, n - 6000):]:
                r = [round(t, 2)]
                for s, m in zip(sps, ms): r += [round(s, 2), round(m, 2)]
                pts.append(r + vs)
            ps = [dict(state=p.state, sp=p.sp, meas=p.meas, target=p.target, rng=p.rng,
                       hold_left=None if p.hold_until is None else max(0, p.hold_until - now)) for p in self.ps]
            return dict(n=n, pts=pts, pumps=ps, error=self.error, max=self.max,
                        device=self.hw.device, file=self.path,
                        valves=dict(connected=self.box.connected, port=self.box.port, reply=self.box.reply,
                                    items=[dict(n=n, label=l, open=self.box.state[n]) for n, l in VALVES.items()]),
                        process=dict(self.proc, events=self.events[-30:]))

    def csv_text(self):
        with self.lock: rows = list(self.log)
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(self.header())
        for t, sps, ms, vs in rows: w.writerow(self.row(t, self.t0 + t, sps, ms, vs))
        return out.getvalue()

    def shutdown(self):
        self.halt.set()
        self.thread.join(timeout=2)
        try:
            for i in range(self.n): self.hw.set(i, 0)
        except Exception: pass
        self.close_valves()
        try: self.hw.close()
        except Exception: pass
        self.f.close()


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    ctrl = None
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)): body = json.dumps(body)
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, INDEX, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            try: after = int(parse_qs(u.query).get("after", ["0"])[0])
            except ValueError: after = 0
            self._send(200, self.ctrl.snapshot(after))
        elif u.path == "/api/tasks":
            self._send(200, tasks_public())
        elif u.path == "/api/ports":
            self._send(200, self.ctrl.box.port_info())
        elif u.path == "/api/export.csv":
            name = "pump_log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
            self._send(200, self.ctrl.csv_text(), "text/csv",
                       {"Content-Disposition": 'attachment; filename="%s"' % name})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/api/cmd", "/api/box") or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(400, {"ok": False, "error": "bad request"})
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "bad request"})
        ok, msg = self.ctrl.box_op(d) if self.path == "/api/box" else self.ctrl.command(d)
        self._send(200 if ok else 400, {"ok": ok, "error": None if ok else msg})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, *a): pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="use fake pumps")
    ap.add_argument("--pumps", type=int, default=0, help="number of pumps, 1 to 4 (default: auto-detect; 2 with --sim)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--max-mbar", type=float, default=2000.0, help="software pressure cap for every pump")
    a = ap.parse_args()
    if a.pumps > MAX_PUMPS: raise SystemExit("--pumps can be at most %d" % MAX_PUMPS)
    hw = SimHardware(a.pumps or 2) if a.sim else FluigentHardware()
    try:
        hw.init()
    except Exception as e:
        raise SystemExit("Could not start the pumps: %s" % e)
    n = a.pumps or min(hw.count, MAX_PUMPS)
    if n < 1 or n > hw.count:
        hw.close()
        raise SystemExit("Found %d pressure channel(s), but %d pump(s) requested. Check USB cables and "
                         "that the Fluigent software is closed." % (hw.count, n))
    if a.sim: VALVES.update({3: "Fluid C", 4: "Fluid D"})   # Task 2 needs four valves: simulation only for now
    ctrl = Controller(hw, n, a.max_mbar, BoxLink(sim=a.sim))
    Handler.ctrl = ctrl
    ctrl.thread.start()
    srv = Server(("127.0.0.1", a.port), Handler)
    url = "http://127.0.0.1:%d" % a.port
    print("Controlling %d pump(s) at %s  (Ctrl+C to stop; pressure returns to 0)" % (n, url))
    print("Logging to", ctrl.path)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Setting all pressures to 0 and closing...")
        ctrl.shutdown()


INDEX = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pump control</title>
<style>
:root{--bg:#e8edef;--panel:#f7f9fa;--ink:#13222a;--mute:#5d6f77;--line:#cfd8dc;--stop:#c62828;--stopbg:#fdeaea;--btn:#13222a;--btnink:#f7f9fa;--c0:#0a7c86;--c1:#c27400;--c2:#7a3fb5;--c3:#c2185b}
@media (prefers-color-scheme:dark){:root{--bg:#10181c;--panel:#18242a;--ink:#e6eef1;--mute:#8ea1a9;--line:#2b3b43;--stop:#ff6b6b;--stopbg:#3a1f1f;--btn:#e6eef1;--btnink:#10181c;--c0:#3fc1cc;--c1:#f0a73a;--c2:#b98cf0;--c3:#f06fa0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 "Segoe UI",system-ui,sans-serif}
main{max-width:1200px;margin:0 auto;padding:18px;display:grid;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}
.top{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
canvas{width:100%;height:300px;display:block;margin-top:8px}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--mute);margin-top:6px}
.legend i{display:inline-block;width:18px;height:0;border-top:3px solid;vertical-align:middle;margin-right:6px}
#cards{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(255px,1fr))}
.card{border-top:5px solid var(--c)}
h2{margin:0 0 8px;font-size:18px}
.read{display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap}
.big{font-size:42px;line-height:1;font-weight:600;font-variant-numeric:tabular-nums;color:var(--c)}
.big small{font-size:14px;font-weight:400;color:var(--mute);margin-left:4px}
.sub{font-variant-numeric:tabular-nums;color:var(--mute);font-size:13px}
.sub b{display:block;font-size:17px;color:var(--ink)}
.badge{margin-left:auto;padding:2px 10px;border:1px solid var(--line);border-radius:99px;font-weight:600;font-size:13px}
.badge.ramping,.badge.holding{border-color:var(--c);color:var(--c)} .badge.error{border-color:var(--stop);color:var(--stop)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 10px}
label{display:block;font-weight:600;margin-top:10px}
label span{font-weight:400;color:var(--mute);font-size:13px}
input,select{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);margin-top:3px}
select{width:auto;margin:0}
button{font:inherit;font-weight:600;padding:9px 12px;border-radius:6px;border:1px solid var(--btn);background:var(--btn);color:var(--btnink);cursor:pointer;width:100%;margin-top:8px}
button.alt{background:transparent;color:var(--ink);border-color:var(--line)}
button.small{width:auto;margin:0;padding:6px 10px}
button.stop{background:var(--stopbg);color:var(--stop);border:2px solid var(--stop);margin-top:10px}
button.all{width:auto;margin:0;padding:12px 22px;font-size:17px}
button.valve{width:auto;min-width:230px;margin:0}button.valve.open{background:#2e7d32;border-color:#2e7d32;color:#fff}button.valve:disabled{opacity:.5;cursor:not-allowed}
.tabs{display:flex;gap:8px}.tabs button{width:auto;margin:0;padding:9px 18px}
.pill{padding:4px 12px;border-radius:99px;border:1px solid var(--line);font-weight:600;font-size:13px}.pill.open{background:#2e7d32;border-color:#2e7d32;color:#fff}
.stagebar{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:14px 0 8px}.bigstage{margin:0;min-height:100px;font-size:20px}.bigstage small{display:block;font-weight:400;font-size:13px;margin-top:4px}.bigstage:disabled{opacity:.45;cursor:not-allowed}.stagegrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:12px}fieldset{border:1px solid var(--line);border-radius:8px;margin:0;padding:4px 12px 12px}legend{font-weight:700;padding:0 6px}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--c0);outline-offset:1px}
.msg{margin-top:8px;color:var(--stop);font-weight:600;min-height:1em} #err{padding:10px;border:1px solid var(--stop);border-radius:6px;background:var(--stopbg);color:var(--stop);font-weight:600}
.note{color:var(--mute);margin-top:8px;font-size:13px}
</style></head><body><main>
<section class="panel">
 <div id="err" hidden></div>
 <div class="top">
  <button class="stop all" onclick="send('all','stop')">STOP ALL: vent every pump to 0 and close valves</button>
  <select id="win" aria-label="Time window"><option value="30">Last 30 s</option><option value="120">Last 2 min</option><option value="600">Last 10 min</option><option value="0">Whole run</option></select>
  <button class="small alt" onclick="location.href='/api/export.csv'">Download CSV</button>
  <span class="msg" id="gmsg"></span>
 </div>
 <div class="top" id="vstrip" style="margin-top:8px"><span class="note" style="margin:0">Valves (as commanded):</span></div>
 <canvas id="chart"></canvas>
 <div class="legend" id="legend"></div>
 <div class="note">Solid line is measured pressure, dashed line is the setpoint. Green bars under the graph show when each valve is open (as commanded; the box does not report back). Esc stops all pumps.</div>
 <div class="note" id="info"></div>
</section>
<div class="tabs" id="tabs"><button id="tab_manual" onclick="tab('manual')">Manual control</button></div>
<div id="manual" style="display:grid;gap:16px">
<section class="panel">
 <h2>Valves</h2>
 <div class="top">
  <select id="port" aria-label="Valve box port"></select>
  <button class="small alt" onclick="refreshPorts()">Refresh ports</button>
  <button class="small" id="conn" onclick="toggleConn()">Connect</button>
  <span class="note" id="vinfo"></span>
 </div>
 <div class="top" id="valves" style="margin-top:10px"></div>
 <div class="msg" id="vmsg"></div>
 <div class="note">Connect only while every pump is at 0 mbar: the valves can open briefly when the box connects. The box does not report back, so each button shows the state this page last commanded.</div>
</section>
<div id="cards"></div>
</div>
<div id="tasks"></div>
</main>
<script>
const $=id=>document.getElementById(id);
let pts=[],n=0,st=null,built=0;
const val=id=>+$(id).value;
const KEYS=[['target','Target','mbar',50],['ramp','Ramp up','mbar/s',10],['deramp','Ramp down','mbar/s',10],['hold','Hold','s',5]];
function build(k){
 built=k;const box=$('cards');box.innerHTML='';$('legend').innerHTML='';
 for(let i=0;i<k;i++){
  const d=document.createElement('section');d.className='panel card';d.style.setProperty('--c',`var(--c${i})`);
  d.innerHTML=`<h2>Pump ${i+1}</h2>
  <div class="read"><div class="big"><span id="m${i}">0.0</span><small>mbar</small></div>
   <div class="sub"><b id="s${i}">0.0</b>setpoint</div><div class="sub"><b id="g${i}">0.0</b>target</div>
   <div class="badge" id="b${i}">connecting</div></div>
  <div class="grid2">${KEYS.map(([id,t,u,v])=>`<label>${t} <span>${u}</span><input id="${id}${i}" type="number" min="0" step="any" value="${v}"></label>`).join('')}</div>
  <div class="note" id="e${i}"></div>
  <button onclick="send(${i},'go')">Ramp to target and hold</button>
  <button class="alt" onclick="send(${i},'cycle')">Run cycle: up, hold, down</button>
  <button class="alt" onclick="send(${i},'depressurize')">Depressurize to 0</button>
  <button class="stop" onclick="send(${i},'stop')">STOP pump ${i+1}</button>
  <div class="msg" id="x${i}"></div>`;
  box.appendChild(d);
  KEYS.forEach(([id])=>$(id+i).addEventListener('input',()=>est(i)));est(i);
  $('legend').insertAdjacentHTML('beforeend',`<span style="color:var(--c${i})"><i></i>Pump ${i+1}</span>`);
 }
}
function est(i){const t=val('target'+i),u=val('ramp'+i),d=val('deramp'+i),h=val('hold'+i);
 $('e'+i).textContent=(u>0&&d>0)?`Cycle takes about ${Math.round(t/u+h+t/d)} s.`:'';}
async function send(i,action){
 const body={action,pump:i};
 if(i!=='all')KEYS.forEach(([k])=>body[k]=val(k+i));
 const out=$(i==='all'?'gmsg':'x'+i);
 try{const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();out.textContent=d.ok?'':d.error;}
 catch(e){out.textContent='Lost connection to the program.';}
}
addEventListener('keydown',e=>{if(e.key==='Escape')send('all','stop')});
let ports=[];
async function refreshPorts(){
 try{const d=await (await fetch('/api/ports')).json();ports=d.ports;
  const s=$('port'),cur=s.value||d.port;
  s.replaceChildren(...(ports.length?ports:[{port:'',desc:'No ports found'}]).map(p=>{const o=document.createElement('option');o.value=p.port;o.textContent=p.port?p.port+' - '+p.desc:p.desc;return o}));
  if(cur)s.value=cur;if(d.error)$('vmsg').textContent=d.error;}catch(e){}
}
async function toggleConn(){
 const c=st&&st.valves.connected;$('vmsg').textContent=c?'':'Connecting (about 2 s)...';
 try{const r=await fetch('/api/box',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c?{op:'disconnect'}:{op:'connect',port:$('port').value})});
  const d=await r.json();$('vmsg').textContent=d.ok?'':d.error;}
 catch(e){$('vmsg').textContent='Lost connection to the program.';}
}
async function setValve(n,open){
 try{const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'valve',valve:n,open})});
  const d=await r.json();$('vmsg').textContent=d.ok?'':d.error;}
 catch(e){$('vmsg').textContent='Lost connection to the program.';}
}
function setText(el,t){if(el.textContent!==t)el.textContent=t;}
function setHTML(el,h){if(el._h!==h){el.innerHTML=h;el._h=h;}}
let tasks=null;
function tab(t){$('manual').style.display=t==='manual'?'grid':'none';$('tab_manual').className=t==='manual'?'':'alt';
 Object.keys(tasks||{}).forEach(k=>{$('task_'+k).style.display=t===k?'block':'none';$('tab_'+k).className=t===k?'':'alt';});}
async function pcmd(body){
 try{const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}
 catch(e){return {ok:false,error:'Lost connection to the program.'};}
}
async function stageClick(tk,k){
 const t=tasks[tk],vals={},roles={};
 t.stages.forEach(g=>g.fields.forEach(f=>vals[f.key]=$(tk+'_'+f.key).value));
 t.roles.forEach(r=>roles[r]=+$(tk+'_role_'+r).value);
 const d=await pcmd({action:'stage',task:tk,stage:k,roles,vals,tol:$(tk+'_tol').value,rabort:$(tk+'_rabort').value});
 $(tk+'_msg').textContent=d.ok?'':d.error;
}
async function abortClick(tk){const d=await pcmd({action:'abort',rabort:$(tk+'_rabort').value});$(tk+'_msg').textContent=d.ok?'':d.error;}
function applyUni(tk){document.querySelectorAll('.'+tk+'_r').forEach(i=>i.value=$(tk+'_uni').value);}
async function loadTasks(){
 try{tasks=await (await fetch('/api/tasks')).json();}catch(e){setTimeout(loadTasks,1000);return;}
 Object.entries(tasks).forEach(([tk,t])=>{
  const b=document.createElement('button');b.id='tab_'+tk;b.className='alt';b.textContent=t.title.split(':')[0];b.onclick=()=>tab(tk);$('tabs').appendChild(b);
  const s=document.createElement('section');s.className='panel';s.id='task_'+tk;s.style.display='none';
  s.innerHTML=`<h2>${t.title}</h2><div class="note">${t.summary}</div>
   <div class="grid2">${t.roles.map(r=>`<label>${t.role_names[r]}<select id="${tk}_role_${r}" style="width:100%"></select></label>`).join('')}</div>
   <div class="stagebar">${t.stages.map((g,i)=>`<button class="bigstage" id="${tk}_b${i+1}" onclick="stageClick('${tk}',${i+1})"></button>`).join('')}</div>
   <div id="${tk}_status" style="font-weight:600"></div><div class="note" id="${tk}_live"></div>
   <button class="stop" id="${tk}_abort" onclick="abortClick('${tk}')">ABORT PROCESS: ramp every pump to 0, then close all valves</button>
   <div class="msg" id="${tk}_msg"></div>
   <h2 style="margin-top:18px">Settings</h2>
   <div class="top"><label style="margin:0">All ramp rates <span>mbar/s</span> <input id="${tk}_uni" type="number" min="0" step="any" value="10" style="width:110px"></label>
    <button class="small" onclick="applyUni('${tk}')">Apply to every ramp</button><span class="note">Each ramp rate can still be changed on its own afterwards.</span></div>
   <div class="stagegrid">${t.stages.map(g=>`<fieldset><legend>${g.title}: ${g.info}</legend><div class="grid2">${g.fields.map(f=>`<label>${f.label} <span>${f.kind==='r'?'mbar/s':'mbar'}</span><input class="${tk}_${f.kind}" id="${tk}_${f.key}" type="number" step="any" value="${f.default}"></label>`).join('')}</div></fieldset>`).join('')}</div>
   <div class="grid2"><label>Zero tolerance <span>mbar</span><input id="${tk}_tol" type="number" min="0" step="any" value="2"></label>
    <label>Abort ramp rate <span>mbar/s</span><input id="${tk}_rabort" type="number" min="0" step="any" value="50"></label></div>
   <div class="note" id="${tk}_ev" style="white-space:pre-line"></div>
   <div class="note">Settings are read when each stage starts, so you can change the next stage's values while a stage is holding. Pumps must be at 0 mbar to start. Zero is each pump's idle reading when Stage 1 starts. Valves only switch after pressure has stayed within the zero tolerance of that reading for 2 s. STOP ALL or Esc ends the process at once.</div>`;
  $('tasks').appendChild(s);
 });
}
function renderTasks(){
 if(!tasks)return;
 const p=st.process,n=st.pumps.length;
 Object.entries(tasks).forEach(([tk,t])=>{
  t.roles.forEach((r,ri)=>{const sel=$(tk+'_role_'+r);
   if(sel.options.length!==n){const cur=sel.value;sel.innerHTML='';for(let i=0;i<n;i++)sel.add(new Option('Pump '+(i+1),i));sel.value=(cur!==''&&+cur<n)?cur:Math.min(ri,n-1);}
   sel.disabled=p.running&&p.task===tk;});
  const mine=p.task===tk,run=p.running,have=t.valves.every(v=>st.valves.items.some(i=>i.n===v));
  const ready=have&&st.valves.connected&&n>=t.roles.length;
  t.stages.forEach((g,i)=>{const k=i+1,b=$(tk+'_b'+k);
   const done=mine&&(run||p.done)&&p.stage>=k,now=mine&&run&&p.busy&&p.stage===k-1&&!p.aborting;
   b.disabled=!(ready&&!p.busy&&((k===1&&!run)||(run&&mine&&p.stage===k-1)));
   setHTML(b,(done?'\u2713 ':now?'\u25B6 ':'')+'<b>'+g.title+'</b><small>'+g.info+'</small>');});
  $(tk+'_abort').disabled=!(run&&mine&&!p.aborting);
  let msg='';
  if(!have)msg='Needs valves '+t.valves.join(', ')+'; only valves '+st.valves.items.map(i=>i.n).join(', ')+' exist, so this is simulation only for now.';
  else if(!st.valves.connected)msg='Connect the valve box first (Manual tab).';
  else if(run&&!mine)msg='Another process is running.';
  else if(mine)msg=p.error||p.msg;
  const el=$(tk+'_status');el.textContent=msg;el.style.color=(mine&&p.error)?'var(--stop)':'';
  $(tk+'_live').textContent=(mine&&run)?t.roles.map(r=>{const q=st.pumps[p.roles[r]];return t.role_names[r]+' (pump '+(p.roles[r]+1)+'): '+q.meas.toFixed(1)+' mbar, setpoint '+q.sp.toFixed(1)}).join('  |  '):'';
  $(tk+'_ev').textContent=p.events.slice(-10).map(e=>e[0]+'  '+e[1]).join('\n');
 });
}
function renderStrip(){
 const bx=$('vstrip');
 st.valves.items.forEach(it=>{let e=$('vp'+it.n);
  if(!e){e=document.createElement('span');e.id='vp'+it.n;e.className='pill';bx.appendChild(e);}
  e.classList.toggle('open',it.open);e.style.opacity=st.valves.connected?'1':'.5';
  setText(e,'V'+it.n+' '+it.label+': '+(it.open?'OPEN':'CLOSED'));});
}
function renderValves(){
 const v=st.valves,box=$('valves');
 v.items.forEach(it=>{let b=$('v'+it.n);
  if(!b){b=document.createElement('button');b.id='v'+it.n;b.className='valve';b.onclick=()=>setValve(it.n,!b.classList.contains('open'));box.appendChild(b);}
  b.classList.toggle('open',it.open);setText(b,it.label+' (valve '+it.n+'): '+(it.open?'OPEN':'CLOSED'));b.disabled=!v.connected;});
 setText($('conn'),v.connected?'Disconnect':'Connect');$('port').disabled=v.connected;
 $('vinfo').textContent=v.connected?'Connected to '+v.port+'. Box says: '+(v.reply||'-'):'Not connected.';
}
async function poll(){
 try{const d=await (await fetch('/api/status?after='+n)).json();
  if(d.n<n)pts=[];n=d.n;for(const p of d.pts)pts.push(p);
  if(pts.length>150000)pts.splice(0,pts.length-150000);
  st=d;render();}
 catch(e){document.querySelectorAll('.badge').forEach(b=>{b.textContent='disconnected';b.className='badge error'});}
 setTimeout(poll,200);
}
function render(){
 if(built!==st.pumps.length)build(st.pumps.length);
 st.pumps.forEach((p,i)=>{
  $('m'+i).textContent=p.meas.toFixed(1);$('s'+i).textContent=p.sp.toFixed(1);$('g'+i).textContent=p.target.toFixed(1);
  let s=p.state;if(s==='holding'&&p.hold_left!=null)s+=` (${Math.ceil(p.hold_left)} s left)`;
  const b=$('b'+i);b.textContent=s;b.className='badge '+(p.state.startsWith('ramping')?'ramping':p.state);
  $('target'+i).max=st.max;
 });
 $('err').hidden=!st.error;$('err').textContent=st.error?'Pump error: '+st.error+'. All pumps were set to 0. Restart the program.':'';
 $('info').textContent=`${st.device}. Software cap ${st.max} mbar per pump. Logging to ${st.file}`;
 renderValves();renderStrip();renderTasks();draw();
}
function nice(x){const p=Math.pow(10,Math.floor(Math.log10(x))),f=x/p;return (f<=1?1:f<=2?2:f<=5?5:10)*p;}
function fmtT(s){s=Math.round(s);return s>=120?Math.round(s/60)+' min':s+' s';}
function draw(){
 const V=st&&st.valves?st.valves.items.length:0,SV=V?V*11+6:0;$('chart').style.height=(300+SV)+'px';
 const c=$('chart'),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;
 if(c.width!==Math.round(w*dpr)||c.height!==Math.round(h*dpr)){c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);}
 const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,h);
 if(!pts.length||!st)return;
 const css=getComputedStyle(document.documentElement),col=k=>css.getPropertyValue(k).trim();
 const L=46,R=10,T=8,B=24,pw=w-L-R,ph=h-T-B-SV,win=+$('win').value,k=st.pumps.length;
 const tEnd=pts[pts.length-1][0],tStart=win>0?tEnd-win:pts[0][0],span=Math.max(tEnd-tStart,1);
 let i0=pts.length-1;while(i0>0&&pts[i0-1][0]>=tStart)i0--;
 let ymax=10,ymin=0;
 for(let i=i0;i<pts.length;i++)for(let j=1;j<=2*k;j++){ymax=Math.max(ymax,pts[i][j]);ymin=Math.min(ymin,pts[i][j]);}
 const step=nice((ymax-ymin)*1.05/4);ymax=Math.ceil(ymax*1.05/step)*step;ymin=Math.floor(ymin*1.05/step)*step;
 const X=t=>L+(t-tStart)/span*pw,Y=v=>T+ph-(v-ymin)/(ymax-ymin)*ph;
 g.font='12px "Segoe UI",system-ui,sans-serif';g.fillStyle=col('--mute');g.strokeStyle=col('--line');g.lineWidth=1;
 g.textAlign='right';g.textBaseline='middle';
 for(let v=ymin;v<=ymax+1e-9;v+=step){g.beginPath();g.moveTo(L,Y(v));g.lineTo(w-R,Y(v));g.stroke();g.fillText(String(+v.toFixed(2)),L-6,Y(v));}
 g.textAlign='center';g.textBaseline='top';
 for(let q=0;q<=4;q++){const dt=span*(1-q/4);g.fillText(q===4?'now':'-'+fmtT(dt),L+pw*q/4,h-B+6);}
 const every=Math.max(1,Math.floor((pts.length-i0)/(pw*2)));
 const line=(idx,color,dash)=>{g.beginPath();g.strokeStyle=color;g.lineWidth=2;g.setLineDash(dash);
  for(let i=i0;i<pts.length;i+=every){const x=X(pts[i][0]),y=Y(pts[i][idx]);i===i0?g.moveTo(x,y):g.lineTo(x,y);}
  g.stroke();g.setLineDash([]);};
 for(let p=0;p<k;p++){line(1+2*p,col('--c'+p),[6,4]);line(2+2*p,col('--c'+p),[]);}
 st.valves.items.forEach((it,j)=>{
  const y=T+ph+6+j*11,bi=1+2*k+j;
  g.fillStyle=col('--mute');g.textAlign='right';g.textBaseline='middle';g.fillText('V'+it.n,L-6,y+4.5);
  g.fillStyle=col('--line');g.fillRect(L,y,pw,9);
  g.fillStyle='#2e7d32';let s0=-1;
  for(let i=i0;i<=pts.length;i++){const on=i<pts.length&&pts[i][bi]===1;
   if(on&&s0<0)s0=i;
   if(!on&&s0>=0){const x0=X(pts[s0][0]),x1=X(pts[Math.min(i,pts.length-1)][0]);g.fillRect(x0,y,Math.max(2,x1-x0),9);s0=-1;}}
 });
}
$('win').addEventListener('change',draw);addEventListener('resize',draw);
refreshPorts();loadTasks();poll();
</script></body></html>
"""

if __name__ == "__main__":
    main()