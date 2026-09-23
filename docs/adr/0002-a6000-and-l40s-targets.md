# ADR 0002: RTX A6000 and L40S as initial targets

Status: accepted (2026-09-23)

## Context
Qwen3-Coder-30B-A3B FP8 needs about 31 GB of weights plus KV cache. A 64K context
with bf16 KV is about 6 GiB (48 layers × 4 KV heads × 128 dims × 2 × 2 bytes/token).
Both 48 GB cards fit this on a single GPU.

## Decision
Benchmark the cheapest adequate Ampere card (A6000, $0.53/hr secure) against
the Ada card with native FP8 (L40S, $1.09/hr secure). GPU type IDs were
verified against the live Runpod catalog: `NVIDIA RTX A6000` and `NVIDIA L40S`.

## Consequences
- **Kernels differ.** From the vLLM v0.30.0 source:
  - A6000 runs FP8 weights through Marlin W8A16 for both dense and MoE layers.
  - L40S uses Marlin for dense layers and native block-FP8 Triton for the MoE experts.
  - Neither card has a tuned MoE config for E=128/N=768, so expect a vLLM "default MoE config" warning on both.
- The L40S reports about 46 GB usable, so 64K context at 0.90 utilization is tight but fits.
- `runtime_overrides` exists per profile if one card needs a different setting. Any override is recorded prominently, and `compare` flags the runs as not directly comparable.
