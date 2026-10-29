# Improvements and Tech Stack: Reasoning

**Date:** 2026-10-29
**Applies to:** design v1.1 (`02_System_Design.md`), after the review in `04_Design_Review.md`
**Purpose:** reason from first principles about where `qosd` can still be improved, and which technology to use in each layer for the best results within a hackathon. Statements that need checking on the real system are marked **[verify]**. Versions are not pinned here; P0 pins them in `versions.lock`.

---

## 1. Start from what is scored

Every improvement should move at least one number the judges will look at. The problem statement scores five things:

| Evaluation criterion | Mechanism that moves the number | Where the evidence comes from |
|---|---|---|
| Accuracy of classification and link estimation | Classifier features and training data; estimator design | Held-out testbed F1, real-capture F1, capacity MAPE |
| Reduction in interactive latency, jitter, loss | **Shaping below capacity** (estimator), **FQ + AQM** at the gateway, then class guarantees | Per-packet one-way delay from `vc_sim`/`game_sim`, S1–S11 vs B0/B1 |
| Fairness and no starvation | HTB floors, per-host CAKE leaves, guardrails | Jain's index, starvation check |
| Adaptation speed and policy stability | Estimator reaction, drain, recovery, backoff | Reaction time, recovery time, probe cycles/min |
| CPU overhead, reproducibility, clarity | In-kernel datapath, measured ns/packet, seeds, one-command runner | `bpftool` stats, agent CPU, report |

The conclusion that drives the rest of this document: **the evidence pipeline is part of the product**. A mechanism without a clean, repeatable measurement earns little.

---

## 2. Where interactive latency actually comes from

A call packet's latency in the testbed breaks down as follows (time-based 300 ms modem buffer):

| Component | No QoS (B0) | With shaping below capacity + FQ/AQM | What controls it |
|---|---|---|---|
| Base path RTT | 20 ms | 20 ms | Physics (netem) |
| Modem queue (bufferbloat) | up to **300 ms** | ≈ 0, except short probe bumps (≤ 19 ms in the model) and the first ≈ 1 s after a capacity drop | **The estimator**: R must stay below the real capacity |
| Gateway queue, sparse flow (call audio, game) | n/a (FIFO is tiny in B0) | ≈ 0–1 ms: fq_codel/CAKE serve new and sparse flows first | FQ scheduling |
| Gateway queue, non-sparse flow (call video) | n/a | up to ≈ 5 ms (CoDel target) when it shares a class with heavy flows | AQM; **class guarantees** under contention |
| Serialization of one 1514 B packet | 0.12 ms at 100 Mbit/s, 0.6 ms at 20 Mbit/s | same | Link rate |
| Wi-Fi (real homes) | not modelled | not modelled | Often the real bottleneck; out of scope |

**The levers, ranked by how much latency they remove:**
1. **Keeping the shaper below the real capacity** (the estimator). This removes hundreds of milliseconds. It is the biggest lever, and v1.0 got it wrong (R-01).
2. **FQ + AQM at the gateway.** This keeps sparse real-time flows near zero queueing even inside a busy class.
3. **Class bandwidth guarantees.** These matter only under contention, when a flow that needs bandwidth (video call, cloud gaming) would otherwise get a 1/N share.
4. **Intents.** These are lever 3 applied on demand.

**What this means for effort:** most of the latency win comes from levers 1 and 2. Static CAKE (B1) already has lever 2 and a static version of lever 1. `qosd` can beat B1 clearly only where **capacity changes** (S2, S5, S11), where **a user intent** is needed (S7), or where **classification decides bandwidth under contention** (S10). In S1 at a steady capacity, expect **parity with B1**, and say so. Over-claiming against a good baseline is the fastest way to lose credibility with network engineers.

| Scenario | Expected result vs B1 | Why |
|---|---|---|
| S1 download during a call, steady link | ≈ parity | Both shape below capacity; both have FQ |
| S2 / S5 / S11 capacity changes | **qosd clearly better** | B1's static rate sits above the new capacity, so bloat returns |
| S3 3 TVs + game | Modest difference | B1 has per-host fairness; qosd adds class priority for the game |
| S7 intent | **qosd better** for the prioritised call | B1 has no notion of intent |
| S10 classification-sensitive | **B2-ML better than B2-rules** (the AI claim); vs B1 unknown | Measure; don't predict |

---

## 3. Remaining weaknesses in v1.1, and how to improve them

Ranked by impact on the scored criteria divided by effort. Effort: **S** = about a day, **M** = a few days, **L** = a week or more.

