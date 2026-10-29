# Software Requirements Specification (SRS)

**Project:** Adaptive QoS Engine for Mixed Home Broadband Traffic (working name: `qosd`)
**Hackathon:** Tata Elxsi Teliport Season 4, Case Study 3
**Document version:** 1.1 (2026-10-29: estimator, datapath and scenario fixes from `04_Design_Review.md`)
**Date:** 2026-10-28
**Structure:** loosely follows ISO/IEC/IEEE 29148:2018 (requirements engineering)

---

## 1. Introduction

### 1.1 Purpose
This document states **what** the system must do and **how well**, in testable terms. Design decisions (how) live in `02_System_Design.md`. The phased build plan lives in `03_Build_Plan.md`.

### 1.2 Scope
`qosd` is an intent-aware Quality-of-Service (QoS) controller for a Linux-based home gateway. It:

1. Observes traffic **metadata only** (headers, sizes, timing). It never reads or decrypts payloads.
2. Classifies flows into four broad *treatment* classes and exposes a confidence for every decision.
3. Estimates the WAN link capacity continuously, for upload and download separately.
4. Enforces queueing and shaping with Linux traffic control (`tc`: HTB, fq_codel, CAKE) and eBPF.
5. Accepts temporary user intents (e.g. "prioritise my work call for 1 hour") while keeping guaranteed minimums for every class.
6. Verifies whether each policy action improved user experience, and rolls back actions that cause harm.
7. Ships with a reproducible emulated testbed, baseline-vs-optimised experiments, a dashboard and an auto-generated results report.

**Out of scope:** Wi-Fi airtime scheduling, ISP-side QoS, multi-WAN, DPI or TLS interception, production hardware certification. These are discussed under limitations.

### 1.3 Definitions

| Term | Meaning |
|---|---|
| Flow | Packets sharing a 5-tuple: client IP, client port, remote IP, remote port, L4 protocol |
| Treatment class | One of RT, STREAM, DEFAULT, BULK (section 3.2). Defined by how traffic must be queued, not by application name |
| Shaper rate (R) | Rate at which `qosd` limits traffic, set slightly below the real bottleneck so the queue forms where `qosd` controls it |
| Bufferbloat | Excess latency caused by large, unmanaged queues in network equipment |
| AQM | Active Queue Management (e.g. CoDel), which keeps queue delay low by dropping or marking early |
| Intent | A time-limited user request to prioritise a device's traffic (a "lease") |
| Selective accuracy | Accuracy computed over the flows the classifier did not abstain on |
| Coverage | Fraction of flows the classifier did not abstain on |
| Profile | A named set of per-class guaranteed rates and ceilings (NORMAL, INTENT) |
| SAFE_MODE | A static, known-good shaping configuration used when anything fails |
| Direct path | HTB's unshaped direct queue, selected when `skb->priority` equals the qdisc handle (`1:0`). Used only for estimator probes and traffic the gateway itself sends to the LAN |
| R_hw | High-water mark: the highest capacity estimate seen recently while the link was saturated. Fast recovery aims for it |
| Bloat episode | A period during which ΔD ≥ 15 ms on at least 2 of 3 reflectors without interruption |
| B0 / B1 / B2 | Experiment configurations: no QoS / static CAKE / `qosd` (section 6) |

### 1.4 References
- Problem statement: *Case Study 3, Adaptive QoS Engine for Mixed Home Broadband Traffic* (Teliport S4)
- RFC 8289 (CoDel), RFC 8290 (FQ-CoDel), RFC 7567 (AQM recommendations)
- RFC 2474 (DiffServ field), RFC 4594 (DiffServ service-class guidelines)
- RFC 3550 (RTP; interarrival jitter estimator)
- RFC 8489 (STUN), RFC 8656 (TURN)
- ITU-T G.107 (E-model), ITU-T G.113 (impairment factor values)
- Linux `tc` man pages: `tc-htb(8)`, `tc-fq_codel(8)`, `tc-cake(8)`, `tc-netem(8)`, `tc-tbf(8)`

