# Build Plan: phased implementation

**Project:** Adaptive QoS Engine for Mixed Home Broadband Traffic (`qosd`)
**Document version:** 1.1 (2026-10-29: applies `04_Design_Review.md`)
**Date:** 2026-10-28
**Related:** `01_SRS.md`, `02_System_Design.md`, `04_Design_Review.md`

---

## 1. How to use this plan
- The plan has **10 phases (P0–P9)**. Each phase lists its goal, tasks, owner role, deliverables and **exit criteria**. A phase is done only when its exit criteria pass, not when its code is written.
- **Reference schedule:** 6 weeks for a team of 4. The submission deadline is not given in the problem statement, so scale the weeks to your real deadline. Keep the order; compress the duration.
- **Requirement IDs** (FR-…, NFR-…) refer to the SRS. **Section numbers** (§) refer to the system design document.
- Build the **evidence pipeline early** (P1). Every later feature is judged by a measurement, and having the measurement first stops you from building things that don't move a metric.

## 2. Team roles
One person can hold more than one role. With fewer than four people, merge R4 into the others and cut optional phases first.

| Role | Focus | Main phases |
|---|---|---|
| **R1 Kernel and networking** | VM, namespaces, tc, eBPF, testbed, impairments | P0, P1, P2 |
| **R2 Controller and backend** | Go agent: enforcer, estimator, policy, API, verifier, safety | P2, P3, P5 |
| **R3 ML and data** | Generators' labels, feature recording, dataset, LightGBM, Go evaluator, real captures | P1 (generators), P4 |
| **R4 Experiments, UI and docs** | Measurement, runner, statistics, Grafana, web page, report, docs, deck | P1 (measurement), P6, P7, P9 |

## 3. Timeline at a glance (reference 6 weeks)

| Week | R1 Kernel / networking | R2 Controller | R3 ML / data | R4 Experiments / UI / docs | Milestone |
|---|---|---|---|---|---|
| 1 | P0 spikes, P1 testbed | P0 Go skeleton, config | P1 generators | P1 measurement, B0/B1 | **M1** bufferbloat proven (end wk 1–2) |
| 2 | P2 eBPF datapath | P2 enforcer | P1/P4 record mode, first dataset | P1 report v0 | |
| 3 | P2 IPv6, hardening | P3 estimator | P4 rules + LightGBM | P6 metrics, Grafana | **M2** adaptive beats static on S2 |
| 4 | Fault injection support | P5 policy, intent, verifier, safety | P4 Go evaluator, parity, online pipeline | P6 web page | **M3** classifier at target; **M4** feature complete |
| 5 | Performance and CPU measurement | Bug fixing, P8 assistant (optional) | P4 real captures, cross-scenario eval | P7 full matrix, report | **M5** evidence complete |
| 6 | P8 hardware demo (optional) | Polish, code freeze | Model card | P9 docs, deck, demo video | **M6** submission |

**Critical path:** P0 → P1 → P2 → P3 → P5 → P7 → P9. P4 runs in parallel from week 2 but must land before P7.

---

## Phase 0: Environment and risk spikes
**Goal:** a reproducible Linux environment, a repository skeleton, and proof (or disproof) of the 13 technical assumptions in design §22 before anything depends on them. SP-8, SP-9 and SP-13 are the most important new ones: they protect the shaper against the direct-path bypass (R-02) and the estimator against its own queue (R-08).
**Owner:** R1 (spikes), R2 (repo), all (environment)

