#!/usr/bin/env python3
"""Capture native /completion SSE on loopback without inventing token timings."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import math
from pathlib import Path
import platform
import time
from urllib.parse import urlsplit


SAMPLING = {
    # Keep the seed fixed, but allow the server's ordinary sampling defaults.
    # In particular, do not force temperature=0 or top_k=1 (both are greedy).
    "seed": 1234, "repeat_penalty": 1.0,
    "ignore_eos": False, "cache_prompt": False, "stream": True,
    "return_tokens": True, "n_probs": 0,
}


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_request(request):
    if not isinstance(request, dict) or set(request) != set(SAMPLING) | {"prompt"}:
        raise ValueError("Unexpected request fields")
    for key, value in SAMPLING.items():
        if request[key] != value or (isinstance(value, bool) and type(request[key]) is not bool):
            raise ValueError("Sampling contract differs: " + key)
    prompt = request["prompt"]
    if not isinstance(prompt, list) or not prompt or any(type(x) is not int or x < 0 for x in prompt):
        raise ValueError("A nonempty frozen prompt token-ID array is required")


def iter_sse(response, started, clock=time.perf_counter_ns):
    """Timestamp completed SSE frames, not a guessed per-token compute clock."""
    fields = []
    size = 0
    while True:
        line = response.readline(8 * 1024 * 1024 + 1)
        now = clock()
        if now - started > 900 * 1_000_000_000:
            raise TimeoutError("Request exceeded the 15-minute capture limit")
        if not line:
            if fields:
                raise ValueError("SSE ended inside an incomplete frame")
            return
        size += len(line)
        if size > 8 * 1024 * 1024:
            raise ValueError("SSE frame exceeds bounded capture size")
        line = line.rstrip(b"\r\n")
        if not line:
            if fields:
                yield {"elapsedNs": now - started, "data": b"\n".join(fields).decode("utf-8")}
            fields = []
            size = 0
        elif line.startswith(b"data:"):
            fields.append(line[5:].removeprefix(b" "))


def normalize_records(records):
    """Flatten atomic arrivals at the same instant; never interpolate gaps."""
    events = []
    groups = []
    final = None
    previous_ns = -1
    decoded = 0
    missing_ids = 0
    for record in records:
        elapsed_ns = record["elapsedNs"]
        if type(elapsed_ns) is not int or elapsed_ns < previous_ns or elapsed_ns < 0:
            raise ValueError("Invalid or non-monotonic arrival timestamp")
        previous_ns = elapsed_ns
        if record["data"] == "[DONE]":
            continue
        data = json.loads(record["data"])
        if "error" in data:
            raise ValueError("Server returned an error: " + str(data["error"]))
        if final is not None:
            raise ValueError("Completion data follows its final frame")
        if data.get("stop") is True:
            if data.get("tokens") or data.get("content"):
                raise ValueError("Unexpected final payload: ambiguous duplicate token ownership")
            final = data
            continue
        if "prompt_progress" in data or not data.get("tokens"):
            if data.get("content"):
                raise ValueError("Visible content has no token identity")
            continue
        count = data.get("tokens_predicted")
        token_ids = data["tokens"]
        text = data.get("content", "")
        if type(count) is not int or count <= decoded or not isinstance(text, str):
            raise ValueError("Invalid token count or content")
        if not isinstance(token_ids, list) or any(type(x) is not int or x < 0 for x in token_ids):
            raise ValueError("Invalid streamed token IDs")
        delta = count - decoded
        if len(token_ids) > delta:
            raise ValueError("Streamed IDs exceed the decoded-token delta")
        # llama.cpp can defer an incomplete UTF-8 sequence and expose only the
        # final token ID when the text becomes valid. Preserve this as an atomic
        # observed group; null IDs mean unavailable, never guessed identities.
        unknown = delta - len(token_ids)
        missing_ids += unknown
        ids = [None] * unknown + token_ids
        arrival = elapsed_ns / 1e9
        groups.append({"timeSeconds": arrival, "tokenCount": delta,
                       "tokenIds": ids, "text": text})
        for index, token_id in enumerate(ids):
            interval = arrival - events[-1][0] if events else None
            events.append([arrival, interval, text if index == delta - 1 else "", token_id])
        decoded = count
    if final is None or not events:
        raise ValueError("Incomplete capture: missing final frame or generated tokens")
    if final.get("tokens_predicted") != decoded:
        raise ValueError("Final generated-token count differs from observed arrivals")
    if final.get("truncated") or final.get("stop_type") != "eos":
        raise ValueError("A valid replay must naturally reach EOS without context truncation or a token limit")
    if decoded < 2:
        raise ValueError("At least two output tokens are needed to measure TPOT")
    tpot = (events[-1][0] - events[0][0]) / (decoded - 1)
    if not math.isfinite(tpot) or tpot <= 0:
        raise ValueError("Nonpositive request-level TPOT")
    output = "".join(event[2] for event in events)
    if not output.strip():
        raise ValueError("Completion has no visible output")
    return {
        "captureStatus": "complete_eos",
        "timeOrigin": "request_submission", "timingResolution": "observed_sse_arrival",
        "tokenCountPolicy": "All observed generated tokens, including the terminal EOS when emitted.",
        "metricDefinition": "(last token arrival - first token arrival) / (output tokens - 1); excludes TTFT.",
        "tokenCount": decoded, "output": output,
        "ttftSeconds": events[0][0], "tpotSeconds": tpot,
        "tokensPerSecond": 1 / tpot, "requestDurationSeconds": previous_ns / 1e9,
        "events": events, "arrivalGroups": groups, "unavailableTokenIdCount": missing_ids,
        "outputSha256": hashlib.sha256(output.encode()).hexdigest(),
        "tokenIdsSha256": canonical_sha([e[3] for e in events]) if missing_ids == 0 else None,
        "final": final,
    }


def check_reported_sampling(final):
    settings = final.get("generation_settings", {})
    for key in ("seed", "repeat_penalty", "ignore_eos"):
        if settings.get(key) != SAMPLING[key]:
            raise ValueError("Server sampling acknowledgement differs: " + key)
    if settings.get("n_predict") != -1:
        raise ValueError("Server applies an output-token limit instead of unlimited generation to EOS")
    temperature = settings.get("temperature")
    if not isinstance(temperature, (float, int)) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Unexpected greedy or invalid temperature")
    if type(settings.get("top_k")) is not int or settings["top_k"] == 1:
        raise ValueError("Unexpected top-k=1 greedy restriction")
    return {key: settings.get(key) for key in (
        "temperature", "seed", "top_k", "top_p", "min_p", "repeat_penalty", "repeat_last_n",
        "presence_penalty", "frequency_penalty", "samplers", "n_predict", "ignore_eos")}


def capture(base_url, request, output_dir, metadata):
    validate_request(request)
    target = urlsplit(base_url)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Capture must run on the native server host via loopback HTTP")
    if target.username or target.password or target.path not in ("", "/") or target.query or target.fragment:
        raise ValueError("Unexpected base URL components")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    records = []
    receipt = {"complete": False, "metadata": metadata, "platform": platform.platform(),
               "requestSha256": canonical_sha(request), "promptTokenSha256": canonical_sha(request["prompt"]),
               "startedUtc": dt.datetime.now(dt.timezone.utc).isoformat()}
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=120)
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    try:
        started = time.perf_counter_ns()
        connection.request("POST", "/completion", payload,
                           {"Content-Type": "application/json", "Accept": "text/event-stream", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200 or "text/event-stream" not in response.getheader("Content-Type", ""):
            raise ValueError("Expected HTTP 200 SSE, got " + str(response.status))
        for record in iter_sse(response, started):
            records.append(record)
        replay = normalize_records(records)
        effective_sampling = check_reported_sampling(replay["final"])
        receipt.update(complete=True, replay=replay, effectiveSampling=effective_sampling)
        return receipt
    except BaseException as exc:
        receipt["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        connection.close()
        for name, value in (("request.json", request), ("raw-sse.json", records), ("result.json", receipt)):
            with (output_dir / name).open("x", encoding="utf-8", newline="\n") as output:
                json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    metadata = json.loads(args.metadata.read_text(encoding="utf-8-sig"))
    result = capture(args.base_url, request, args.output, metadata)
    replay = result["replay"]
    print(json.dumps({"complete": True, "output": str(args.output), "tokens": replay["tokenCount"],
                      "ttft_s": replay["ttftSeconds"], "tpot_s": replay["tpotSeconds"],
                      "tok_s": replay["tokensPerSecond"], "unknown_ids": replay["unavailableTokenIdCount"]}), flush=True)


if __name__ == "__main__":
    main()
