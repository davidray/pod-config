# ADR 0001: Runpod Pods before Serverless

Status: accepted (2026-09-23)

## Context
The experiment compares GPUs on multi-hour agentic coding sessions that send
many long, cache-heavy requests. We need warm, stable sessions, predictable
latency, explicit start/stop and a clean per-GPU comparison.

## Decision
Use Runpod **Pods** via REST v2 for the initial benchmark. Isolate them behind
`ComputeProvider` (`providers/base.py`) so a Serverless backend can be added
without touching benchmarks, dispatch or Hero routing, all of which consume only
an `Endpoint`.

## Consequences
- **Benefits:** no cold starts mid-task; prefix cache stays warm for the whole session; one pod = one GPU = clean attribution of time and cost.
- **Cost:** we pay for idle time. That is mitigated by the in-pod idle watchdog plus the local guard (ADR 0011).
- **Serverless** remains attractive for bursty real-world use after the quality question is answered. Its cold-start and queueing behavior would need its own benchmark profile.