### Tasks
| ID | Task | Owner |
|---|---|---|
| P0.1 | Create an Ubuntu 24.04 LTS VM (**not WSL2**). Suggested: 4 vCPU, 8 GB RAM, 40 GB disk. Snapshot it right after install. Hyper-V is not available on Windows 11 **Home**; use VMware Workstation Pro (free for personal use) or VirtualBox 7. With Windows virtualization-based security enabled, VirtualBox may fall back to a slower execution mode, which adds timing jitter. For the final measurement runs prefer a bare-metal Ubuntu machine if one is available (`05_Improvements_and_Tech_Stack.md` §5) | All |
| P0.2 | Install the toolchain (commands below) and generate `versions.lock` | R1 |
| P0.3 | Create the repository with the layout in design §18, `Makefile` targets, `.gitignore`, `config.example.yaml`, `THIRD_PARTY.md` stub, pre-commit secret scan | R2 |
| P0.4 | CI: `go test`, Python lint and tests, secret scan on every pull request. Root integration tests (namespaces, tc, eBPF) on GitHub-hosted `ubuntu-24.04` runners, which have passwordless sudo; check whether `sch_cake` loads there, otherwise those tests run in the VM only | R2 |
| P0.5 | Run spikes SP-1 … SP-13 (design §22). Write each result (pass / fail / fallback chosen) in `docs/spikes.md` | R1 |
| P0.6 | Confirm every dependency's license from its LICENSE file; fill `THIRD_PARTY.md` | R4 |

### Environment commands (run inside the VM; record what actually gets installed)
```bash
sudo apt update
sudo apt install -y build-essential clang llvm libbpf-dev linux-tools-$(uname -r) \
     iproute2 ethtool nftables tcpdump iperf3 jq python3-venv git
# flent, netperf and irtt: check availability first, then install from apt or upstream
apt-cache policy flent netperf irtt
# Go: install a pinned release from go.dev (the distro package may be older than cilium/ebpf needs)
# CAKE availability (spike SP-4)
sudo modprobe sch_cake && lsmod | grep sch_cake
# No vmlinux.h / CO-RE needed: the programs use only UAPI headers (<linux/bpf.h>, <linux/if_ether.h>, ...) (R-22)
# Per-program eBPF cost (NFR-PERF-6): enable kernel BPF statistics while measuring
sudo sysctl -w kernel.bpf_stats_enabled=1   # then: sudo bpftool prog show  (run_time_ns / run_cnt)
# Record exact versions
{ uname -r; tc -V; clang --version | head -1; go version; bpftool version | head -1;
  iperf3 --version | head -1; python3 --version; } > versions.lock
```

### Deliverables
VM snapshot, repo skeleton, CI green, `versions.lock`, `docs/spikes.md`, `THIRD_PARTY.md`.

### Exit criteria
- [ ] All team members can build the empty agent and run `make test` in the VM.
- [ ] Each spike SP-1 … SP-13 is marked pass, or has a chosen and tested fallback.
- [ ] `versions.lock` committed.

---

## Phase 1: Testbed, traffic generators, measurement and baselines
**Goal:** prove the problem exists (bufferbloat under B0) and that a static fix helps (B1), with one command and real numbers. This is milestone **M1** and the foundation for every later claim.
**Owner:** R1 (testbed), R3 (generators), R4 (measurement, report)

### Tasks
| ID | Task | Owner |
|---|---|---|
| P1.1 | `testbed/up.sh` / `down.sh`: namespaces `c-*`, `sw`, `rtr`, `isp`, `srv`; veths; IPv4 and IPv6 addressing; routes; IPv4 masquerade on `rtr:wan0`; offloads off (design §16.1). Idempotent: running `up.sh` twice gives the same result | R1 |
| P1.2 | Impairment profiles in YAML: modem `tbf` (rate, burst, and `latency` or `limit` re-specified on every change), buffer model `time` or `bytes`, `netem` delay with `limit 10000`; a timeline format for capacity changes in both directions (`at: 20s, down_mbit: 20`, `at: 40s, up_mbit: 5`) (design §16.1) | R1 |
| P1.3 | Generators: `vc_sim`, `game_sim`, `abr_client` (+ segment server), `web_sim`; iperf3 wrappers. Every generator takes `--seed`, writes per-packet or per-event logs with kernel RX timestamps (`SO_TIMESTAMPNS`), and has a ground-truth label (design §16.2). `vc_sim` can run on UDP 443 (needed for S10) | R3 |
| P1.4 | Measurement: parse generator logs into latency percentiles, jitter (RFC 3550), loss, throughput, MOS, ABR stats; `irtt` and `flent rrul` as cross-checks | R4 |
| P1.5 | `baselines/b0.sh`, `baselines/b1_cake.sh` (design §7.4: B1 with `ingress` on download, `nat ack-filter` on upload, so the baseline is as strong as an expert would make it) | R1 |
| P1.5a | Idle calibration run: measure the generators' own jitter floor; the report shows it next to every jitter result | R4 |
| P1.6 | Minimal runner: `make run SCENARIO=s1 CONFIG=b0 REPEAT=3` → run folder with logs and metadata (versions, seed, git commit) | R4 |
| P1.7 | Report v0: S1 and S2 under B0 vs B1, with charts | R4 |

