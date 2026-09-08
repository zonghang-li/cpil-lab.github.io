# Qwen3.6 IQ1_M — Testbed 3 recorded requests

Measured on 8 September 2026. These are **unedited model outputs and request
timings**, not an accuracy benchmark or a live inference service. All 18 final
requests finished naturally at EOS. Earlier capped or greedy attempts are
diagnostics and are not used in the playground.

## What is being compared

| Deployment | Hardware | Target placement at 262,144 context capacity |
| --- | --- | --- |
| Mac llama.cpp | MacBook Pro, Apple M3 Pro, 18 GB unified memory | Transformer non-expert/shared-expert weights on Metal; first 24 routed-expert blocks on CPU, remaining 16 on Metal |
| Windows llama.cpp | Core i9-9900K, 64 GB RAM, RTX 2080 Ti GPU 0, 11 GB VRAM | Transformer non-expert/shared-expert weights on CUDA; first 34 routed-expert blocks in system RAM/CPU, remaining 6 on CUDA |
| PRIMA | The same Mac and Windows desktop together | Mac head: 14 target layers on Metal; Windows worker: 26 on CUDA; assigned target-layer weights resident on their accelerators |

The Mac uses Wi-Fi and Windows uses Ethernet to the same LAN router. Windows'
physical adapter reported a **100 Mbps** link, not the speed of its virtual
Hyper-V adapter. No remote server, WAN or VPN hop carries inference traffic.
The device pictures are illustrations, not photographs of the machines.

All runs use the same Qwen3.6-35B-A3B **IQ1_M** target GGUF, concurrency 1,
F16 KV cache, Flash Attention on, batch 8192 and microbatch 512. **262,144 is
allocated context capacity, not input length.** Inputs are the three original,
short Qwen3.8-27B playground prompts, with no 8K padding. The target has 40
ordinary layers; an unused MTP block is not counted in the 14:26 partition.