---

## 2. Overall Description

### 2.1 Product perspective
`qosd` runs on the home gateway between the LAN and the WAN. In the prototype the gateway, clients, "ISP" and servers are Linux network namespaces on one Ubuntu VM. Heavy components (dashboard, model training, experiments, optional LLM assistant) run **off the gateway**.

### 2.2 Users and stakeholders

| Stakeholder | Need |
|---|---|
| Household user | Calls and games that don't lag; downloads that still finish; a simple way to say "this matters now" |
| Evaluator / judge | Reproducible evidence, honest baselines, clear prototype-vs-production boundary |
| Developer / operator | Inspectable decisions, logs, safe rollback, clear module contracts |
| ISP / CPE vendor (future) | Light footprint, standard interfaces, integration path to RDK-B / OpenWrt / prplOS |

### 2.3 Operating environment
- Ubuntu 24.04 LTS, GA kernel 6.8, x86_64 VM. Exact versions are captured in `versions.lock` during Phase 0.
- Optional demo hardware (e.g. Raspberry Pi 4). Any hardware result is labelled as such, and anything emulated is labelled as emulation.
- The host development machine is Windows. The testbed runs inside a Linux VM, not WSL2 (WSL2 is not a supported target for this project).

### 2.4 Constraints, from the problem statement
| ID | Constraint |
|---|---|
| C-1 | Do not decrypt application traffic |
| C-2 | Use Linux traffic control or another open, inspectable enforcement mechanism |
| C-3 | Support IPv4 and, where available, IPv6 |
| C-4 | Provide policy rollback and prevent starvation of low-priority traffic |
| C-5 | Classifiers expose confidence and allow correction of misclassification |
| C-6 | Document third-party licenses; no redistribution contrary to licenses |
| C-7 | Include generated data and impairment settings for reproducibility |
| C-8 | Label any emulated hardware capability as emulation and explain its model and limits |
| C-9 | No credentials, keys or tokens in source control; example secrets clearly fictitious |
| C-10 | Automated remediation is bounded, observable and reversible; failure returns to a known safe state |
| C-11 | Where AI is used, compare against a deterministic baseline on a measurable outcome |

### 2.5 Assumptions
- A-1: One WAN uplink and one LAN-facing interface on the gateway (the testbed uses a single LAN veth; a multi-port LAN bridge is a production concern).
- A-2: The WAN bottleneck is beyond the gateway (modem or ISP), which is the usual home situation.
- A-3: Traffic in the testbed is synthetic and generated by our own tools, so ground-truth labels are known. Real-world validation uses only header-only captures from consenting team members' own devices.

---

## 3. Functional Requirements

Priority uses MoSCoW: **M** = Must, **S** = Should, **C** = Could.

### 3.1 Telemetry (TEL)
| ID | Requirement | Pri |
|---|---|---|
| FR-TEL-1 | The system shall collect per-flow counters in both directions: packets, bytes, packet-size histogram (8 bins), inter-arrival-time sum and sum of squares, first-seen and last-seen timestamps | M |
| FR-TEL-2 | Collection shall use packet headers only. No payload byte shall be read, stored or exported | M |
| FR-TEL-3 | Telemetry shall cover IPv4 and IPv6 TCP and UDP flows. The parser shall honour the IPv4 header length (IHL), accept one 802.1Q tag, and walk at most 4 IPv6 extension headers. For non-first fragments (IPv4 fragment offset ≠ 0, or an IPv6 Fragment header with offset ≠ 0) it shall not read L4 ports, because those bytes are payload. Packets that cannot be parsed shall be counted in an "unparsed" counter and treated as DEFAULT | M |
| FR-TEL-4 | The flow table shall have a fixed maximum size with automatic eviction of the least-recently-used entries | M |
| FR-TEL-5 | The system shall sample per-class queue statistics (bytes, packets, drops, backlog) from `tc` at least once per second | M |
| FR-TEL-6 | The system shall measure round-trip time to at least three configurable reflectors at 10 Hz each | M |
| FR-TEL-7 | The system shall map device MAC addresses to their current IPv4 and IPv6 addresses using the kernel neighbour table, updated from netlink neighbour events | S |
| FR-TEL-8 | Flows idle for 30 s, or for 5 s after a TCP FIN or RST, shall be removed from the flow and class maps | S |
| FR-TEL-9 | Packet counts shall count GSO/GRO segments (`max(1, gso_segs)`), so counters stay correct with offloads enabled | M |