### Exit criteria
- [ ] `make testbed-up` works on a fresh VM snapshot; IPv4 and IPv6 ping work end to end.
- [ ] Under **B0**, S1 shows clearly inflated call latency during the download (expected: hundreds of ms with the 300 ms modem buffer). Record the real numbers.
- [ ] Under **B1**, S1 latency drops substantially, and S2 shows B1's weakness: bloat returns after the drop because its rate is static.
- [ ] Repeated runs with the same seed give consistent results.

---

## Phase 2: Datapath and enforcement
**Goal:** traffic is counted per flow in the kernel and lands in the right HTB class according to `class_map`. Classes are still set by hand at this stage.
**Owner:** R1 (eBPF), R2 (enforcer)

### Tasks
| ID | Task | Owner |
|---|---|---|
| P2.1 | `bpf/flow.bpf.c`: parse Ethernet (+802.1Q) → IPv4 (IHL, fragments) / IPv6 (≤ 4 extension headers) → TCP/UDP; canonical key; GSO-aware per-direction counters, histogram, inter-arrival sums; `class_map` lookup; `skb->priority` written **only** for codes 1–4, the fast lane or gateway-to-LAN traffic (FR-ENF-7); `skb->mark` for upload; pure-ACK promotion; "20th packet" ring-buffer event (design §4, §5.1, §6.2) | R1 |
| P2.2 | Build with `bpf2go` (UAPI headers only, no CO-RE); attach with tcx (or the SP-1 fallback); links not pinned (FR-SAF-3) | R1 |
| P2.3 | eBPF tests with `BPF_PROG_TEST_RUN`: IPv4 TCP/UDP, IPv6 TCP/UDP, IPv4 options, VLAN, IPv4/IPv6 fragments (assert no port read), extension-header chains, pure ACKs, GSO packets, unclassified packets (assert priority unchanged), malformed packets → `unparsed` | R1 |
| P2.4 | Enforcer: render the HTB trees and leaves on `lan0` and `wan0` (design §7, including `stab`, explicit burst/quantum, CAKE STREAM/BULK leaves) into one `tc -batch` file; runtime `class change` over netlink with explicit `quantum`/`burst`/`cburst`; low-rate fq_codel adjustment; snapshot / restore; audit log | R2 |
| P2.5 | Agent skeleton: config loading and validation, structured logging, health endpoint, systemd unit with watchdog, `StartLimit*` and `ReadWritePaths` (design §5.9), `qos-safe-mode` script | R2 |
| P2.5a | Flow reaper (idle 30 s, FIN/RST 5 s) and netlink neighbour-event subscription | R2 |
| P2.6 | Debug CLI: `qosd-ctl set-class <flow> <class>`, `qosd-ctl flows` | R2 |

### Exit criteria
- [ ] Pinning the `iperf3` flow to BULK and `vc_sim` to RT by hand produces the expected per-class byte counters (`tc -s class show dev lan0` and `wan0`), for IPv4 **and** IPv6.
- [ ] Changing the shaper rate at runtime does not flush queues (no burst of drops in `tc -s` output).
- [ ] Forcing an invalid config triggers a restore within 1 s (NFR-REL-2).
- [ ] Killing the agent detaches eBPF and the safe-mode script installs CAKE (first version of S8).
- [ ] Throughput with programs attached is within 5% of without (first NFR-PERF-3 measurement), and the cost per packet of each program is recorded from `bpftool prog show` (NFR-PERF-6).
- [ ] With the agent running but `class_map` empty, `tc -s qdisc show dev wan0` shows `direct_packets_stat` rising **only** at the probe rate: no client traffic bypasses the shaper (FR-ENF-7).

