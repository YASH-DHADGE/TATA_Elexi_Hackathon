# Adaptive QoS Engine: documentation

Tata Elxsi Teliport Season 4, Case Study 3: *Adaptive QoS Engine for Mixed Home Broadband Traffic*.

| File | What it answers |
|---|---|
| [06_How_It_Works.md](06_How_It_Works.md) | **Start here if you are new**: plain-language guide to how the system works, what runs where, how classification works, why LightGBM, which LLM and whether our laptop can run it |
| [01_SRS.md](01_SRS.md) | **What** the system must do and how well: requirements with IDs, targets, scenarios, traceability to the problem statement |
| [02_System_Design.md](02_System_Design.md) | **How** it works: architecture, packet path, module contracts, data structures, tc commands, algorithms, state machines, API, configuration, testbed, limitations, Phase 0 spikes |
| [03_Build_Plan.md](03_Build_Plan.md) | **How we build it**: 10 phases with tasks, owners, exit criteria, timeline, MVP cut line, risks |
| [04_Design_Review.md](04_Design_Review.md) | **What changed in v1.1 and why**: Linux-networking review of v1.0 (4 critical defects, realism gaps, speed and simplicity changes), with evidence |
| [05_Improvements_and_Tech_Stack.md](05_Improvements_and_Tech_Stack.md) | **Where to improve next and which technology to use**: latency budget, where qosd can and cannot beat static CAKE, remaining weaknesses, layer-by-layer stack choices, prioritised recommendations |
| [07_Plumbing_and_Leak_Audit.md](07_Plumbing_and_Leak_Audit.md) | **Where the design leaked and how v1.2 seals it**: every channel traced from producer to consumer; 29 leaks (control, state, data-plane, measurement, security, resource) with fixes |
| [sim/estimator_fluid_model.py](sim/estimator_fluid_model.py) | Runnable fluid model of the v1.0 vs v1.1 capacity estimator (`python sim/estimator_fluid_model.py`); seed of the P3.0 harness |

## The system in one paragraph
A small Go agent (`qosd`) on a Linux home gateway uses eBPF to collect header-only statistics for every flow, without per-packet work in user space. A LightGBM classifier sorts flows into four treatment classes (RT, STREAM, DEFAULT, BULK) with a confidence, and is compared against a deterministic rule classifier. A delay-based estimator keeps the shaper rate just below the real link capacity, targeting a reaction to capacity drops within 0.5 s (see `04_Design_Review.md` for the model evidence). A deterministic policy engine turns all of this, plus time-limited user intents, into HTB + fq_codel/CAKE configuration. Every class has a guaranteed floor and a ceiling, so nothing is starved and nothing takes over. A verifier checks that each action helped and rolls back the ones that cause harm, and a systemd supervisor installs a static safe configuration if the agent fails. A namespace-based testbed and an experiment runner produce reproducible baseline-vs-optimised evidence.

## Consistency rule
All numeric defaults (thresholds, rates, timings) are defined once in design §12 and match the SRS. If you change one, change it there and in the SRS in the same pull request.
