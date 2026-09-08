#!/usr/bin/env python3
"""Bounded native-server launcher for the measured playground replay dataset."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from capture_replay import SAMPLING, capture, canonical_sha
from sample_memory import MemorySampler


def post(port, endpoint, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        connection.request("GET" if body is None else "POST", endpoint, data,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        content = response.read()
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} at {endpoint}: {content[:500]!r}")
        return json.loads(content)
    finally:
        connection.close()


def save(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write("\n")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exe", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--draft-model")
    p.add_argument("--device", required=True)
    p.add_argument("--cpu-moe", type=int, required=True)
    p.add_argument("--context", type=int, default=262144, help="Allocated context capacity, independent of input length")
    p.add_argument("--gpu-embedding", action="store_true", help="Explicitly place target token embeddings on the selected GPU")
    p.add_argument("--threads", type=int, default=6)
    p.add_argument("--port", type=int, default=65220)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--prompts", type=Path, required=True)
    p.add_argument("--requests", type=Path)
    p.add_argument("--probe-only", action="store_true")
    p.add_argument("--no-mmap", action="store_true", help="Allocate separate CPU/GPU weight buffers; record explicitly")
    p.add_argument("--label", required=True)
    p.add_argument("--source-commit", required=True)
    args = p.parse_args()
    if args.context < 2 or args.cpu_moe < 0:
        p.error("Context must be at least 2 and CPU MoE layer count must be nonnegative")
    args.run_dir.mkdir(parents=True, exist_ok=False)
    if sys.platform == "darwin":
        for command in ("memory_pressure", "vm_stat"):
            result = subprocess.run([command], capture_output=True, text=True, check=True)
            (args.run_dir / (command + "-before.log")).write_text(result.stdout)
    elif os.name == "nt":
        inventory = subprocess.run(["nvidia-smi", "-i", "0", "--query-gpu=uuid,name,memory.used,memory.free,utilization.gpu",
                                    "--format=csv,noheader"], capture_output=True, text=True, check=True)
        (args.run_dir / "gpu-before.log").write_text(inventory.stdout)
        free = int(subprocess.check_output(["nvidia-smi", "-i", "0", "--query-gpu=memory.free",
                                           "--format=csv,noheader,nounits"], text=True).strip())
        if free < 10400:
            raise RuntimeError("Reviewed GPU0 starting margin unavailable")
    else:
        raise RuntimeError("This acceptance task has only Mac and Windows hosts")
    command = [args.exe, "-m", args.model, "--device", args.device, "-ngl", "all",
               "-ncmoe", str(args.cpu_moe), "-c", str(args.context), "-np", "1", "-b", "8192", "-ub", "512",
               "-t", str(args.threads), "-tb", str(args.threads), "-sm", "none", "-mg", "0",
               "-fa", "on", "-ctk", "f16", "-ctv", "f16", "--fit", "off", "--no-cache-prompt",
               "--host", "127.0.0.1", "--port", str(args.port), "--no-webui", "--offline", "--log-colors", "off",
               "--no-warmup", "--cache-ram", "0", "-lv", "4"]
    if args.no_mmap:
        command += ["--no-mmap"]
    if args.gpu_embedding:
        command += ["--override-tensor", "token_embd.weight=" + args.device]
    if args.draft_model:
        command += ["--spec-type", "draft-dflash", "--model-draft", args.draft_model,
                    "--device-draft", args.device, "--gpu-layers-draft", "99",
                    "--spec-draft-n-max", "8", "--spec-draft-p-min", "0.5"]
    environment = os.environ.copy()
    if os.name == "nt":
        environment["CUDA_VISIBLE_DEVICES"] = "0"
    metadata = {"label": args.label, "command": command, "sourceCommit": args.source_commit,
                "executableSha256": sha(args.exe), "modelBytes": Path(args.model).stat().st_size,
                "contextCapacity": args.context, "dflash": bool(args.draft_model), "cpuMoeLayers": args.cpu_moe,
                "gpuEmbedding": args.gpu_embedding,
                "mmap": not args.no_mmap,
                "promptOrigin": "Unmodified Qwen3.8-27B playground prompt strings", "sampling": SAMPLING}
    save(args.run_dir / "configuration.json", metadata)
    process = None
    sampler = None
    stdout = (args.run_dir / "server.stdout.log").open("x")
    stderr = (args.run_dir / "server.stderr.log").open("x")
    try:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=environment,
                                   start_new_session=os.name != "nt")
        save(args.run_dir / "process.json", {"pid": process.pid, "command": command})
        sampler = MemorySampler(process.pid, args.run_dir / "memory.jsonl")
        print(f"START {args.label} pid={process.pid} ncmoe={args.cpu_moe} dflash={bool(args.draft_model)}", flush=True)
        deadline = time.monotonic() + 180
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Native server exited during startup: {process.returncode}")
            try:
                health = post(args.port, "/health")
                if health.get("status") == "ok":
                    break
            except (OSError, RuntimeError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("Native server readiness deadline reached")
            time.sleep(.5)
        props = post(args.port, "/props")
        save(args.run_dir / "props.json", props)
        if props.get("default_generation_settings", {}).get("params", {}).get("n_predict") != -1:
            raise RuntimeError("The server default applies a token limit; measured requests must be unlimited")
        stderr.flush()
        text = (args.run_dir / "server.stderr.log").read_text(errors="replace")
        if not re.search(r"n_ctx\s*=\s*" + str(args.context) + r"\b", text):
            raise RuntimeError("No evidence of the requested full context capacity")
        print(f"READY requested context confirmed: {args.context}", flush=True)
        prompts = json.loads(args.prompts.read_text(encoding="utf-8-sig"))
        request_dir = args.run_dir / "requests"
        request_dir.mkdir()
        for prompt in prompts:
            rendered = post(args.port, "/apply-template", {"messages": [{"role": "user", "content": prompt["prompt"]}],
                            "add_generation_prompt": True, "chat_template_kwargs": {"enable_thinking": False}})
            tokenized = post(args.port, "/tokenize", {"content": rendered["prompt"], "add_special": False,
                                                      "parse_special": True})["tokens"]
            request = {**SAMPLING, "prompt": tokenized}
            if args.requests:
                frozen = json.loads((args.requests / (prompt["id"] + ".json")).read_text(encoding="utf-8-sig"))
                if request != frozen:
                    raise RuntimeError("Canonical prompt/sampling mismatch: " + prompt["id"])
            save(request_dir / (prompt["id"] + ".json"), request)
            save(request_dir / (prompt["id"] + "-rendered.json"), {"original": prompt["prompt"], "rendered": rendered,
                                                                    "promptTokenSha256": canonical_sha(tokenized)})
        warmup_request = json.loads((request_dir / (prompts[0]["id"] + ".json")).read_text())
        warmup_request.update(n_predict=16, stream=False)
        warmup = post(args.port, "/completion", warmup_request)
        save(args.run_dir / "warmup.json", warmup)
        print("WARMUP completed (excluded from formal replay)", flush=True)
        if not args.probe_only:
            for prompt in prompts:
                request = json.loads((request_dir / (prompt["id"] + ".json")).read_text())
                print("CAPTURE " + prompt["id"], flush=True)
                result = capture(f"http://127.0.0.1:{args.port}", request, args.run_dir / prompt["id"],
                                 {**metadata, "promptId": prompt["id"], "promptText": prompt["prompt"]})
                replay = result["replay"]
                print(json.dumps({"prompt": prompt["id"], "tokens": replay["tokenCount"],
                                  "ttft": replay["ttftSeconds"], "tpot": replay["tpotSeconds"]}), flush=True)
        print("COMPLETE " + args.label, flush=True)
    finally:
        if sampler is not None:
            sampler.stop()
        if process is not None and process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        stdout.close()
        stderr.close()
        if process is not None:
            print(f"STOPPED owned server pid={process.pid} exit={process.returncode}", flush=True)


if __name__ == "__main__":
    main()