### W1 The measurement platform on Windows 11 Home (impact: high, effort: S)
- Hyper-V is not available on Windows 11 **Home**. VirtualBox or VMware Workstation Pro must be used.
- With Windows virtualization-based security (Memory integrity) switched on, VirtualBox may fall back to a slower execution mode via the Windows Hypervisor Platform. **[verify]** on this machine.
- A VM sharing the CPU with Windows adds timing jitter to exactly the quantities we report (jitter, p99 latency).

**Improve:** develop in the VM, but run the **final evidence matrix on bare-metal Ubuntu** if any machine allows it (a spare laptop or an old PC). Always run the idle calibration (P1.5a) and report the generator jitter floor next to every jitter number. Inside the VM, pin vCPUs and keep the host idle during runs.

### W2 Classifier trained only on synthetic traffic (impact: high, effort: M)
Accuracy on our own generators says little about real traffic, and judges know it.

**Improve:**
- **Early-packet features.** Sizes, directions and inter-arrival times of a flow's first 8–10 packets are the strongest signals for encrypted-traffic classification, and work for QUIC. They are cheap in eBPF: store 8 sizes as signed 16-bit values (sign = direction) in `flow_val`, 16 B per flow. They also fit the event-driven provisional class at packet 20 (FR-CLS-12).
- **Cross-check on a public dataset.** For example, CESNET-QUIC22 provides per-packet information for the first packets of real QUIC flows. Its labels are service names, which need a documented mapping to our four treatment classes. **[verify]** the license and label coverage before using it. Report it separately, like the real captures.
- **Calibrate probabilities** with isotonic or temperature scaling on the validation split. The abstain and switch thresholds (0.60 / 0.85) only mean something if confidence is calibrated; report expected calibration error before and after.

### W3 The verifier sees probes, not users (impact: high, effort: M)
Probe RTT measures the queue beyond the gateway, not what applications experience. "Did the policy improve user experience" is the problem statement's own wording.

**Improve:** add **passive TCP RTT** measured in eBPF from TCP timestamp options: match the TSval going out with the TSecr coming back, per flow. This is the technique of passive ping (pping) and its eBPF implementation ePPing (xdp-project). It gives real RTT for web and streaming flows with no extra traffic and no payload reading. Limits: TCP only (QUIC and UDP calls stay with the probes and generator measurements), and only for connections that use timestamps. Feed it to the verifier as a second KPI and to the dashboard.

### W4 Direction attribution (impact: medium, effort: S in the testbed)
RTT cannot tell which direction is congested, so both directions are cut when both are loaded.

**Improve:**
- **Testbed:** a small UDP reflector in `srv` that echoes with its receive timestamp. All namespaces share one clock, so one-way delay is exact, and the estimator can cut only the congested direction. Label this as a testbed-only advantage.
- **Real world:** ICMP timestamp requests give millisecond one-way delay deltas when clocks are not synchronised. A constant clock offset cancels out against the baseline. **[verify]** that reflectors answer them.

### W5 Real-internet reflectors (impact: medium in production, effort: S)
Public hosts rate-limit or deprioritise ICMP, and a single reflector's path can be congested on its own.

**Improve:** a reflector pool (for example 6–10 well-spread anycast DNS resolvers), health scoring, and replacement of reflectors that stop answering or whose baseline drifts. Keep the 2-of-N vote. The testbed does not exercise this, so document it as a production item.

### W6 Intent UX names devices by IP or MAC (impact: medium for the demo, effort: S)
**Improve:** show device names from the gateway's DHCP leases (hostname) and let the user rename them. "Prioritise *Work laptop* for 1 h" demos much better than an IP address. No payload or DPI is involved.

### W7 Portability of the datapath (impact: medium, effort: S–M)
tcx needs kernel ≥ 6.6. OpenWrt 24.10 ships 6.6, but OpenWrt 23.05 (5.15) and many RDK-B/prplOS builds use older kernels.

**Improve:** implement the clsact attach path as a real, tested fallback (not only a spike fallback), selected at runtime. cilium/ebpf supports both. This also strengthens the production story for Tata Elxsi's CPE audience.

### W8 CPU at gigabit rates (impact: low for the prototype, high for production; effort: M)
HTB serialises on one qdisc lock, and software shaping at gigabit is CPU-bound on low-cost CPE.