### 3.2 Classification (CLS)
| ID | Requirement | Pri |
|---|---|---|
| FR-CLS-1 | Flows shall be classified into exactly one of: **RT** (voice, video calls, gaming), **STREAM** (adaptive video), **DEFAULT** (web, apps, unknown), **BULK** (downloads, updates, backups) | M |
| FR-CLS-2 | Every decision shall carry a confidence in [0,1] and its source: `override`, `rule`, `ml`, `guardrail` or `default` | M |
| FR-CLS-3 | A provisional class shall be assigned by the first classification cycle after a flow has at least 20 packets | M |
| FR-CLS-4 | When the maximum class probability is below the abstain threshold (default 0.60), the flow shall be classified DEFAULT with source `default` | M |
| FR-CLS-5 | A classified flow shall change class only if the smoothed probability of the new class exceeds 0.85 for 3 consecutive cycles (hysteresis) | M |
| FR-CLS-6 | Guardrail G1: a flow in RT (classified or intent-pinned) that exceeds 8 Mbit/s for at least 5 s shall be moved to STREAM. STREAM, not BULK, because high-rate real-time traffic such as cloud gaming exists | M |
| FR-CLS-7 | Guardrail G2: a DEFAULT flow that has transferred at least 20 MB, with no idle gap of 2 s or more in the last 10 s, shall be reclassified BULK | M |
| FR-CLS-8 | Users shall be able to correct a flow's class through the API. The correction takes effect within one classification cycle and can optionally persist as a rule for future matching flows | M |
| FR-CLS-9 | A deterministic rule-based classifier shall exist and be selectable at runtime, as the baseline for AI comparison | M |
| FR-CLS-10 | The ML model file shall declare its feature-schema version. On mismatch or load failure the system shall fall back to the rule classifier and raise an alert | M |
| FR-CLS-11 | Corrected labels shall be logged in a format usable for retraining | S |
| FR-CLS-12 | When a flow reaches 20 packets, the datapath shall notify the agent (one event per flow), and the agent shall assign a provisional class immediately instead of waiting for the next 1 s cycle | S |