---

## Phase 3: Capacity estimator and adaptive shaping
**Goal:** the shaper follows the real capacity. This is milestone **M2**: `qosd` beats static CAKE (B1) in S2.
**Owner:** R2

### Tasks
| ID | Task |
|---|---|
| P3.0 | **Build the simulation harness first** (fluid queue model; port `docs/sim/estimator_fluid_model.py` to Go tests). It must place T_obs correctly: download measured after the bottleneck (min(offered, capacity)), upload before it (min(offered, R)). Write the S2, S5 and S11 traces as failing tests before writing the estimator; v1.0's load-gate defect (R-01) shows up only with this model |
| P3.1 | ICMP prober: 3 reflectors at 10 Hz each, `SO_PRIORITY` = HTB direct path (FR-EST-9, SP-8), IPv4 and IPv6 |
| P3.2 | Estimator per design §8.2, 50 ms tick: smoothed RTT, idle-only baseline drift, ΔD, T_obs on the bottleneck side (download: eBPF `dir_bytes` arrival counter), cut without load gate, escalating cuts only while ΔD rises, capacity-drop cut + DRAIN, fast recovery toward R_hw, probe backoff, PROBING_UP, EXTERNAL_BLOAT, LINK_DEGRADED, oscillation freeze |
| P3.3 | C_est (EWMA of saturated T_obs) and R_hw; export the true emulator rate as a metric in the testbed for MAPE |
| P3.4 | Persistent netlink socket for upload root-class byte counters; per-CPU map read for download arrival bytes (no exec on the 50 ms path) |
| P3.5 | Scenarios S2, S5 and S11 in the runner; compare B1 vs B2 (rule classifier or no classifier at this point) |
| P3.6 | Tune the defaults in design §12 **only** with evidence; record each tuning change and its effect |

### Exit criteria
- [ ] The harness tests for S2, S5 and S11 pass before testbed runs start.
- [ ] S2 and S11: shaper reacts within 500 ms of the drop; RT p95 recovery time measured and reported against the 3 s target (NFR-UX-2).
- [ ] S5: R reaches ≥ 85% of restored capacity within 10 s (NFR-UX-6); ≤ 8 probe cycles/min in steady state, each ≤ 25 ms (NFR-STAB-2); no runaway oscillation.
- [ ] Capacity MAPE during saturated periods reported (target ≤ 10%, NFR-ACC-4).
- [ ] B2 clearly better than B1 on S2 latency, with confidence intervals that don't overlap. If they overlap, investigate before moving on.

---

## Phase 4: Classification (rules → ML), runs in parallel from week 2
**Goal:** a classifier that meets the accuracy targets on held-out scenarios, a rule baseline to compare it with, and an online pipeline that is safe when wrong. This is milestone **M3**.
**Owner:** R3 (with R2 for agent integration)

### Tasks
| ID | Task |
|---|---|
| P4.1 | **Record mode** in the agent: write one feature row per flow per cycle (design §6.3) as JSON Lines, joined with generator ground truth by 5-tuple and time |
| P4.2 | Dataset generation: many randomised runs of every generator alone and in mixes; both port configurations (distinctive ports vs everything on 443); confounders (design §16.2). Fixed seeds, all stored |
| P4.3 | Splits by **run/scenario** (never by window): train / validation / test, plus a cross-scenario holdout (train on some scenarios, test on others) |
| P4.4 | Rule classifier (design §8.1) implemented in Go; evaluated on the same test sets |
| P4.5 | LightGBM training (`ml/train.py`): multiclass, early stopping; feature importance; calibration (reliability diagram, ECE) |
| P4.6 | Threshold selection on the validation set: abstain (0.60), switch (0.85) and windows (3); plot selective accuracy vs coverage |
| P4.7 | Export: `dump_model()` JSON + `model.meta.json` (schema version, feature order, thresholds, SHA-256, dataset hash) |
| P4.8 | Go tree evaluator + parity test (max probability difference ≤ 1e-6 vs Python on the test set) |
| P4.9 | Online pipeline: overrides, smoothing, hysteresis, guardrails G1 (→ STREAM, also for pinned flows) and G2, abstain, fallback to rules on model failure, event-driven provisional class at packet 20 (record mode also emits these partial-window rows so the model is trained on them) (FR-CLS-*) |
| P4.10 | Real-world check: consented `tcpdump -s 96` captures from team devices; offline extractor with parity against testbed pcaps; report separately (NFR-ACC-3) |
| P4.11 | Model card: data, splits, metrics, known failure cases, limits |