**Improve:** keep HTB for the provable floors and ceilings, and document a **"lite" backend** for weak CPUs: one CAKE `diffserv4` instance per direction, with the eBPF program choosing the tin through `skb->priority` (CAKE accepts a tin override when the priority's major number equals its handle **[verify]**). Trade-off: CAKE tins give priority, not hard floors. Measure both on the Raspberry Pi if time allows.

### W9 Wi-Fi hop (impact: high in real homes; out of scope)
**Improve later:** on LAN egress, write the class into the DSCP field, so an integrated access point maps RT to the Voice/Video Wi-Fi access categories. This needs IP checksum updates in eBPF and a test with a real AP. Future work.

### W10 Integration story for a CPE vendor (impact: medium with these judges, effort: S, documentation only)
Tata Elxsi works on RDK-B and CPE software. A short table that maps our objects to the TR-181 `Device.QoS.` data model (Classification, Queue, Shaper), and intents to TR-369 (USP) operations, shows a production path without building it. Add it to design §21.

---

## 4. Tech stack, layer by layer

For each layer: what we use, what else was considered, and why. **Verdict:** Keep / Change / Add.

| Layer | Choice | Alternatives considered | Reasoning | Verdict |
|---|---|---|---|---|
| Dev host | Windows 11 Home + VMware Workstation Pro or VirtualBox 7; VS Code Remote-SSH into the VM | Hyper-V (not on Home); WSL2 | WSL2's kernel and qdisc support differ from a normal distro kernel, which hurts reproducibility. Use bare metal for the final runs (W1) | **Change** (Hyper-V removed) |
| OS / kernel | Ubuntu 24.04 LTS, GA kernel 6.8 | Ubuntu HWE kernel; Debian | tcx (≥ 6.6), CAKE and BTF available; LTS for the setup guide | Keep |
| eBPF programs | C, compiled with clang; UAPI headers only | Rust (Aya); bpftrace | C is the reference language for tc programs, with the most examples; no CO-RE needed | Keep |
| eBPF loader and maps | `cilium/ebpf` + `bpf2go` (pure Go, objects embedded in the binary) | libbpf (C); Aya | No cgo: `CGO_ENABLED=0` gives a static binary that cross-compiles to arm64 (Raspberry Pi, OpenWrt) with one command | Keep |
| Attach point | tcx, with clsact as a tested runtime fallback | XDP | XDP has no `skb` (cannot set `skb->priority`) and no egress hook, so it cannot do this job | **Add** clsact path (W7) |
| Scheduling / shaping | HTB root; fq_codel leaves (RT, DEFAULT); CAKE `unlimited` leaves (STREAM, BULK) | CAKE `diffserv4` alone; fq_pie | Hard floors and ceilings are the anti-starvation proof; CAKE-only as the "lite" option | Keep; document the lite option (W8) |
| tc control | `tc -batch` at init; `vishvananda/netlink` for runtime class changes | `florianl/go-tc`; exec `tc` | Batch file = audit record and full `tc` feature set; netlink for fast changes (SP-13) | Keep |
| Agent | Go (current stable at P0), one static binary, `GOMEMLIMIT` set | Rust; Python | Go's GC is irrelevant at 50 ms / 1 s ticks; a single language with the generators and tooling | Keep |
| HTTP API + web page | Go standard library `net/http`, page embedded with `embed` | Gin/Echo; a separate SPA | Zero extra dependencies; the page is small | Keep |
| Model training | Python, LightGBM, scikit-learn (splits, calibration, metrics) | XGBoost; neural networks | Small tabular features: gradient-boosted trees are the right tool; deep learning adds data needs and nothing measurable here | Keep; **add** calibration (W2) |
| Model inference on the gateway | Own Go tree walker over LightGBM `dump_model()` JSON + parity test | `leaves` (pure-Go LightGBM reader); `m2cgen` (generates Go code); ONNX runtime | About 150 lines, no dependency, microsecond inference; the parity test (≤ 1e-6) proves it | Keep |
| Data for analysis | Generator logs as Parquet; analysis with pandas or Polars, DuckDB for ad-hoc queries | CSV; Prometheus export only | Per-packet logs from about 200 runs grow quickly; Parquet is compact and fast to load | **Add** Parquet |
| Timing-critical generators (`vc_sim`, `game_sim`) | **Go**, with kernel receive timestamps | Python | Precise pacing at 50–128 packets/s without interpreter jitter; same toolchain as the agent | **Change** from Python |
| Other generators | Python (`abr_client`, `web_sim`); `iperf3`; `irtt` and `flent` as cross-checks | Replaying captured pcaps | Second-scale behaviour is fine in Python. Replay is open-loop (no congestion control), so it cannot be used to evaluate QoS | Keep |
| Testbed | `ip netns` + veth + Linux bridge, bash scripts driven by the YAML scenarios | Mininet; containers | Namespaces are exactly what the design needs, with no extra layers | Keep |
| Metrics and dashboard | Prometheus + Grafana, off-gateway, provisioned from files | VictoriaMetrics (Prometheus-compatible, lighter) | Standard and familiar to judges; switch only if resources are tight | Keep |
| Report | Python script → Markdown/HTML with matplotlib charts, CI tables per SRS target | Notebooks | A script is reproducible from one command (FR-EXP-1); notebooks are not | Keep |
| CI | GitHub Actions, `ubuntu-24.04` runners (sudo for namespaces/tc/eBPF); gitleaks | VM-only tests | Fast feedback on every pull request; check `sch_cake` availability on the runner kernel **[verify]** | Keep |
| Optional LLM | Hosted: a current Claude model with tool use and a JSON schema for intents (a small, fast model such as Claude Haiku 5.5 is enough for intent parsing); local: a small instruct model via Ollama | LLM inside the control loop | Parsing and explanations only; the policy engine validates everything; the system works with it off (FR-LLM-4) | Keep optional |
| Hardware demo | Raspberry Pi 5 + USB 3 Ethernet adapter, or an OpenWrt 24.10 router (kernel 6.6) with hardware flow offload **disabled** | x86 mini-PC | Shows ns/packet and CPU on real low-cost hardware; label results as hardware | Keep optional |

**Deliberately not used:**
- **DPI or TLS SNI reading.** It reads payload bytes (violates FR-TEL-2 and the spirit of C-1), and Encrypted Client Hello removes it anyway.
- **ML in the per-packet path.** All inference stays in user space on per-flow summaries.
- **An LLM in the decision loop.** Latency, safety, and the problem statement's warning.
- **Microservices, Kubernetes, message brokers.** One agent on one gateway; extra moving parts add failure modes and no measurable benefit.
- **pcap replay for QoS experiments.** Replayed traffic does not react to queues, so it cannot show bufferbloat or its fix.

---

## 5. Recommendations

### Adopt now (small effort, directly moves a scored number)
| # | Change | Moves | Effort |
|---|---|---|---|
| A1 | Final evidence runs on bare-metal Ubuntu if available; calibration run always (W1) | Credibility of jitter and latency numbers | S |
| A2 | `vc_sim` and `game_sim` in Go with kernel receive timestamps | Measurement accuracy | S |
| A3 | Early-packet features (first 8 sizes with direction) in `flow_val` and the feature schema (W2) | Classification accuracy, faster provisional class | S |
| A4 | Probability calibration on the validation split (W2) | Meaningful confidence (constraint C-5) | S |
| A5 | Device names from DHCP leases in the intent page (W6) | Demo clarity | S |
| A6 | clsact fallback implemented and tested (W7) | Portability, production story | S–M |
| A7 | TR-181 / USP mapping table in the limitations (W10) | Production-boundary clarity | S |
| A8 | Generator logs as Parquet | Faster analysis of about 200 runs | S |

### Stretch (after M5, if time allows)
| # | Change | Moves | Effort |
|---|---|---|---|
| B1 | Passive TCP RTT (ePPing-style) as a verifier KPI and dashboard panel (W3) | "Verifies user experience" | M |
| B2 | One-way delay via a timestamping UDP reflector in the testbed (W4) | Upload/download attribution, stability | S–M |
| B3 | Public QUIC dataset cross-check, with a documented label mapping (W2) | Classification credibility | M |
| B4 | CAKE-only "lite" backend measured on the Raspberry Pi (W8) | CPU-overhead criterion | M |

### Future work (document only)
Reflector pool management (W5), DSCP → Wi-Fi access-category mapping (W9), L4S support (check whether the target kernel ships the DualPI2 AQM **[verify]**), federated retraining, hardware-offload integration.

---

## 6. Summary of the reasoning
1. **Most of the latency win comes from keeping the queue out of the modem**, so the estimator is the most important component. v1.1 fixed its defects, and its evidence must be measured with a clean setup.
2. **Static CAKE is a strong baseline.** `qosd`'s honest advantages are adaptation to capacity changes, user intents, and bandwidth guarantees under contention. The scenarios and the deck should argue exactly these.
3. **The AI earns its place only through an outcome** (S10). Early-packet features, calibrated confidence and a real-traffic cross-check make the classifier claim robust.
4. **The stack stays small:** C eBPF + one Go binary on the gateway; Python, Prometheus and Grafana off the gateway. The main changes are about measurement quality (bare metal, Go generators, Parquet), not about adding technology.