### 3.3 Capacity estimation (EST)
| ID | Requirement | Pri |
|---|---|---|
| FR-EST-1 | The system shall maintain a shaper rate R per direction (download, upload), bounded by configured minimum and maximum | M |
| FR-EST-2 | Every 50 ms the system shall compute T_obs over the last 250 ms, link load L = T_obs ÷ R, and delay rise ΔD = smoothed RTT − baseline RTT per reflector. T_obs is the rate **through the bottleneck side**: for download, the arrival rate at the LAN interface before our shaper (what the modem delivered); for upload, the departure rate of the WAN root class | M |
| FR-EST-3 | When ΔD ≥ 15 ms on at least 2 of 3 reflectors and T_obs ≥ 0.5 Mbit/s, the system shall reduce R **regardless of L**: R = max(R_min, min(f_k·R, 0.90·T_obs)), with f_k = 0.85, 0.70, 0.50 for the 1st, 2nd and 3rd-or-later cut in one bloat episode. The first cut of an episode is immediate; further cuts are at least 500 ms apart and only while ΔD is still higher than at the previous cut (the queue is still growing). If bloat is present but T_obs < 0.5 Mbit/s, the system shall hold R and raise an alert (the queue is not caused by our traffic) | M |
| FR-EST-4 | If L < 0.75 while the FR-EST-3 condition holds, the bottleneck is upstream and below R (a capacity drop). The target is then R_target = max(R_min, 0.90·T_obs), and this cut is allowed even within 500 ms of the previous one. For download, R_target is updated every tick while bloat persists (while the modem is backlogged, arrival rate equals capacity); for upload it is fixed at detection. After a capacity-drop cut, or any cut with ΔD ≥ 30 ms, the system shall drain the modem queue: R = max(R_min, 0.65·R_target) until ΔD < 7.5 ms on at least 2 reflectors or 1.5 s have passed, then R = R_target | M |
| FR-EST-5 | When L ≥ 0.75, ΔD < 5 ms on all reflectors for 1 s, and the probe backoff (FR-EST-10) has elapsed, the system shall increase R. While R < 0.97·R_hw it adds min(15% of the gap to R_hw, 25% of R) every 250 ms (fast recovery); otherwise it multiplies R by at most 1.05 per second, up to R_max. In all other cases R is held | M |
| FR-EST-6 | The system shall publish a capacity estimate C_est per direction: an EWMA of T_obs over saturated intervals (L ≥ 0.95 or bloat). R_hw is the maximum C_est over the last 300 s | S |
| FR-EST-7 | If ΔD stays ≥ 15 ms for more than 10 s while R = R_min, the system shall raise a "link degraded" alert and stop reducing | M |
| FR-EST-8 | A reflector's baseline RTT shall follow lower samples immediately, and may rise only while L < 0.3 in both directions, so sustained load is never learned as the new baseline | M |
| FR-EST-9 | Estimator probes shall use the HTB direct path on the WAN interface, so they measure only queues beyond the gateway | M |
| FR-EST-10 | Probe backoff: a cut that follows an increase and lands at ≥ 0.75 × the rate set by the previous cut is a failed probe; increases then pause for 2 s, doubling with each consecutive failed probe up to 8 s. A cut to below 0.75 × the previous cut's rate is a capacity event and resets the backoff | M |

### 3.4 Policy (POL)
| ID | Requirement | Pri |
|---|---|---|
| FR-POL-1 | The policy engine shall implement the states INIT, NORMAL, INTENT_ACTIVE and SAFE_MODE with the transitions defined in the design document | M |
| FR-POL-2 | Each class shall have a guaranteed rate (floor) and a ceiling, expressed as percentages of R, per profile (table below) | M |
| FR-POL-3 | The BULK floor shall never be configured below 10% of R in any profile (anti-starvation) | M |
| FR-POL-4 | The RT ceiling shall never exceed 60% of R in any profile | M |
| FR-POL-5 | Profile switches shall occur at most once per 5 s, except entry into SAFE_MODE, which is immediate | M |
| FR-POL-6 | Every policy decision shall be logged with a timestamp, reason code, inputs summary and resulting configuration | M |

**Profiles (percent of shaper rate R):**

| Class | NORMAL floor / ceiling | INTENT floor / ceiling | HTB prio |
|---|---|---|---|
| RT | 20 / 40 | 40 / 60 | 0 |
| STREAM | 30 / 100 | 20 / 100 | 1 |
| DEFAULT | 20 / 100 | 20 / 100 | 2 |
| BULK | 10 / 100 | 10 / 50 | 3 |
| **Sum of floors** | **80** | **90** | |

