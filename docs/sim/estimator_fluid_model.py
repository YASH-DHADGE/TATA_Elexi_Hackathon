"""Fluid model of the qosd capacity estimator, v1.0 vs v1.1 (design §8.2).

Seed for the P3.0 simulation harness (build plan). Run: python estimator_fluid_model.py

What it models
- One bottleneck queue (the emulated modem, tbf with a time-based 300 ms buffer).
- Crude TCP: the aggregate sender rate A chases the tighter of {modem capacity C, our shaper R}
  with a 100 ms time constant, and overfills the modem buffer when the modem is the bottleneck.
- WHERE T_obs is measured, which is what v1.0 got wrong:
    download: arrival rate at lan0 = modem output (measured AFTER the bottleneck)
    upload:   departure rate from wan0 = min(offered, R) (measured BEFORE the bottleneck)
- Probe RTT = base RTT + modem queue delay, sampled 30 times/s, EWMA alpha 0.3.

What it does NOT model: real TCP dynamics (slow start, CUBIC), multiple flows, RTT differences,
probe noise, reflector disagreement. Its numbers are indicative; testbed runs decide.
"""

P = dict(tick=0.05, win_ticks=25, bloat=15.0, calm=5.0, calm_hold=1.0, min_tobs=0.5,
         cut_factors=(0.85, 0.70, 0.50), tobs_f=0.90, min_cut=0.5,
         drain_f=0.65, drain_enter=30.0, drain_exit=7.5, drain_max=1.5,
         rec_frac=0.15, rec_cap=0.25, rec_step=0.25, inc_f=1.05, inc_step=1.0,
         capacity_event=0.75, backoff0=2.0, backoff_max=8.0, ewma=0.2, hw_window=300.0)


def run(version, trace, direction="down", dur=60.0, dt=0.01, base=0.020, buf_s=0.3,
        R0=90.0, Rmax=100.0, Rmin=2.0, p=P):
    R = R0; A = 0.95 * R0; Q = 0.0; rtt = base; calm_since = 0.0
    state = "STEADY"; ep = 0; t_cut = -9.0; t_inc = -9.0; d_cut = 0.0
    R_target = R0; R_base = R0; drain_t = 0.0; drain_kind = ""
    backoff = 0.0; hold_until = 0.0; C_est = R0; hw = [(0.0, R0)]
    inc_since_cut = False; win = []; hist = []; probe_cycles = []
    t = 0.0; next_tick = 0.0; next_probe = 0.0
    while t < dur:
        C = trace(t); Qmax = C * buf_s
        tgt = (C * 1.03 if Q < Qmax else C) if C < R else R * 1.02
        A += (tgt - A) * dt / 0.1
        if direction == "down":
            Q = min(Qmax, max(0.0, Q + (A - C) * dt)); meas = C if Q > 0 else min(A, C)
        else:
            sent = min(A, R); Q = min(Qmax, max(0.0, Q + (sent - C) * dt)); meas = sent
        win.append(meas); win = win[-p["win_ticks"]:]
        if t >= next_probe:
            rtt = 0.3 * (base + Q / C) + 0.7 * rtt; next_probe += 1 / 30
        d = (rtt - base) * 1000.0
        bloat = d >= p["bloat"]
        if d >= p["calm"]: calm_since = t
        calm = t - calm_since >= p["calm_hold"]

        if version == "v1.0" and t >= next_tick:               # design v1.0 §8.2, 250 ms tick
            next_tick += 0.25
            T = sum(win) / len(win); L = T / R
            if L >= 0.75 and bloat and t - t_cut >= 0.5:
                R = max(Rmin, min(0.85 * R, 0.90 * T)); t_cut = t
            elif L >= 0.75 and calm and t - t_inc >= 1.0:
                R = min(Rmax, 1.05 * R); t_inc = t

        elif version == "v1.1" and t >= next_tick:             # design v1.1 §8.2, 50 ms tick
            next_tick += p["tick"]
            T = sum(win) / len(win); L = T / R
            R_hw = max(v for ts, v in hw if ts >= t - p["hw_window"])
            if not bloat: ep = 0
            if state == "DRAIN":
                if drain_kind == "capacity" and bloat and direction == "down":  # arrival == capacity while backlogged
                    R_target = max(Rmin, p["tobs_f"] * T)
                R = max(Rmin, p["drain_f"] * R_target)
                if d < p["drain_exit"] or t - drain_t >= p["drain_max"]:
                    R = R_target; state = "STEADY"
            elif bloat and T >= p["min_tobs"]:
                cap_drop = L < 0.75
                if ep == 0 or cap_drop or (t - t_cut >= p["min_cut"] and d > d_cut):
                    if cap_drop:
                        R_target = max(Rmin, p["tobs_f"] * T); drain_kind = "capacity"
                    else:
                        f = p["cut_factors"][min(ep, 2)]
                        R_target = max(Rmin, min(f * R, p["tobs_f"] * T)); drain_kind = "cut"
                    ep += 1
                    if cap_drop or d >= p["drain_enter"]:
                        state = "DRAIN"; drain_t = t; R = max(Rmin, p["drain_f"] * R_target)
                    else:
                        state = "CUTTING"; R = R_target
                    if R_target < p["capacity_event"] * R_base:    # far below pre-probe rate
                        backoff = 0.0
                    elif inc_since_cut:                             # our own probe overshot
                        backoff = min(p["backoff_max"], backoff * 2 if backoff else p["backoff0"])
                        probe_cycles.append(t)
                    hold_until = t + backoff
                    inc_since_cut = False; R_base = R_target; t_cut = t; d_cut = d
            elif L >= 0.75 and calm and t >= hold_until:
                if R < 0.97 * R_hw and t - t_inc >= p["rec_step"]:
                    R = min(Rmax, R + min(p["rec_frac"] * (R_hw - R), p["rec_cap"] * R))
                    state = "RECOVERING"; t_inc = t; inc_since_cut = True
                elif R >= 0.97 * R_hw and t - t_inc >= p["inc_step"] and R < Rmax:
                    R = min(Rmax, p["inc_f"] * R); state = "PROBING_UP"; t_inc = t; inc_since_cut = True
            if (L >= 0.95 or bloat) and state != "DRAIN":
                C_est = p["ewma"] * T + (1 - p["ewma"]) * C_est; hw.append((t, C_est))
        hist.append((t, C, R, Q / C * 1000.0))
        t += dt
    return hist, probe_cycles


