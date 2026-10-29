# Design Review v1.0 → v1.1 (Linux networking review)

**Date:** 2026-10-29
**Scope:** `01_SRS.md`, `02_System_Design.md`, `03_Build_Plan.md` v1.0, read against the Case Study 3 problem statement.
**Result:** the architecture holds up (eBPF marking, HTB with floors and ceilings, a delay-based estimator, a deterministic policy layer, safe-mode fallback). However, v1.0 had **4 defects that would make the headline scenarios fail**, 8 high-impact realism gaps, and a set of speed and simplicity improvements. All accepted changes are applied in the v1.1 documents. Section 5 lists what was kept and why.

Severity: **CRITICAL** = a headline scenario or a hard constraint fails as written. **HIGH** = an expert judge will spot it, or results become misleading. **MEDIUM** = speed, simplicity or robustness.

Anything that depends on kernel behaviour I could not demonstrate here is marked **[spike]** and added to design §22 as SP-8 to SP-13.

---

## 1. Critical defects

### R-01 Capacity drops are never detected, in either direction (S2 and S11 fail as written)
**v1.0:** a cut requires `L = T_obs / R ≥ 0.75` (FR-EST-3, design §8.2).
**Problem:** with TCP's ACK clocking, the traffic through a bottleneck converges to that bottleneck's capacity. When the modem drops from 100 to 20 Mbit/s, T_obs falls to about 20 while R is still 90, so L ≈ 0.22. The cut condition is never true, FR-EST-5 says "hold", and the modem queue stays full. Download is measured after the bottleneck, so this is immediate there. Upload senders also slow to the modem's rate, so the same thing happens on upload. (A first draft of this review wrongly said upload was safe; the fluid model below showed otherwise.) This is the problem statement's own second scenario.
**Evidence:** `docs/sim/estimator_fluid_model.py` runs the v1.0 logic. S2: no cut, 300 ms standing queue. S11 (upload 20 → 5): no cut.
**Fix:** a cut no longer depends on L. Bloat (2 of 3 reflectors) plus some own traffic (`T_obs ≥ 0.5 Mbit/s`) is enough. "Bloat with L < 0.75" is now read as what it is, an upstream bottleneck below R, and R jumps to 0.9 × T_obs (FR-EST-4). L still gates increases.
**Related fix:** T_obs must be measured **on the bottleneck side**. For download that is the *arrival* rate at `lan0` before our shaper (new eBPF `dir_bytes` counter). HTB's class byte counter only counts what we send, which is capped at R. In the model, using it during a drain made the estimate spiral down.

### R-02 Unclassified packets can bypass the shaper entirely
**v1.0:** `skb->priority = 0x00010000 | (class_code << 4)`, and `wan_egress` copies `skb->mark & 0xff` "the same way".
**Problem:** for a packet with no class (mark byte 0, or a `class_map` miss), that formula produces `0x00010000`, which equals the HTB qdisc handle `1:0`. HTB treats `skb->priority == sch->handle` as **HTB_DIRECT**: the packet goes to the direct FIFO and is **not rate-limited**. Every new flow's first packets, and all abstained or unclassified upload traffic, would skip the upload shaper, and the bloat would come back.
**Fix:** FR-ENF-7. The datapath writes `skb->priority` only when a valid class code (1–4) exists. Otherwise it leaves priority alone, and HTB's `default 30` applies. Packets that are forwarded or generated locally carry small priority values from `rt_tos2priority()` or the socket, which never match a class, so they fall through to the default class.
**Turned into a feature:** the direct path is now used deliberately for estimator probes and for traffic the gateway itself sends to the LAN (R-08, R-16).