### 3.5 Enforcement (ENF)
| ID | Requirement | Pri |
|---|---|---|
| FR-ENF-1 | Enforcement shall use Linux `tc` (HTB with fq_codel or CAKE leaves) and eBPF programs attached at the tc layer | M |
| FR-ENF-2 | Download traffic shall be shaped on the LAN interface egress. Upload traffic shall be shaped on the WAN interface egress | M |
| FR-ENF-3 | IPv4 and IPv6 traffic shall be classified and shaped identically | M |
| FR-ENF-4 | Before every configuration change the enforcer shall snapshot the current configuration. If any step fails, it shall restore the snapshot | M |
| FR-ENF-5 | Shaper rate changes shall not flush queues (use class change, not qdisc replace) | M |
| FR-ENF-6 | Flows without a class entry shall be enqueued in DEFAULT | M |
| FR-ENF-7 | The datapath shall write `skb->priority` only for a valid class code (1–4) or the fast-lane rule. It shall never produce the HTB handle value (`1:0`) for client traffic, because that value selects HTB's unshaped direct queue | M |
| FR-ENF-8 | Link-layer overhead and minimum packet unit shall be configurable and applied to all shapers (`stab` on HTB, `overhead`/`mpu` on CAKE) | M |
| FR-ENF-9 | On upload, pure TCP ACKs (ACK flag only, no payload, determined from header length fields) of a BULK flow shall be enqueued in DEFAULT, and the upload BULK leaf shall use CAKE `ack-filter` | S |
| FR-ENF-10 | Fast lane: packets of flows without a class shall be enqueued in RT if they are DNS (port 53), ICMP/ICMPv6, or non-IP frames | S |
| FR-ENF-11 | HTB `quantum`, `burst` and `cburst` shall be set explicitly on every class create and change. When the shaper rate is low, the fq_codel target shall be raised to at least 1.5 × the MTU transmission time at R | M |

### 3.6 Intent (INT)
| ID | Requirement | Pri |
|---|---|---|
| FR-INT-1 | A REST API shall allow creating, listing and cancelling intents. An intent names a target device (IP or MAC), a scope (`realtime` or `all`), and a duration between 60 s and 14,400 s | M |
| FR-INT-2 | Intents shall expire automatically. When the last intent ends, the system shall return to NORMAL | M |
| FR-INT-3 | At most 3 intents shall be active at once | M |
| FR-INT-4 | Scope `realtime` pins the device's flows whose RT probability is at least 0.4 to RT. Scope `all` pins all the device's flows to RT. In both cases the RT ceiling and guardrail G1 apply, so a pinned high-rate flow (e.g. a sync job) cannot crowd out the call it is meant to protect | M |
| FR-INT-5 | A minimal web page served by the agent shall let a user create and cancel intents and correct flow classes | S |
| FR-INT-6 | The API shall require a bearer token, listen only on the LAN address, and rate-limit to 10 requests/s per client | M |

### 3.7 Verification (VER)
| ID | Requirement | Pri |
|---|---|---|
| FR-VER-1 | For every action (profile switch, override, config reload) the system shall compare KPIs over the 10 s before and after, and record a verdict IMPROVED, NEUTRAL or REGRESSED | M |
| FR-VER-2 | The system shall roll back an action when a safety violation occurs: a backlogged class receives zero throughput for ≥ 5 s, or RT-probe p95 RTT exceeds 2× its pre-action value and is more than 20 ms higher. The estimator's rate changes are not subject to rollback (FR-EST-* has its own guards). A latency regression shall not be attributed to a policy action while the estimator is cutting or draining | M |
| FR-VER-3 | Two rollbacks within 5 minutes, or any failed restore, shall trigger SAFE_MODE | M |
| FR-VER-4 | If more than 10 probe cycles (an increase followed by a cut) occur within a minute, increases shall be frozen for 60 s (oscillation guard; FR-EST-10 normally keeps the rate lower) | M |

### 3.8 Safety (SAF)
| ID | Requirement | Pri |
|---|---|---|
| FR-SAF-1 | On agent start, a known-safe configuration shall be applied before any adaptive behaviour begins | M |
| FR-SAF-2 | If the agent stops or crashes, a supervisor shall install SAFE_MODE: CAKE at 80% of the last known shaper rate, or the configured fallback rate if none is known | M |
| FR-SAF-3 | eBPF programs shall detach automatically when the agent process exits (no pinned links), so traffic falls back to the HTB default class | M |
| FR-SAF-4 | SAFE_MODE shall be retried out of automatically after 60 s of passing health checks, at most 3 times per hour | S |