### Exit criteria
- [ ] Held-out testbed: selective accuracy ≥ 98% at coverage ≥ 90% and macro-F1 ≥ 0.95, **or** the real numbers are reported with an analysis of the gap.
- [ ] ML vs rules: per-class F1 and confusion matrices side by side; the "everything on port 443" condition included.
- [ ] ML vs rules **on an outcome**: S10 call loss, p95 latency and MOS under B2-rules vs B2-ML (NFR-UX-7). If the difference is not significant, report that honestly; it is the result the problem statement asks for.
- [ ] Go evaluator parity test passes.
- [ ] Replaying recorded traces shows no flow changing class more than 2 times/min (NFR-STAB-3).
- [ ] Corrupting `model.json` makes the agent fall back to rules with an alert.

> **Honesty rule:** testbed accuracy is measured on our own synthetic traffic. The report must state this next to the number and show the real-capture result separately, even if it is lower.

---

## Phase 5: Policy engine, intents, verifier and safety
**Goal:** the whole control loop works with bounds, intents, verification and a safe fallback. Together with P6 this is milestone **M4**.
**Owner:** R2

### Tasks
| ID | Task |
|---|---|
| P5.1 | Policy FSM (design §9.1); profiles NORMAL / INTENT; validation of floors, ceilings and sums (FR-POL-*); dwell time; decision log |
| P5.2 | Intent leases: create / list / cancel / expire; max 3; device by IP or MAC (neighbour table); `scope` realtime / all; pinning rule (FR-INT-4) |
| P5.3 | REST API (design §11): token auth, LAN-only listener, schema validation, rate limit, error codes |
| P5.4 | Overrides: per flow and persisted rules; corrected labels logged for retraining (FR-CLS-8, FR-CLS-11) |
| P5.5 | Verifier (design §8.3): verdicts, rollback on safety violation, SAFE_MODE after 2 rollbacks in 5 min, oscillation freeze hook |
| P5.6 | Safety: safe config on start (FR-SAF-1), watchdog, `ExecStopPost`, SAFE_MODE retry policy (FR-SAF-4), `/run/qosd/state.json` for last R |
| P5.7 | Fuzz test: random inputs never produce a config violating the bounds |

### Exit criteria
- [ ] S6: a forced misclassification is corrected through the API and takes effect within one cycle.
- [ ] S7: intent start → INTENT profile visible in `tc` within 1 s; expiry → NORMAL within 1 s of `expires_at`.
- [ ] S8: SAFE_MODE installed within 10 s of `kill -9` (NFR-REL-1); traffic never left unshaped.
- [ ] Injected starvation or latency regression causes a rollback; a second one within 5 min causes SAFE_MODE.
- [ ] Bounds fuzz test passes.

---

## Phase 6: Observability and user interface
**Goal:** a judge can watch the system work live, and a household user can set an intent without curl.
**Owner:** R4 (with R2 for metrics)

### Tasks
| ID | Task |
|---|---|
| P6.1 | Prometheus metrics (design §13.1) |
| P6.2 | Prometheus + Grafana on the host/laptop side; provisioned dashboard JSON in `dashboards/` |
| P6.3 | Dashboard panels: latency, jitter, loss, throughput per class, queue depth, Jain's fairness, shaper vs estimate vs true rate, classifier confidence, policy timeline, decision annotations |
| P6.4 | Minimal intent web page embedded in the agent (`GET /`): device list, "prioritise for N minutes", active intents, flow list with "correct class" |