Both standalone baselines use the clean official llama.cpp commit
[`9558fa44c92746a58dd07ad1bf0c889715b938a6`](https://github.com/ggml-org/llama.cpp/tree/9558fa44c92746a58dd07ad1bf0c889715b938a6).
CPU expert placement is the official `-ncmoe` mechanism. These experts compute
on the CPU; this experiment does **not** claim a custom disk-streaming engine.
`--no-mmap` separates the CPU/GPU weight buffers. On Mac, mmap had retained an
oversized Metal mapping even when expert computation was assigned to CPU.
The selected expert placement is identical with DFlash off and on on each
standalone host. This favors a controlled comparison, not independent maximum
GPU packing for every mode.

The standalone commands keep official default input-embedding placement;
they do not apply a GPU tensor override to `token_embd.weight`. The table's
placement description concerns the Transformer blocks, not a claim that all
non-expert tensors or the entire GGUF are accelerator-resident.

DFlash uses the matching **Q4_K_M** draft on the local accelerator (on the Mac
head for PRIMA), maximum draft block 8 and minimum draft probability 0.5.
PRIMA uses FP32 target activations, FP16 reduced DFlash features, prefill
pipeline depth 4 and CUDA Graphs enabled. The selected-terminal-row return
optimization is enabled for non-speculative requests and disabled for DFlash;
DFlash request pinning and the measured first/tail target-chunk policy 3/8 are
enabled. The target is still distributed; it is not duplicated onto the head.

### Sampling and termination

Measured requests do **not** specify `temperature`, `top_k`, `top_p`, `min_p`
or `n_predict`. They use normal server defaults with **seed 1234**, repeat
penalty 1, `ignore_eos: false` and `cache_prompt: false`. All 18 server receipts
reported effective temperature **1.0**, top-k **20**, top-p approximately
**0.95**, min-p approximately **0.05**, and `n_predict: -1`.

Thus temperature was not forced by the request and there was **no output token
cap**. A fixed seed does not promise identical text across different floating-
point backends or speculative algorithms. Outputs have different lengths;
none has been edited, padded or shortened. Repeated PRIMA-on captures use the
middle request by mean TPOT, not the fastest request or an edited answer.

The chat template uses `enable_thinking: false`; the rendered prompt token
arrays were checked for exact equality across all deployments. Each server
received one explicitly excluded 16-token math warmup. Standalone retests and
the original PRIMA-off series then ran math, code, literature sequentially.
PRIMA-on uses three math/code/literature cycles for math and literature, and
three consecutive code requests for the separately accepted code group.
There is no prompt-cache reuse. A capture timeout is an incomplete run, not EOS.

### Retests and representative records

All twelve standalone records below are the later native retests. PRIMA-off
retains its original three completed records; PRIMA-on uses the later
no-power-probe repeat groups. For each PRIMA-on prompt the page replays the
**actual request at the median mean TPOT** among its three declared candidates.
Its TTFT, output, and every arrival timestamp all come from that one request.
No average or interpolated token timeline is generated.

The exact archive selection is recorded in
[`qwen36-capture-selection-20260908.json`](qwen36-capture-selection-20260908.json).
The exporter validates every candidate, including unselected ones, and stores
candidate TTFT/TPOT, token counts, output hashes, selection index and sample
count in the public dataset. The PRIMA-on candidate throughputs (tok/s) are:

| Prompt | Candidate 1 | Candidate 2 | Candidate 3 | Selected |
| --- | ---: | ---: | ---: | ---: |
| Math | 39.00 | 37.95 | 35.58 | 2 |
| Code | 43.31 | 42.19 | 43.42 | 1 |
| Literature | 23.36 | 22.02 | 23.58 | 1 |

An additional already-started mixed-prompt control produced code rates of
43.95, 36.96 and 40.55 tok/s. It is retained in the experiment archive, not
pooled into the separate code group. Earlier slow PRIMA-on code runs reached
about 20 tok/s and coincided with Mac GPU frequency falling to about 338 MHz.
Later unchanged-runtime runs recovered without a power probe. The reason for
that variation remains unproven; neither a permanent power fix nor network
competition is asserted as its cause. Standalone retest outputs and each
same-configuration PRIMA repeat match their earlier text and token IDs.

## Request-level results

TTFT is measured from request submission to the first generated-token event,
using the native client's monotonic clock over loopback HTTP. It includes
prompt processing and request overhead, but excludes model loading and the
declared warmup. Windows requests are timed on Windows, not through SSH.

For a request with `N` observed output tokens:

```text
mean TPOT = (last token arrival - first token arrival) / (N - 1)
displayed throughput = 1 / mean TPOT
```

This is neither compute-only TG nor an average pooled across prompts. The
table rounds values for reading; the dataset retains measured precision.

| Prompt | DFlash | Deployment | Output tokens | TTFT (s) | Mean TPOT (ms) | Throughput (tok/s) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Math | Off | Mac llama.cpp | 335 | 0.665 | 43.938 | 22.76 |
| Math | Off | Windows llama.cpp | 540 | 1.118 | 42.587 | 23.48 |
| Math | Off | PRIMA | 185 | 0.408 | 26.057 | 38.38 |
| Math | On | Mac llama.cpp | 397 | 2.392 | 66.884 | 14.95 |
| Math | On | Windows llama.cpp | 669 | 1.145 | 39.331 | 25.43 |
| Math | On | PRIMA | 682 | 0.412 | 26.351 | 37.95 |
| Code | Off | Mac llama.cpp | 334 | 1.601 | 45.183 | 22.13 |
| Code | Off | Windows llama.cpp | 384 | 1.097 | 41.593 | 24.04 |
| Code | Off | PRIMA | 473 | 0.880 | 29.625 | 33.75 |
| Code | On | Mac llama.cpp | 366 | 5.739 | 93.862 | 10.65 |
| Code | On | Windows llama.cpp | 388 | 1.112 | 35.500 | 28.17 |
| Code | On | PRIMA | 322 | 0.358 | 23.091 | 43.31 |
| Literature | Off | Mac llama.cpp | 165 | 1.079 | 43.954 | 22.75 |
| Literature | Off | Windows llama.cpp | 159 | 1.191 | 37.960 | 26.34 |
| Literature | Off | PRIMA | 171 | 1.347 | 27.363 | 36.55 |
| Literature | On | Mac llama.cpp | 159 | 5.670 | 97.367 | 10.27 |
| Literature | On | Windows llama.cpp | 177 | 1.192 | 46.920 | 21.31 |
| Literature | On | PRIMA | 152 | 0.466 | 42.805 | 23.36 |

DFlash is **not uniformly faster** on this workload. The selected PRIMA-on
code request is faster than PRIMA-off; math is similar and literature is
slower. The standalone Mac DFlash run remains slower at this full-context
memory footprint, despite recovering from its earlier 3.56–5.93 tok/s samples.
Its OS-reported lifetime physical-footprint peak reached **15.74 GiB**; this is
part of the 18 GB UMA, not extra RAM. The Windows DFlash run's sampled GPU0
peak was **10,583 MiB**, with a minimum **445 MiB** free. GPU usage is device-
wide, sampled every two seconds, not an exact process allocator peak.

Each cell displays one complete request; three PRIMA-on cells select it from
the declared repeat groups. Other cells have one candidate. These measurements
were collected at different times and are not a randomized, simultaneous
comparison or a study with confidence intervals. Prompt length, sampled answer
length, acceptance behavior, memory pressure and network variability can
affect results. No causal speed claim is made from these samples alone, and
earlier long-prompt/greedy benchmarks are not substituted for this dataset.

## Known answer-quality limitations

Natural EOS fixes the collection/termination problem; it does not certify an
answer as correct. **All six mathematics answers are incorrect.** The correct
residue classes are **2, 7, 10, 11 modulo 12**, obtained by checking
`(n + 1)(n + 2)` for `n = 0,...,11`. Several answers also exceed the requested
120-word limit. The website intentionally shows them unchanged.

Static review also found the following in the code answers:

| Sample | Finding |
| --- | --- |
| Mac, DFlash off | Merging uses `max(last[0], current[0])` as the start. For `(1,4),(2,6)` this returns `(2,6)`, contradicting its own expected `(1,6)`. |
| Mac, DFlash on | The nested-interval example passes tuples, then assigns to `last[1]`; it raises `TypeError`. |
| Windows, DFlash off | Code sorts ascending; the explanation incorrectly says descending. |
| Windows, DFlash on | The merge operation is sensible, but tuple inputs can yield a mixture of lists and tuples; the supplied tests do not exercise overlap. |
| PRIMA, DFlash off | The documented input and examples use tuples, then mutate `last[1]` on overlap; the second example raises `TypeError`. |
| PRIMA, DFlash on | Static review found no corresponding merge defect; this is not an exhaustive correctness certification. |

Literature samples are readable, on-topic interpretations, not independently
graded literary analyses. This dataset does not isolate whether any quality
defect comes from quantization, model behavior, sampling or a runtime. Do not
attribute one of those causes without a controlled accuracy experiment.

## Replay data and reproducibility

The public source of truth is
[`src/qwen36-replay-data-20260908.js`](../../src/qwen36-replay-data-20260908.js).
It contains all 18 full outputs, token IDs, token/arrival-group timestamps,
request fingerprints, frozen prompt-token requests, effective sampling,
compute-only timings (separate from displayed metrics), and allowlisted
deployment provenance. No private host addresses or local paths are exported.

Each event is `[seconds_since_request, observed_interval, text_piece, token_id]`.
If SSE delivers multiple tokens atomically, they share its observed arrival
time: the replay preserves the burst, without inventing internal intervals.
When an emitted EOS token has an empty text piece it remains in the recorded
token count; `[DONE]` and a metadata-only final frame do not add a token. All
18 final traces have complete token IDs. Clicking Run retains each panel's
TTFT, then follows its own recorded timeline. Reduced-motion preference
suppresses decoration but does not remove measured latency. Pausing or hiding
the tab pauses the replay clock, not an actual inference process.

The capture and validation tools are in [`tools/`](../../tools/):

- `run_native_replay.py`: native baseline launch, capacity evidence, frozen
  prompts, excluded warmup, capture and owned-PID cleanup. Mac settings:
  `--device MTL0 --cpu-moe 24 --threads 6 --no-mmap`; Windows settings:
  `--device CUDA0 --cpu-moe 34 --threads 8 --no-mmap`. Supply the native
  executable, model path, source commit, prompts and fresh run directory;
  supply `--draft-model` only for DFlash on. Inspect `--help` before running.
- `capture_replay.py`: loopback SSE capture and strict EOS/token/timing checks;
  also used on the Mac head of the separately launched PRIMA ring.
- `export_qwen36_replays.py`: accepts the explicit complete `--selection`
  manifest above, or the original six `{mac,windows,prima}-sampled-eos-dflash-
  {off,on}` directories when no manifest is supplied. It recomputes every
  candidate from raw SSE, selects whole median-TPOT requests for repeats, and
  rejects missing, capped, edited, mismatched, temporary-power-probe, out-of-
  archive or prompt-cache-contaminated measurements. Example:

  ```sh
  python3 tools/export_qwen36_replays.py --input /path/to/capture-archive \
    --prompts /path/to/capture-archive/prompts.json \
    --selection docs/benchmarks/qwen36-capture-selection-20260908.json \
    --output src/qwen36-replay-data-20260908.js
  ```
- `test_capture_replay.py`, `test_export_qwen36_replays.py`: focused unit
  tests; synthetic fixtures stay in temporary directories, never site data.
- `check_playground_browser.py`: explicit native headless-Chrome integration
  test for the 18 timelines, TTFT, bursts, exact outputs and old examples.

Raw SSE, native launch arguments, process receipts, memory samples and failed
diagnostics remain in the experiment archive; they are not served as public
website assets. The public dataset is a validated projection of that archive,
not a reconstruction from summary throughput numbers.

### Source and binary provenance

Target GGUF: 9,421,649,536 bytes, SHA-256
`d291e12a0f693b5f14c11bb1278ba43b432b2b6f6c33c7fdaf750c214ff83f28`.
Draft GGUF: 235,692,224 bytes, SHA-256
`107b7a433cbbdb4656a52b7845b35f6eed94eb0efb0019c8af7faed16d2d52f7`.
Both were rechecked on both hosts.

| Artifact | SHA-256 |
| --- | --- |
| Mac official llama-server | `924efd9a6546036ea927ca6e68cc3c11cbdc81ce9ff8cda241218bccd2df9eee` |
| Windows official llama-server.exe | `23ddf7b4a9a00b5991b6afd9863c2dfaada381a4d004baebc5c773ae7daa3ece` |
| Mac prima-server | `267422f0db4a5e88f5ca1bbde18a94284c0bf321cc0840635b52f3434b5d79ac` |
| Windows prima-server.exe | `d73c498108e174c08e9d0bd673348376acb01434786a4c31865bca858858faca` |

The Windows official baseline was built natively with CUDA 12.4.1,
MSVC 19.44.35228, sm_75 and CUDA Graphs enabled; the GPU driver was 551.86.
Only an official source export, not a `.git` directory, was transferred.
Consequently the binary reports `commit unknown`; it must not be presented as
a Git-stamped build. Its official source archives were checked:

- Main source archive SHA-256:
  `1fef7af765efd5f10a7d9e11386da1865acd80f520b825eea987241f9dfc57d3`.
- Missing UI-provisioning-script supplement, from the same official commit:
  `ceba5ebcc4dbd52c26a23d49e5c99e871c12f11dad3ad9c30abac559628dabff`.

PRIMA's Mac parent is `dc15bb0734b465c273b2080d9719f00f91c73830`, with
materialized nested runtime `d2716855d7ed3241d014c5ec0cb24d56df1555ac` over the
official base above. The parent gitlink and ordered 56-patch constraint are
unchanged. Windows uses the previously validated, Mac-authored runtime overlay
and the checked binary above; its Git metadata is older. This is **not** a new
same-parent-commit cluster acceptance claim. No runtime source changes were
made for this website dataset.