### 3.9 Observability (OBS)
| ID | Requirement | Pri |
|---|---|---|
| FR-OBS-1 | The agent shall expose Prometheus-format metrics for: shaper rate, capacity estimate, probe RTT, jitter and loss, per-class throughput, backlog and drops, active flows per class, classifier confidence, policy state, rollbacks, and process CPU and memory | M |
| FR-OBS-2 | A Grafana dashboard shall show latency, jitter, loss, throughput, queue depth and fairness, as required by the problem statement | M |
| FR-OBS-3 | The decision log shall be queryable through the API | M |

### 3.10 Experiments (EXP)
| ID | Requirement | Pri |
|---|---|---|
| FR-EXP-1 | A single command shall reset the testbed, apply a configuration (B0, B1, B2-rules, B2-ML), run a scenario, collect evidence and generate a report | M |
| FR-EXP-2 | Scenarios S1 to S9 (section 6) shall be scripted, with impairment settings stored in version control | M |
| FR-EXP-3 | Each scenario-configuration pair shall run at least 5 times. Reports shall show mean and 95% confidence interval | M |
| FR-EXP-4 | Generated datasets and the scripts that produced them shall be included or reproducible from a fixed seed | M |

### 3.11 Optional LLM assistant (LLM)
| ID | Requirement | Pri |
|---|---|---|
| FR-LLM-1 | An off-gateway assistant could translate natural-language requests into intent JSON, validated against the API schema before submission | C |
| FR-LLM-2 | The assistant could explain decisions using read-only access to state and the decision log | C |
| FR-LLM-3 | The assistant shall have no direct access to `tc`, eBPF or configuration. All changes pass through the policy engine's bounds | M (if built) |
| FR-LLM-4 | The system shall be fully functional with the assistant disabled | M |

---

## 4. Non-Functional Requirements

Values marked *target* are goals to be measured and reported honestly, whether met or not.

### 4.1 Accuracy
| ID | Requirement |
|---|---|
| NFR-ACC-1 | *Target:* selective accuracy ≥ 98% at coverage ≥ 90% on **held-out testbed scenarios** (split by scenario/run, never by window) |
| NFR-ACC-2 | *Target:* macro-F1 ≥ 0.95 on the same held-out set |
| NFR-ACC-3 | Accuracy on consented real-device captures shall be reported separately, without a pass/fail target. The expectation is lower (about 90%) and the report must say so |
| NFR-ACC-4 | *Target:* capacity estimate MAPE ≤ 10% during saturated periods, against the emulator's true rate |

### 4.2 Performance and footprint
| ID | Requirement |
|---|---|
| NFR-PERF-1 | *Target:* agent CPU ≤ 2% of one core at 100 Mbit/s on the test VM. Measure and report it; repeat on a Raspberry Pi 4 if available |
| NFR-PERF-2 | *Target:* agent resident memory ≤ 30 MB, eBPF flow map ≤ 5 MB |
| NFR-PERF-3 | *Target:* maximum forwarding throughput reduced by ≤ 5% with eBPF programs attached, compared with without |
| NFR-PERF-4 | No per-packet processing in user space |
| NFR-PERF-5 | Classification inference ≤ 1 ms per flow per cycle |
| NFR-PERF-6 | eBPF cost per packet shall be measured with kernel BPF statistics (`kernel.bpf_stats_enabled=1`; `run_time_ns ÷ run_cnt`) and reported per program. *Target:* ≤ 500 ns per packet on the test VM |