### Exit criteria
- [ ] All six problem-statement dashboard quantities (latency, jitter, loss, throughput, queue depth, fairness) are visible during a live S1 run.
- [ ] An intent can be created and cancelled from the web page; it shows up in the dashboard timeline.

---

## Phase 7: Experiment automation and full evaluation
**Goal:** the complete, reproducible evidence set. This is milestone **M5**.
**Owner:** R4 (all roles support)

### Tasks
| ID | Task |
|---|---|
| P7.1 | Runner completes the matrix: S1–S11 × {B0, B1, B2-rules, B2-ML}; ≥ 5 repeats (10 for S1, S2, S10); S2, S5, S11 once more with the bytes buffer model (stress test); INVALID runs kept and explained |
| P7.2 | Analysis: all metric formulas in design §17; mean ± 95% CI; starvation check; adaptation time; stability counts |
| P7.3 | Report generator: one Markdown/HTML report with charts per scenario, a summary table against every SRS target (met / not met), and environment metadata |
| P7.4 | Overhead: agent CPU and memory at 100 Mbit/s; eBPF forwarding cost and ns per packet per program (`kernel.bpf_stats_enabled`); one S1 run with offloads on (GSO-aware counting); optionally on a Raspberry Pi 4 (labelled as hardware) |
| P7.5 | `make demo SCENARIO=s2`: reset, inject, collect, report in one command (problem-statement requirement) |

### Exit criteria
- [ ] Every SRS target has a row in the report with the measured value and met / not met.
- [ ] A teammate who did not write the runner reproduces one scenario from the setup guide on a fresh VM snapshot.
- [ ] `make demo` works end to end.

---

## Phase 8: Optional extensions (only after M5)
**Goal:** extras that strengthen the story without risking the core.

| ID | Task | Owner |
|---|---|---|
| P8.1 | LLM assistant (design §5.13): local model through Ollama or a hosted API; tools `create_intent`, `get_state`, `get_decisions`, `list_flows`; JSON-schema validation; 100-utterance evaluation vs a regex parser | R2 |
| P8.2 | Hardware demo: run the agent on a Raspberry Pi 4 (or an OpenWrt-capable router) with a USB Ethernet adapter; measure CPU; label results as hardware | R1 |
| P8.3 | Early prototype of one-way-delay direction attribution | R2 |

Exit criteria: each extension has its own measured result, and the system still passes P7 with the extension disabled (FR-LLM-4).

---

## Phase 9: Documentation, packaging and submission
**Goal:** everything the problem statement lists under "Expected Output and Acceptance Evidence", polished. This is milestone **M6**.
**Owner:** R4 (all review)

### Tasks
| ID | Task |
|---|---|
| P9.1 | Architecture diagram (device, edge, network, data, analytics, UI): design §3 |
| P9.2 | Setup guide: prerequisites, exact versions (`versions.lock`), commands, configuration, verification steps; tested from a fresh snapshot |
| P9.3 | Known-limitations section (design §21) with emulation disclosures (C-8) |
| P9.4 | Final results report from P7 |
| P9.5 | `THIRD_PARTY.md` complete; repository scanned for secrets |
| P9.6 | Presentation deck (Teliport template) and a 3–5 minute demo video of `make demo` plus the dashboard |
| P9.7 | Code freeze, tag the release, final reproducibility check |

### Submission checklist (problem statement → artefact)
| Problem-statement expected output | Artefact |
|---|---|
| Traffic-class and link-capacity estimator | `internal/classifier`, `internal/estimator`, model card, P4/P3 results |
| Dynamic QoS policy engine and enforcement module | `internal/policy`, `internal/enforcer`, `bpf/` |
| Dashboard (latency, jitter, loss, throughput, queue depth, fairness) | `dashboards/` + screenshots in the report |
| Automated baseline-vs-optimised experiments | `experiments/`, final report |
| API or UI for temporary service intent | REST API + web page |
| Architecture diagram | Design §3, exported image in the deck |
| Setup guide with exact versions, commands, configuration, verification | `docs/SETUP.md`, `versions.lock` |
| Scripted demo: reset, inject, collect, report | `make demo` |
| Known limitations | `docs/LIMITATIONS.md` (from design §21) |

