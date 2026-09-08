# Mac 128K starting point for the Qwen3.6 capacity story

Measured on 8 September 2026. This supplement documents the Mac configuration
at the beginning of the [capacity-expansion story](https://zonghang-li.github.io/cpil-lab.github.io/blog/more-than-speed).
It is **not** added to the playground, which still compares only the
[262,144-context deployments](qwen36-testbed3.md).

## Configuration

- MacBook Pro, Apple M3 Pro, 18 GB unified memory, Metal, six inference/batch threads.
- Official llama.cpp commit `9558fa44c92746a58dd07ad1bf0c889715b938a6`.
- Qwen3.6-35B-A3B IQ1_M, target GGUF 9,421,649,536 bytes.
- Context capacity 131,072; concurrency 1; batch 8192; microbatch 512.
- Flash Attention on; F16 K and V; DFlash off; no mmap; no fit adjustment.
- All loaded target inference weights on Metal: `-ngl all -ncmoe 0`, with
  `--override-tensor token_embd.weight=MTL0` explicitly placing the input embedding.
- Native logs report 8,118.35 MiB of target Metal weight buffers and no target
  CPU weight buffer. The buffers are not the size of the entire GGUF file.
- OS-reported lifetime peak physical footprint 10.781 GiB. This belongs to
  the 18 GB UMA; it must not be added to a separate copy of “GPU memory.”

This is a verified fully accelerator-resident starting configuration, not an
exhaustive search proving that 131,072 is the Mac's absolute context ceiling.
Standalone 262K also ran, with different weight placement.

## Requests and measurements

The three prompts are the same math, code and literature prompts used by the
262K playground case. Their rendered input lengths are **53, 48 and 49 tokens**,
not 128K tokens. One excluded 16-token warmup preceded the three formal requests.

Each formal request used seed 1234, repeat penalty 1, no prompt-cache reuse,
and natural EOS. The request did not send temperature, top-k, top-p, min-p or
`n_predict`. Receipts confirm defaults T=1, top-k=20, top-p≈0.95, min-p≈0.05,
and `n_predict=-1`. No answer was truncated or selected as the fastest of repeats.

Timings were captured on the Mac's loopback HTTP connection. TTFT runs from
request submission to the first output-token event, excluding loading and
warmup. Mean TPOT is `(last arrival - first arrival) / (output tokens - 1)`;
the generation rate is its reciprocal. Atomic token-arrival groups remain
atomic in the archived capture.

| Prompt | Input tokens | Output tokens | TTFT (s) | Mean TPOT (ms) | Generation (tok/s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Math | 53 | 377 | 0.288 | 23.795 | 42.03 |
| Code | 48 | 512 | 0.279 | 23.836 | 41.95 |
| Literature | 49 | 160 | 0.290 | 23.845 | 41.94 |

The code answer's 512 output tokens are coincidental: the server reported EOS,
not a 512-token cap. Fixed seeds do not imply identical text across backends,
placements or speculative algorithms.

## What the story can and cannot conclude

The story compares a Mac-only 128K deployment with larger-capacity 262K
deployments. The capacity target doubles; it does **not** establish an isolated
causal effect of increasing context alone. In particular, expert placement
changes, and the 262K standalone baseline retains default input-embedding
placement rather than the explicit GPU override used here.

The Mac's 41.95 tok/s code request with DFlash off and PRIMA's selected 43.31
tok/s code request with DFlash on illustrate a similar generation rate at
twice the configured capacity. This is an end-to-end configuration comparison,
not an algorithm-only A/B test. Other requests do not promise unchanged speed:
PRIMA's 262K DFlash-off requests range from 33.75 to 38.38 tok/s, and its
DFlash-on literature request is slower. No long-document retrieval or
near-full-used-KV throughput has been established by these short inputs.

Natural EOS does not certify answer quality. The Mac 128K math answer is also
incorrect: it gives residue classes 5, 9 and 10, whereas the correct classes
are 2, 7, 10 and 11 modulo 12. This supplement is a timing record, not an
accuracy result, and does not isolate the cause of that error.

## Provenance

The [machine-readable summary](qwen36-mac-128k-story-baseline.json) is a
Mac-only, allowlisted projection of the validated native capture. It retains
full-precision metrics, request start times, output counts and output/token-ID
hashes. Raw SSE, full answers, native launch receipts and memory samples are
retained in the local experiment archive, not served from this website.

- Target GGUF SHA-256:
  `d291e12a0f693b5f14c11bb1278ba43b432b2b6f6c33c7fdaf750c214ff83f28`.
- Native official executable SHA-256:
  `924efd9a6546036ea927ca6e68cc3c11cbdc81ce9ff8cda241218bccd2df9eee`.
- Existing 262K replay dataset SHA-256, unchanged by this article:
  `e67d0b07973a94bedf4562bee6998fae9e88603911e6f1ff20ab9a682911d732`.
