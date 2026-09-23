# ADR 0003: Qwen3-Coder-30B-A3B-Instruct-FP8, pinned

Status: accepted (2026-09-23)

## Decision
- **Model:** `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8` at HF revision `dcaee4d4dfc5ee71ad501f01f530e5652438fde0`. It is not gated, 31.18 GB in 4 shards, and uses block-128 e4m3 FP8 with dynamic activations.
- **Serving image:** `vllm/vllm-openai:v0.30.0` (CUDA 13), pinned by digest. Pods request `gpu.minCudaVersion: 13.0`, i.e. host driver 580 or newer.
- **Why not the `-cu129` variant (revised 2026-09-23):** it was the original choice, to tolerate hosts with drivers older than 580. On the first real A6000 pod it crashed at import: the image ships `torch 2.14.0+cu130` with a cu129 torchvision (`operator torchvision::nms does not exist`, upstream vllm-project/vllm#56829). The main CUDA 13 build is used instead; Runpod's catalog lists hosts on CUDA 13.0 and 13.2, and the A6000 host we got ran driver 580.159.03.
- **Why v0.30.0:** it was one day old when chosen. v0.29.0 is the fallback if a regression appears; change `image` and `vllm_version` together.

vLLM flags:

- `--max-model-len 65536`, `--gpu-memory-utilization 0.90`, `--tensor-parallel-size 1`
- `--enable-prefix-caching` (default-on in V1; explicit for the record)
- `--enable-auto-tool-choice --tool-call-parser qwen3_coder`
- `--enable-prompt-tokens-details`, which vLLM needs before it reports `cached_tokens`
- `--revision`/`--tokenizer-revision` pinned

## Generation
Qwen's recommended sampling (temperature 0.7, top_p 0.8, top_k 20,
repetition_penalty 1.05) with a fixed seed. Greedy decoding was rejected: it
is prone to repetition loops in long agentic sessions. It remains a config
change if we want to test it.

## Consequences
Reproducible: model revision, image digest, argv and generation parameters
are all copied into every `run.json`. The model is a 3B-active MoE, so decode
throughput should be high relative to its 30B parameter count. That is part
of the hypothesis being tested.
