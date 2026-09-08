#!/usr/bin/env python3
"""Publish only a complete, internally consistent 18-request natural-EOS dataset."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

from capture_replay import SAMPLING, canonical_sha, check_reported_sampling, normalize_records, validate_request

SOURCES = {'home': 'mac', 'server': 'windows', 'prima': 'prima'}
PROMPTS = ('math', 'cs', 'literature')
BASE = '9558fa44c92746a58dd07ad1bf0c889715b938a6'
PUBLIC_CONFIG_FIELDS = (
    'sourceCommit', 'nestedRuntimeCommit', 'officialBase', 'executableSha256', 'modelBytes',
    'contextCapacity', 'dflash', 'cpuMoeLayers', 'mmap', 'layerSplit', 'activationDtype',
    'featureDtype', 'featureReduction', 'prefillDepth', 'cudaGraphs', 'runtimeProvenance',
    'targetFirstChunk', 'targetTailChunk', 'draftNMax',
)


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def validate_selection(selection):
    if selection is None:
        return
    if (set(selection) != {'schemaVersion', 'selectionPolicy', 'captures'}
            or selection['schemaVersion'] != 1
            or selection['selectionPolicy'] != 'median_request_mean_tpot'
            or set(selection['captures']) != set(SOURCES)):
        raise ValueError('Invalid capture-selection manifest')
    for modes in selection['captures'].values():
        if set(modes) != {'off', 'on'}:
            raise ValueError('Both DFlash modes must be explicitly selected')
        for prompts in modes.values():
            if set(prompts) != set(PROMPTS):
                raise ValueError('All three prompt selections are required')
            for prompt_id, names in prompts.items():
                if (not isinstance(names, list) or not names or len(names) % 2 != 1
                        or any(not isinstance(name, str) for name in names)
                        or len(set(names)) != len(names)):
                    raise ValueError('Use an odd number of distinct complete captures, never an averaged trace')
                pattern = r'[a-z0-9-]+/' + prompt_id + r'(?:-repeat-[1-9][0-9]*)?'
                if any(re.fullmatch(pattern, name) is None for name in names):
                    raise ValueError('Capture paths must be plain run/prompt names within the archive')


def public_configuration(config, source, mode):
    if config['contextCapacity'] != 262144 or config['dflash'] != (mode == 'on'):
        raise ValueError('Deployment configuration mismatch')
    if source != 'prima' and config['sourceCommit'] != BASE:
        raise ValueError('Not the frozen official llama.cpp baseline')
    if source == 'prima' and (config['activationDtype'] != 'fp32' or config['layerSplit'] != [14, 26]):
        raise ValueError('Unexpected distributed activation type or layer split')
    if config.get('activityProbe') or config.get('latencyProbe'):
        raise ValueError('Temporary power probes are not publication measurements')
    # No private addresses, usernames, local paths or process receipts.
    return {key: config[key] for key in PUBLIC_CONFIG_FIELDS if key in config}


def validated_capture(path, prompt_text):
    receipt, request, raw = read(path / 'result.json'), read(path / 'request.json'), read(path / 'raw-sse.json')
    validate_request(request)
    if not receipt.get('complete'):
        raise ValueError('Incomplete replay: ' + str(path))
    if receipt['metadata']['promptText'] != prompt_text:
        raise ValueError('Prompt text changed')
    request_sha = canonical_sha(request)
    if request_sha != receipt['requestSha256']:
        raise ValueError('Request fingerprint differs')
    replay = normalize_records(raw)
    effective_sampling = check_reported_sampling(replay['final'])
    if effective_sampling != receipt.get('effectiveSampling'):
        raise ValueError('Effective sampling settings differ from the receipt')
    if replay != receipt['replay']:
        raise ValueError('Saved timing/output differs from raw SSE evidence')
    if replay['final'].get('timings', {}).get('cache_n', 0) != 0:
        raise ValueError('Prompt caching contaminated a measured request')
    if replay['unavailableTokenIdCount']:
        raise ValueError('Published examples require complete observed token identities')
    public_replay = {key: value for key, value in replay.items() if key != 'final'}
    public_replay.update(requestSha256=request_sha, promptTokenSha256=canonical_sha(request['prompt']),
                         startedUtc=receipt['startedUtc'], effectiveSampling=effective_sampling)
    public_replay['computeTimings'] = replay['final'].get('timings', {})
    return public_replay, request


def build_dataset(root, prompts, selection=None):
    validate_selection(selection)
    root = root.resolve()
    dataset = dict(schemaVersion=1, model='Qwen3.6-35B-A3B', quantization='IQ1_M',
        contextCapacity=262144, sampling=SAMPLING, outputLimit=None,
        outputTermination='natural EOS', officialLlamaCommit=BASE,
        targetModelSha256='d291e12a0f693b5f14c11bb1278ba43b432b2b6f6c33c7fdaf750c214ff83f28',
        draftModelSha256='107b7a433cbbdb4656a52b7845b35f6eed94eb0efb0019c8af7faed16d2d52f7',
        prompts={}, deployments={})
    canonical_requests = {}
    for prompt in prompts:
        if prompt['id'] not in PROMPTS or prompt['id'] in dataset['prompts']:
            raise ValueError('Unexpected or duplicate prompt')
        dataset['prompts'][prompt['id']] = dict(prompt=prompt['prompt'], modes={'off': {'results': {}}, 'on': {'results': {}}})
    if set(dataset['prompts']) != set(PROMPTS):
        raise ValueError('Exactly the three original prompts are required')
    for result_id, source in SOURCES.items():
        dataset['deployments'][result_id] = {}
        for mode in ('off', 'on'):
            deployment_config = None
            deployment_command = None
            for prompt_id in PROMPTS:
                names = (selection['captures'][result_id][mode][prompt_id] if selection is not None else
                         [f'{source}-sampled-eos-dflash-{mode}/{prompt_id}'])
                candidates = []
                for name in names:
                    path = (root / name).resolve()
                    if not path.is_relative_to(root):
                        raise ValueError('Capture resolves outside the archive')
                    config = read(path.parent / 'configuration.json')
                    public_config = public_configuration(config, source, mode)
                    if deployment_config is None:
                        deployment_config, deployment_command = public_config, config.get('command')
                    elif public_config != deployment_config or config.get('command') != deployment_command:
                        raise ValueError('Runtime configuration changed within one deployment/mode')
                    replay, request = validated_capture(path, dataset['prompts'][prompt_id]['prompt'])
                    if prompt_id in canonical_requests and canonical_requests[prompt_id] != request:
                        raise ValueError('Prompt tokens or sampling differ across deployments')
                    canonical_requests[prompt_id] = request
                    candidates.append(replay)
                # Select an existing middle request, never average timestamps,
                # splice outputs, or select the maximum throughput.
                order = sorted(range(len(candidates)), key=lambda index: (candidates[index]['tpotSeconds'], index))
                selected = order[len(order) // 2]
                public_replay = candidates[selected].copy()
                public_replay['captureSelection'] = dict(
                    policy='single_recorded_request' if len(candidates) == 1 else 'median_request_mean_tpot',
                    selectedIndex=selected, sampleCount=len(candidates),
                    candidates=[{key: candidate[key] for key in (
                        'startedUtc', 'tokenCount', 'ttftSeconds', 'tpotSeconds', 'outputSha256', 'tokenIdsSha256',
                    )} for candidate in candidates])
                dataset['prompts'][prompt_id]['modes'][mode]['results'][result_id] = public_replay
            dataset['deployments'][result_id][mode] = deployment_config
    dataset['requests'] = canonical_requests
    if selection is not None:
        dataset['selectionManifestSha256'] = canonical_sha(selection)
    dataset['exportedUtc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--prompts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--selection', type=Path, help='Explicit complete matrix of original/retest capture groups')
    args = parser.parse_args()
    dataset = build_dataset(args.input, read(args.prompts), read(args.selection) if args.selection else None)
    serialized = json.dumps(dataset, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    args.output.write_text('// Generated from 18 validated, naturally terminated SSE captures.\n'
                           'export const qwen36ReplayData = ' + serialized + ';\n', encoding='utf-8')
    print(json.dumps(dict(complete=True, requests=18, output=str(args.output),
                         sha256=hashlib.sha256(args.output.read_bytes()).hexdigest())))


if __name__ == '__main__':
    main()