---

## 4. Definition of done (every task)
- Code merged through a reviewed pull request; CI green (tests, lint, secret scan).
- Unit tests for new logic; integration test if it touches the kernel.
- Inputs, outputs, failure handling and success condition documented in the module's README (mirrors design §5).
- If it changes a default, the change and its measured effect are recorded.

## 5. Scope control: the MVP cut line
If time runs short, cut from the bottom up. Never cut anything above the line.

| Keep (MVP) | Cut first (in this order) |
|---|---|
| Testbed + B0/B1 evidence (P1) | P8 hardware demo |
| eBPF datapath + HTB enforcement (P2) | P8 LLM assistant |
| Adaptive estimator (P3) | Real-world captures (P4.10); state the gap in limitations |
| Rule classifier + LightGBM with comparison (P4.4–P4.9) | Persisted override rules (keep per-flow overrides) |
| Intents, verifier, SAFE_MODE (P5) | Web page (keep the REST API + curl examples) |
| Dashboard with the six quantities (P6.1–P6.3) | Event-driven provisional classification (FR-CLS-12; the 1 s cycle still classifies) |
| Estimator fixes R-01..R-04 (cut without load gate, drain, escalating cuts, fast recovery) and direct-path probes | Bytes buffer model stress runs |
| Datapath safety: FR-ENF-7 (no direct-path bypass), safe parsing (FR-TEL-3) | Fast lane and pure-ACK promotion (FR-ENF-9/10) |
| Automated matrix + report + `make demo` (P7) | Repeats beyond 5 |

## 6. Risk register
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| eBPF learning curve slows P2 | Medium | High | Spikes in P0; fallback is AF_PACKET header capture of the first 64 packets per flow (no other design change) |
| A spike fails (e.g. HTB via `skb->priority`) | Low–medium | Medium | Fallbacks listed in design §22, decided in P0 |
| Synthetic traffic too easy, so accuracy looks unrealistically high | High | Medium | Randomised parameters, confounders, port-443 condition, cross-scenario holdout, real-capture check, honest reporting |
| Estimator oscillates or recovers slowly | Medium | High | Simulation harness first, with T_obs placed correctly (P3.0); two-speed recovery; evidence-based tuning; freeze guard |
| Unclassified traffic silently bypasses the shaper via HTB's direct path | Medium (easy mistake) | High | FR-ENF-7, SP-9, unit test, P2 exit criterion on `direct_packets_stat` |
| B1 (static CAKE) matches B2 on S1, so the classifier shows no outcome benefit | High | High | S10 designed to need correct classification (NFR-UX-7); report S1 honestly. B2 also wins on S2, S5, S7 and S11 through adaptation and intents |
| Model trained with offloads off behaves differently with GRO on | Medium | Medium | GSO-aware counting; one offloads-on run; deployment offload setting recorded in the model card |
| Testbed results not reproducible across machines | Medium | High | Pinned versions, VM snapshot, seeds, offloads off, repeated runs with CIs |
| Team member unavailable | Medium | Medium | Two people know each module (pair reviews); docs per module |
| Time pressure near the deadline | High | High | MVP cut line; code freeze 1 week before submission |
| License problem discovered late | Low | Medium | Licenses confirmed in P0 |

## 7. Ways of working
- **Branching:** `main` protected; feature branches; small pull requests reviewed by a second role.
- **Daily:** 15-minute sync on blockers and on which exit criteria are passing.
- **Weekly:** run the current scenario set and look at the numbers together; decide next week's priorities from the evidence.
- **Experiment log:** every tuning run gets a line (date, change, scenario, result) so the final report can explain each default.