def first(hist, cond, after):
    for x in hist:
        if x[0] >= after and cond(*x): return x[0] - after
    return None


def fmt(x): return "never" if x is None else f"{x:.2f} s"


if __name__ == "__main__":
    s2 = lambda t: 100.0 if t < 10 else 20.0
    s5 = lambda t: 100.0 if int(t // 20) % 2 == 0 else 40.0
    s11 = lambda t: 20.0 if t < 10 else 5.0
    for v in ("v1.0", "v1.1"):
        print(f"== {v}")
        h, cyc = run(v, s2, dur=130)
        print(f"  S2  100->20 down: first cut {fmt(first(h, lambda t, C, R, q: R < 0.95 * 89, 10))}, "
              f"modem queue < 15 ms after {fmt(first(h, lambda t, C, R, q: q < 15, 10.2))}, "
              f"max queue later {max(x[3] for x in h if x[0] > 13):.0f} ms, "
              f"probe cycles/min {sum(30 <= c < 130 for c in cyc) * 0.6:.1f}")
        h, _ = run(v, s5, dur=200)
        rec = [first(h, lambda t, C, R, q: R >= 85, k) for k in (40, 80, 120, 160)]
        dq = [first(h, lambda t, C, R, q: q < 15, k + 0.2) for k in (20, 60, 100, 140)]
        print(f"  S5  100<->40: queue < 15 ms after each drop {[fmt(x) for x in dq]}; "
              f"R >= 85 after each rise {[fmt(x) for x in rec]}")
        h, _ = run(v, s11, direction="up", dur=60, R0=18.0, Rmax=20.0, Rmin=1.0)
        print(f"  S11 20->5 up:   first cut {fmt(first(h, lambda t, C, R, q: R < 17, 10))}, "
              f"R <= C after {fmt(first(h, lambda t, C, R, q: R <= 5.0, 10))}, "
              f"queue < 15 ms after {fmt(first(h, lambda t, C, R, q: q < 15, 10.3))}")
