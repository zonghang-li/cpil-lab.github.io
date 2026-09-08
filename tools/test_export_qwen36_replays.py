"""Synthetic unit fixtures live only in TemporaryDirectory, never in site data."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from capture_replay import SAMPLING, canonical_sha, check_reported_sampling, normalize_records
from export_qwen36_replays import BASE, PROMPTS, SOURCES, build_dataset


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prompts = [dict(id=key, prompt='Unit fixture ' + key) for key in PROMPTS]
        for source in SOURCES.values():
            for mode in ('off', 'on'):
                run = self.root / f'{source}-sampled-eos-dflash-{mode}'
                run.mkdir()
                self.write(run / 'configuration.json', dict(contextCapacity=262144,
                    dflash=mode == 'on', sourceCommit=BASE, activationDtype='fp32', layerSplit=[14, 26]))
                for prompt in self.prompts:
                    path = run / prompt['id']
                    path.mkdir()
                    request = dict(SAMPLING, prompt=[11, 12])
                    settings = dict(SAMPLING, n_predict=-1, temperature=1.0, top_k=20, top_p=.95, min_p=.05)
                    frames = [dict(stop=False, content='A', tokens=[1], tokens_predicted=1),
                              dict(stop=False, content=' B', tokens=[2], tokens_predicted=2),
                              dict(stop=True, content='', tokens=[], tokens_predicted=2, stop_type='eos',
                                   truncated=False, generation_settings=settings, timings=dict(cache_n=0))]
                    raw = [dict(elapsedNs=(i + 1) * 1000000000, data=json.dumps(frame)) for i, frame in enumerate(frames)]
                    replay = normalize_records(raw)
                    receipt = dict(complete=True, metadata=dict(promptText=prompt['prompt']),
                        requestSha256=canonical_sha(request), replay=replay,
                        effectiveSampling=check_reported_sampling(replay['final']), startedUtc='2000-01-01T00:00:00Z')
                    for filename, value in [('request.json', request), ('raw-sse.json', raw), ('result.json', receipt)]:
                        self.write(path / filename, value)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_all_18_and_seed_and_eos_required(self):
        dataset = build_dataset(self.root, self.prompts)
        self.assertEqual(sum(len(mode['results']) for prompt in dataset['prompts'].values()
                             for mode in prompt['modes'].values()), 18)
        self.assertIsNone(dataset['outputLimit'])
        self.assertNotIn('temperature', dataset['sampling'])
        self.assertEqual(dataset['sampling']['seed'], 1234)
        replay = dataset['prompts']['math']['modes']['on']['results']['prima']
        self.assertEqual(replay['effectiveSampling']['temperature'], 1)
        self.assertEqual(replay['ttftSeconds'], 1)
        self.assertEqual(replay['tpotSeconds'], 1)

    def test_missing_trace_rejected(self):
        (self.root / 'mac-sampled-eos-dflash-on/math/result.json').unlink()
        with self.assertRaises(FileNotFoundError):
            build_dataset(self.root, self.prompts)

    def test_edited_output_rejected(self):
        path = self.root / 'mac-sampled-eos-dflash-on/math/result.json'
        receipt = json.loads(path.read_text())
        receipt['replay']['output'] = 'Edited answer'
        self.write(path, receipt)
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts)

    def test_prompt_mismatch_rejected(self):
        path = self.root / 'windows-sampled-eos-dflash-on/math/request.json'
        self.write(path, dict(SAMPLING, prompt=[111]))
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts)

    def selection(self):
        return dict(schemaVersion=1, selectionPolicy='median_request_mean_tpot', captures={
            result_id: {mode: {prompt: [f'{source}-sampled-eos-dflash-{mode}/{prompt}']
                              for prompt in PROMPTS} for mode in ('off', 'on')}
            for result_id, source in SOURCES.items()})

    def repeated_selection(self):
        selection = self.selection()
        names = []
        for index, tpot in enumerate((3, 1, 2)):
            run = self.root / f'prima-retest-{index}'
            shutil.copytree(self.root / 'prima-sampled-eos-dflash-on', run)
            path = run / 'math'
            raw = json.loads((path / 'raw-sse.json').read_text())
            for frame, seconds in zip(raw, (index + 1, index + 1 + tpot, index + 2 + tpot)):
                frame['elapsedNs'] = seconds * 1000000000
            receipt = json.loads((path / 'result.json').read_text())
            receipt['replay'] = normalize_records(raw)
            self.write(path / 'raw-sse.json', raw)
            self.write(path / 'result.json', receipt)
            names.append(run.name + '/math')
        selection['captures']['prima']['on']['math'] = names
        return selection

    def test_median_selects_one_whole_observed_request(self):
        selection = self.repeated_selection()
        dataset = build_dataset(self.root, self.prompts, selection)
        replay = dataset['prompts']['math']['modes']['on']['results']['prima']
        self.assertEqual(replay['captureSelection']['selectedIndex'], 2)
        self.assertEqual(replay['captureSelection']['sampleCount'], 3)
        self.assertEqual(replay['tpotSeconds'], 2)
        self.assertEqual(replay['ttftSeconds'], 3)
        expected = json.loads((self.root / 'prima-retest-2/math/result.json').read_text())['replay']
        self.assertEqual(replay['events'], expected['events'])
        self.assertEqual(dataset['selectionManifestSha256'], canonical_sha(selection))
        self.assertEqual(len(replay['captureSelection']['candidates']), 3)

    def test_invalid_unselected_candidate_is_not_silently_skipped(self):
        selection = self.repeated_selection()
        path = self.root / 'prima-retest-0/math/result.json'
        receipt = json.loads(path.read_text())
        receipt['complete'] = False
        self.write(path, receipt)
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts, selection)

    def test_even_or_duplicate_candidates_rejected(self):
        for names in (['prima-sampled-eos-dflash-on/math'] * 3,
                      ['prima-sampled-eos-dflash-on/math', 'prima-retest-2/math']):
            selection = self.selection()
            selection['captures']['prima']['on']['math'] = names
            with self.assertRaises(ValueError):
                build_dataset(self.root, self.prompts, selection)

    def test_incomplete_manifest_rejected_without_fallback(self):
        selection = self.selection()
        del selection['captures']['server']['on']
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts, selection)

    def test_path_escape_and_wrong_prompt_rejected(self):
        for name in ('../private/math', '/private/math', 'run/cs', 'run/math/extra'):
            selection = self.selection()
            selection['captures']['prima']['on']['math'] = [name]
            with self.assertRaises(ValueError):
                build_dataset(self.root, self.prompts, selection)

    def test_candidate_runtime_drift_rejected(self):
        selection = self.repeated_selection()
        path = self.root / 'prima-retest-0/configuration.json'
        config = json.loads(path.read_text())
        config['targetFirstChunk'] = 'off'
        self.write(path, config)
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts, selection)

    def test_power_probe_not_published(self):
        path = self.root / 'prima-sampled-eos-dflash-on/configuration.json'
        config = json.loads(path.read_text())
        config['activityProbe'] = True
        self.write(path, config)
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts)

    def test_different_context_blog_capture_not_published(self):
        path = self.root / 'mac-sampled-eos-dflash-on/configuration.json'
        config = json.loads(path.read_text())
        config['contextCapacity'] = 131072
        self.write(path, config)
        with self.assertRaises(ValueError):
            build_dataset(self.root, self.prompts)

    def test_private_configuration_fields_not_exported(self):
        for source in SOURCES.values():
            for mode in ('off', 'on'):
                path = self.root / f'{source}-sampled-eos-dflash-{mode}/configuration.json'
                config = json.loads(path.read_text())
                config['command'] = ['unit-exe', '/private/example', 'PRIVATE_TEST_MARKER']
                config['privateHost'] = 'PRIVATE_TEST_MARKER'
                self.write(path, config)
        self.assertNotIn('PRIVATE_TEST_MARKER', json.dumps(build_dataset(self.root, self.prompts)))


if __name__ == '__main__':
    unittest.main()