### 4.3 User experience (measured in scenarios)
| ID | Requirement |
|---|---|
| NFR-UX-1 | *Target:* in S1, RT p99 added latency under load ≤ 30 ms, and not worse than B1 by more than 5 ms |
| NFR-UX-2 | *Target:* in S2 and S11, the shaper reacts within 500 ms of the capacity drop, and RT p95 latency returns within baseline + 15 ms within 3 s (time-based modem buffer, design §16.1) |
| NFR-UX-3 | *Target:* RT packet loss ≤ 1% and RT p95 jitter ≤ 10 ms in S1 to S4 |
| NFR-UX-4 | *Target:* Jain's fairness index ≥ 0.9 across STREAM devices in S3 |
| NFR-UX-5 | No backlogged class receives zero throughput for any 5 s window in any scenario (starvation check) |
| NFR-UX-6 | *Target:* in S5, after capacity is restored, R reaches ≥ 85% of the new capacity within 10 s (v1.0's ×1.05 per second needs about 19 s, from 36 to 90 Mbit/s) |
| NFR-UX-7 | *Target:* in S10, B2-ML gives a lower call loss and p95 latency than B2-rules, with non-overlapping 95% confidence intervals. This is the outcome-level AI-vs-baseline comparison |

### 4.4 Stability
| ID | Requirement |
|---|---|
| NFR-STAB-1 | Profile switches occur only on intent or safety events |
| NFR-STAB-2 | *Target:* at most 8 probe cycles (an increase followed by a cut) per minute in steady state, each adding at most 25 ms of queueing delay. Any probing estimator saw-tooths; v1.0's "≤ 6 reversals per minute" equals 3 probe cycles and could not be met together with timely recovery (design §8.2) |
| NFR-STAB-3 | *Target:* no flow changes class more than 2 times per minute |

### 4.5 Reliability and safety
| ID | Requirement |
|---|---|
| NFR-REL-1 | *Target:* SAFE_MODE installed within 10 s of agent failure |
| NFR-REL-2 | *Target:* configuration restore after a failed apply completes within 1 s |
| NFR-REL-3 | Every automated action is bounded by the limits in section 3, logged, and reversible |

### 4.6 Security and privacy
| ID | Requirement |
|---|---|
| NFR-SEC-1 | No secrets in the repository. The repo ships `config.example.yaml` with clearly fictitious values; a pre-commit secret scan is enabled |
| NFR-SEC-2 | The agent runs with only the capabilities it needs (CAP_NET_ADMIN, CAP_BPF, CAP_NET_RAW, and CAP_PERFMON if the kernel requires it) |
| NFR-SEC-3 | The API listens on the LAN address only, requires a token, and validates all input against a schema |
| NFR-PRIV-1 | Packet captures, when used, are truncated to 96 bytes (headers only). Published datasets contain features only, with IP addresses removed or anonymised |

### 4.7 Reproducibility and maintainability
| ID | Requirement |
|---|---|
| NFR-REPRO-1 | A clean VM reaches a working testbed by following the setup guide. Exact versions are pinned in `versions.lock` |
| NFR-REPRO-2 | All random generators take a seed; seeds are recorded in each run's metadata |
| NFR-MAINT-1 | Each module has unit tests that run without root, using recorded traces or fakes |
| NFR-LIC-1 | `THIRD_PARTY.md` lists every dependency, its license (confirmed from its LICENSE file) and how it is used |

---

## 5. External Interfaces

| Interface | Direction | Format |
|---|---|---|
| REST API `/api/v1/*` | User / assistant → agent | JSON over HTTP, bearer token |
| Prometheus `/metrics` | Agent → Prometheus | Prometheus text exposition format |
| Decision log | Agent → file / API | JSON Lines |
| Feature recording (training mode) | Agent → file | JSON Lines, one row per flow per cycle |
| Model file | Training pipeline → agent | LightGBM `dump_model()` JSON plus metadata (schema version, SHA-256) |
| Kernel | Agent ↔ kernel | netlink (tc), BPF syscalls (maps, links) |

---

## 6. Verification: scenarios and configurations

**Configurations**
- **B0:** no QoS. Default queue at the gateway; deep buffer at the emulated modem.
- **B1:** static CAKE at a fixed 90% of nominal rate, configured the way an expert would deploy it: per-host isolation, `ingress` mode on the download side, `nat` and `ack-filter` on upload. A weak baseline proves nothing.
- **B2-rules:** `qosd` with the rule classifier.
- **B2-ML:** `qosd` with the ML classifier.

**Scenarios**
| ID | Scenario | Main requirements verified |
|---|---|---|
| S1 | Large multi-connection download starts during a video call | NFR-UX-1, NFR-UX-3, FR-CLS-* |
| S2 | Download capacity drops from 100 to 20 Mbit/s during mixed load | FR-EST-*, NFR-UX-2, NFR-ACC-4 |
| S3 | Three video streams and one game share the link | NFR-UX-4, NFR-UX-5 |
| S4 | Cloud-backup upload during a video call | Upload path, FR-ENF-2 |
| S5 | Capacity alternates 100 ↔ 40 Mbit/s every 20 s | NFR-STAB-*, FR-VER-4, NFR-UX-6 |
| S6 | Deliberate misclassification, then correction via the API | FR-CLS-8 |
| S7 | Intent created, used and expired | FR-INT-* |
| S8 | Agent killed mid-run | FR-SAF-*, NFR-REL-1 |
| S9 | S1 repeated with IPv6-only clients | FR-TEL-3, FR-ENF-3 |
| S10 | Classification-sensitive: a video call on UDP 443, 3 TVs streaming, a bulk download, and N greedy web/sync flows. N is chosen so that DEFAULT's share per flow is below half the call's bitrate | NFR-UX-7, FR-CLS-*, C-11 |
| S11 | Upload capacity drops from 20 to 5 Mbit/s during a video call and a cloud backup | FR-EST-3, NFR-UX-2 |

---

## 7. Traceability: problem statement → requirements

| Problem-statement item | Requirements |
|---|---|
| Classify broad traffic categories without reading private payloads | FR-TEL-2, FR-CLS-1..10, NFR-PRIV-1 |
| Estimate link capacity | FR-EST-1..10, NFR-ACC-4, NFR-UX-6 |
| Apply queueing and shaping policies | FR-POL-*, FR-ENF-* |
| Verify whether the policy improved user experience | FR-VER-1..4, FR-EXP-* |
| Temporary user intent while retaining fairness | FR-INT-*, FR-POL-3, FR-POL-4 |
| Independently testable modules with inputs, outputs, states, failures, success conditions | Design doc §5; NFR-MAINT-1 |
| AI compared with deterministic baseline | FR-CLS-9, B2-rules vs B2-ML (accuracy and the S10 outcome, NFR-UX-7), FR-LLM-4 |
| Do not decrypt | C-1, FR-TEL-2 |
| Linux tc / inspectable enforcement | FR-ENF-1 |
| IPv4 and IPv6 | FR-TEL-3, FR-ENF-3, S9 |
| Rollback, no starvation | FR-ENF-4, FR-VER-2, FR-POL-3, NFR-UX-5 |
| Confidence and correction | FR-CLS-2, FR-CLS-8 |
| Licenses | NFR-LIC-1 |
| Generated data and impairment settings included | FR-EXP-2, FR-EXP-4 |
| Emulation labelled | C-8, design doc §16 |
| No secrets committed | NFR-SEC-1 |
| Bounded, observable, reversible remediation; safe state | FR-SAF-*, FR-VER-*, NFR-REL-* |
| Dashboard (latency, jitter, loss, throughput, queue depth, fairness) | FR-OBS-2 |
| Automated baseline-vs-optimised experiments | FR-EXP-1..3 |
| API / UI for temporary intent | FR-INT-1, FR-INT-5 |
| Architecture diagram; setup guide; scripted demo; known limitations | Design doc §3, §16, §21; build plan Phase 9 |
| Evaluation: accuracy, latency/jitter/loss, fairness, adaptation and stability, CPU, reproducibility | NFR-ACC, NFR-UX, NFR-STAB, NFR-PERF, NFR-REPRO |