### R-03 Recovery after a capacity drop takes 19–33 s
**v1.0:** increases are ×1.05 per second.
**Math:** going from 18 to 90 Mbit/s takes ln(5)/ln(1.05) ≈ **33 s**. In S5 (100 ↔ 40 every 20 s), going from 36 to 90 takes ln(2.5)/ln(1.05) ≈ **19 s**.
**Fix:** fast recovery toward `R_hw` (the highest recent saturated capacity estimate): every 250 ms, close 15% of the gap, but never step more than 25% of R. Near R_hw, keep cautious ×1.05 per second probing.
**Trade-off found in the model:** when capacity has *not* come back, each fast attempt overshoots briefly (≤ 19 ms bump in the model). Remembering the old capacity (good for S5) conflicts with not probing too often (good for latency). **Probe backoff** resolves this (FR-EST-10). A cut that lands near the pre-probe rate is a failed probe, and the pause before the next increase doubles (2 s up to 8 s). A cut far below it is a real capacity event and resets the backoff. Model results: S5 recovery 1.8–9.7 s, 7.2 probe cycles/min in steady state. The cap of 8 s is the tuning knob (table in design §8.2).
**Consequence for the SRS:** v1.0's NFR-STAB-2 ("≤ 6 direction reversals per minute") equals 3 probe cycles, which no probing estimator meets while recovering in seconds. v1.0's own ×1.05 probing would also have tripped it, and the FR-VER-4 freeze would then have blocked recovery. It is redefined as ≤ 8 probe cycles/min with ≤ 25 ms each, the freeze threshold moves to > 10 cycles/min, and NFR-UX-6 is set to ≤ 10 s.

### R-04 Draining the modem queue takes about 3 s, and upload cuts are slow
**Problem:** after the cut, `R = 0.9 × C`, so the modem queue drains at only 0.1 × C. A full 300 ms buffer takes 0.3 / 0.1 = **3 s**, plus TCP's reaction time. The detection side is not the bottleneck: at 70 Mbit/s of excess, a 15 ms queue builds in about 4 ms.
**Fix:**
- **DRAIN state** after a capacity-drop cut, or any cut with ΔD ≥ 30 ms: `R = 0.65 × R_target` until ΔD < 7.5 ms on 2 reflectors or 1.5 s pass, then `R = R_target`. For download, R_target is re-measured while the modem is still backlogged, because the arrival rate then equals capacity. For upload it is fixed at detection, because once we shape below capacity the departure rate shows R, not C.
- **Escalating cuts** within one bloat episode (×0.85, ×0.70, ×0.50), but **only while ΔD is still rising**. Without that condition, the model over-cut upload to R_min while the queue was already draining.
**Model results:** S2 modem queue below 15 ms **0.91 s** after the drop; S11 upload **2.05 s**. v1.0 never gets there in either case.

## 2. High-impact realism gaps

### R-05 No link-layer overhead compensation
HTB and CAKE count `skb->len`. Real access links add framing (DOCSIS, PPPoE, VDSL/PTM, ATM cells). Without compensation, small-packet traffic (VoIP, games, ACKs) is under-counted and bloat comes back exactly where it hurts. Judges from the CPE world will look for this.
**Fix:** FR-ENF-8; `link.overhead_bytes` / `mpu_bytes`, applied with `stab` on the HTB roots and `overhead`/`mpu` on CAKE. The testbed uses 0, because the emulated `tbf` counts the same bytes HTB does. That is stated explicitly.

### R-06 Upload ACKs of a bulk download fight for the BULK floor
**Math:** a 90 Mbit/s download generates about 3,900 ACKs/s (MSS 1448, delayed ACK). At about 66 B each, that is ≈ **2.0 Mbit/s** of upload. Those ACKs share the download's 5-tuple, so they are classified BULK on upload. The upload BULK floor is 10% × 18 = **1.8 Mbit/s**. With a backup saturating upload (S1 + S4 together), the ACK flow gets only part of that, and the download stalls far below capacity.
**Fix:** FR-ENF-9. `lan_ingress` promotes **pure ACKs** (TCP, ACK flag only, payload length 0, computed from header length fields only) from BULK to DEFAULT on upload. The upload BULK CAKE leaf also gets `ack-filter`.

### R-07 Guardrail G1 demotes legitimate high-rate real-time traffic to BULK
Cloud gaming and high-resolution screen sharing are latency-critical and commonly run above 8 Mbit/s. G1 would push them to the lowest class.
**Fix:** G1 now demotes to **STREAM**. That removes the flow from the RT ceiling without punishing it. G1 also applies to intent-pinned flows. Otherwise `scope=all` would let a device's sync job ride in RT next to the call it is supposed to protect (this was undefined in v1.0).

### R-08 Estimator probes measure our own RT queue
**v1.0:** probes use `SO_MARK` = RT, so they wait in our RT queue on `wan0`. A backlog in RT (for example a misclassified flow) inflates ΔD, and the estimator then cuts R for a queue it created itself, which can spiral.
**Fix:** probes set `SO_PRIORITY = 0x00010000` (HTB direct path, about 20 kbit/s: 30 probes/s × 84 B), so they measure only the queue **beyond** the gateway. `wan_egress` leaves priority alone when the mark byte is 0 (R-02). **[spike SP-8]**

### R-09 The ML must improve an outcome, and static CAKE is a strong opponent
The problem statement asks for AI that "improves a measurable outcome". In S1, B1 (CAKE) already protects a sparse call flow very well through its sparse-flow and per-host scheduling. B2-rules vs B2-ML may differ in accuracy but not in call latency, which would weaken the AI claim.
**Fix:**
- New **S10 (classification-sensitive):** a video call on UDP 443 (the port rule fails, and the size rule fails because video packets are > 300 B), 3 TVs streaming, a bulk download, and N greedy web/sync flows in DEFAULT. N is chosen so that DEFAULT's achievable bandwidth ÷ (N + 1) is below half the call's bitrate (for example, with DEFAULT at its 18 Mbit/s floor and N = 30, that is ≈ 0.6 Mbit/s against the ≈ 2.5 Mbit/s the call needs). If the call lands in DEFAULT it is starved by per-flow fairness; in RT it is protected. The KPI is call loss, latency and MOS, not F1.
- **B1 is configured the way an expert would deploy static CAKE** (`ingress` on the download side, `ack-filter` and `nat` on upload). Beating a weak baseline convinces nobody.
- **Deck fix:** the deck lists the Kalman filter under "Where AI is used". A Kalman filter is classical estimation, not AI. Claiming it as AI invites an easy challenge. v1.1 drops the Kalman filter altogether (R-21).

### R-10 The parser can read payload bytes as "ports" (privacy constraint)
Non-first IPv4 fragments have no L4 header, so the bytes at the "port" offset are payload. Reading them breaks FR-TEL-2 and C-1 (no payload byte read). v1.0 also did not state that IHL must be honoured (IPv4 options) or how to handle VLAN tags.
**Fix:** FR-TEL-3 rewritten. Honour IHL. If `frag_off & 0x1fff` ≠ 0, count the packet without reading ports (DEFAULT). Walk up to 4 IPv6 extension headers with a bounded loop, including the Fragment header with the same rule. Accept one 802.1Q tag. Test cases are added to P2.3.

### R-11 The RTT baseline creeps upward during sustained load
**v1.0:** the baseline rises slowly (≈ 50 s time constant) whenever a sample is above it, including during bloat. During a long `LINK_DEGRADED` period or heavy load, the baseline learns the bloated RTT and the bloat becomes invisible.
**Fix:** FR-EST-8. The baseline may rise **only while L < 0.3**. If RTT stays high while we are not loading the link, that is a route change, not bloat, so the baseline should follow it.

### R-12 CoDel's target is too small at low class rates
At 1 Mbit/s, one 1514 B packet takes 12 ms to send, which is longer than CoDel's 5 ms target. fq_codel then drops far too aggressively. This happens on upload after a cut, and in every leaf running at its floor.
**Fix:** FR-ENF-11. When R is low, set `target = max(5 ms, 1.5 × MTU time at R)` and `interval = 100 ms + (target − 5 ms)`. This is the usual low-bandwidth adjustment used by SQM setups. It is applied with `tc qdisc change` on the leaves, which does not flush them. **[spike SP-11]**

---

## 3. Speed ("blazing fast") and efficiency

For a QoS engine, "fast" means three things: low **added latency** for RT traffic, fast **reaction** to capacity changes, and low **CPU per packet** so it scales on cheap CPE. Each change below is tied to one of these.

| ID | Change | Effect |
|---|---|---|
| R-13 | **Estimator runs every 50 ms** on a sliding 250 ms throughput window (v1.0: 250 ms). Probes still arrive every ≈ 33 ms in total (3 × 10 Hz). One persistent netlink socket reads tc stats; nothing is exec'd | Detection-to-cut drops from ≤ 250 ms to ≤ 50 ms after the EWMA trips. NFR-UX-2 reaction target tightened from 1 s to **500 ms** |
| R-01/03/04 | Cut without load gate, drain phase, escalating cuts, fast recovery with probe backoff | Model: S2 queue cleared in 0.91 s (v1.0: never); S5 recovery 1.8–9.7 s (v1.0 ×1.05/s: ≈ 19 s) |
| R-14 | **Event-driven provisional classification:** the eBPF program emits a ring-buffer event when a flow reaches its 20th packet, and the classifier runs on that flow immediately (one event per flow, never per packet) | A flow's first class arrives in tens of ms instead of up to 1 s later. FR-CLS-12 (Should) |
| R-15 | **Fast lane in eBPF:** DNS (port 53), ICMP/ICMPv6 (includes Neighbour Discovery) and non-IP frames (ARP) go to RT when the flow has no class | Name lookups and address resolution don't queue behind bulk traffic. This is visible in `web_sim` page-fetch time with no classifier involved |
| R-16 | **Direct path for gateway-to-LAN traffic** (source is the gateway's own address) | Local traffic (DNS answers from the gateway, the web page) neither uses WAN shaping budget nor distorts T_obs |
| R-17 | **Explicit HTB `burst`/`cburst` = max(2 × MTU, R × 1 ms) and `quantum` on every class change** | If `quantum` is left out of a change, the kernel recomputes it from `r2q` (with warnings). `tc` and the netlink library also compute default bursts differently, so the "equivalent `tc` command" in the audit log would not actually be equivalent. Small explicit bursts also limit line-rate bursts into the modem on upload |
| R-18 | **Measure eBPF cost, don't guess:** `sysctl kernel.bpf_stats_enabled=1`, then `bpftool prog show` gives `run_time_ns` / `run_cnt` = ns per packet | A hard number for the "CPU overhead" criterion (NFR-PERF-6). Optimise the hot path only if it fails the budget |
| R-19 | **Idle-flow reaping:** remove flows idle for 30 s (5 s after TCP FIN/RST) from both maps | The 1 s batch read scales with active flows, not with 16k LRU slots, and LRU eviction (with its lock contention) almost never runs |
| R-20 | **GSO/GRO-aware counting:** `pkts += max(1, skb->gso_segs)`; size bin = `len / gso_segs` | The code stays correct on real hardware with offloads on. The testbed keeps offloads off for clean measurements, but production cannot afford to turn GRO off |
| — | Neighbour table via netlink **events** (RTNLGRP_NEIGH) instead of a 10 s poll | MAC-based intents follow address changes immediately |

**Considered and rejected:**
- `BPF_F_NO_COMMON_LRU`. Per-CPU LRU lists split capacity across CPUs, so it can evict early when most new flows land on one CPU (likely in the veth testbed). Reaping (R-19) solves the contention more safely.
- Merging `class_map` into `flow_stats` to save one lookup per packet. Userspace would have to rewrite the whole value, which races with kernel counter updates. At 100 Mbit/s, two hash lookups are a small cost; R-18 will show whether that's true on our setup.

---

## 4. Simplifications (smoother to build)

| ID | Change | Why |
|---|---|---|
| R-21 | **Drop the Kalman filter.** Report C_est = EWMA of T_obs over saturated intervals, and keep `R_hw` (high-water of C_est) for R-03 | A 1-D Kalman filter with hand-picked noise values behaves like an EWMA, but judges can question it. Removing it deletes code, tuning and the incorrect "AI" claim |
| R-22 | **No `vmlinux.h` / CO-RE.** The programs only touch the UAPI `__sk_buff` and packet headers | Simpler build, no kernel BTF needed at load time, smaller agent memory |
| R-23 | **Initial tree via one `tc -batch` file; runtime rate changes via netlink** | One exec, and the generated batch file *is* the audit record. It also avoids gaps in netlink library support (`stab`, CAKE options). Safe mode uses the same mechanism |
| R-24 | **STREAM leaf → CAKE `unlimited` with per-host fairness** (as BULK already uses) | Three TVs get per-device fairness inside STREAM, which supports NFR-UX-4 directly without new code |
| R-25 | **Verifier attribution rules:** rate changes by the estimator are never rolled back by the verifier (the estimator has its own guards). A latency regression is not blamed on a policy action while the estimator is in CUTTING/DRAIN (a capacity event explains it). Starvation right after a policy action rolls that action back; starvation with no policy action in the last 10 s means the HTB floors failed (a bug) → SAFE_MODE directly | Prevents rollback storms caused by blaming the wrong action |
| R-26 | systemd: `StartLimitIntervalSec=3600`, `StartLimitBurst=3` so systemd's restart limit matches FR-SAF-4; `ReadWritePaths=/var/lib/qosd /run/qosd` because `ProtectSystem=strict` makes everything else read-only | Without the second line the agent cannot write its state or overrides |
| R-27 | Testbed `tbf` changes must re-specify `limit`/`latency`. Two buffer models: **time** (300 ms at any rate; the NFR targets apply here) and **bytes** (3.75 MB fixed, like real modems, so the queue gets longer in time when the rate drops; reported as a stress test). `netem limit 10000` | Without this, a `tbf rate` change silently keeps or rejects the old buffer, and the experiment means something different from what we think |
| R-28 | Generators: kernel receive timestamps (`SO_TIMESTAMPNS`); an idle B0 run measures the generators' own jitter floor and reports it | Python timer jitter must not be mistaken for network jitter |
| R-29 | CI: GitHub-hosted `ubuntu-24.04` runners have passwordless sudo, so namespace/tc/eBPF integration tests can run there. Check `sch_cake` is available on the runner kernel; if not, those tests run in the VM only | Faster feedback than VM-only tests |

---

## 5. Decisions reviewed and kept

| Decision | Verdict |
|---|---|
| HTB root + leaf AQM instead of CAKE `diffserv4` alone (D4) | **Keep.** Explicit floors and ceilings are what make "no starvation" and "intent stays bounded" provable (C-4). CAKE's tins give priority, not hard guarantees |
| Download shaped on `lan0` egress, no IFB (D3) | **Keep**, under A-1. Shaping download after the bottleneck needs a margin (initial R = 0.9 × nominal) and `ingress`-mode accounting in CAKE configs, both now in place |
| `skb->mark` carries the class across forwarding (D2) | **Keep.** Mark survives forwarding and masquerade within one namespace; SP-3 verifies it |
| Go agent, Python offline, LightGBM with our own tree walker | **Keep.** A pure-Go LightGBM inference library would save about 150 lines but adds a dependency risk; the parity test is needed either way |
| tcx on kernel 6.8 | **Keep.** tcx exists from 6.6; SP-1 keeps the clsact fallback |
| Optional, off-gateway LLM | **Keep**, and don't let it compete for time before M5 |

---

## 6. Documents changed in v1.1

| Document | Changes |
|---|---|
| SRS | FR-TEL-3 (parsing rules), new FR-TEL-8/9; FR-CLS-6 (G1 → STREAM), new FR-CLS-12; FR-EST-2..8 rewritten, FR-EST-9/10 new; FR-VER-4 and NFR-STAB-2 redefined in probe cycles; new FR-ENF-7..11; FR-INT-4 (G1 applies to pinned flows); NFR-UX-2 tightened, NFR-UX-6/7 new; NFR-PERF-6 new; scenarios S10, S11; B1 definition |
| Design | §3.3, §4, §5.1–5.4, §5.6, §5.8, §5.9, §6.2, §6.4, §7, §8.1, §8.2, §9.2, §10, §12, §13.1, §16, §17, §20, §21, §22 (SP-8..13) |
| `docs/sim/estimator_fluid_model.py` | New: runnable fluid model of the v1.0 and v1.1 estimator logic; the seed of the P3.0 harness |
| Build plan | P0 spikes (13), P2/P3 tasks and exit criteria, P7 matrix (S1–S11), MVP cut line, risk register |
| **Deck (not yet changed)** | "≤1 s reaction" → "≤0.5 s" (model: 0.06–0.16 s); "9 scripted scenarios" → "11"; remove the Kalman filter from "Where AI is used" (state that the estimator is deterministic control, and the classifier is the AI component compared against rules on call latency) |
