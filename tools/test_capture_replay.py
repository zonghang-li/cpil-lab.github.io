import io
import json
import unittest

from capture_replay import SAMPLING, check_reported_sampling, iter_sse, normalize_records, validate_request


def record(time_s, **data):
    return {"elapsedNs": int(time_s * 1e9), "data": json.dumps(data)}


def final(time_s=2.3, count=3, **kwargs):
    return record(time_s, stop=True, tokens=[], content="", tokens_predicted=count,
                  stop_type="eos", truncated=False, generation_settings={**SAMPLING, "n_predict": -1,
                      "temperature": 1.0, "top_k": 20, "top_p": .95, "min_p": .05}, **kwargs)


class CaptureTests(unittest.TestCase):
    def test_request_contract(self):
        validate_request({**SAMPLING, "prompt": [11, 12]})
        for update in ({"seed": 2}, {"temperature": 0.7}, {"cache_prompt": True}, {"prompt": []}, {"n_predict": 1024}):
            with self.assertRaises(ValueError):
                validate_request({**SAMPLING, "prompt": [11], **update})

    def test_real_ttft_intervals_and_eos(self):
        rows = [record(2, stop=False, tokens=[1], tokens_predicted=1, content="A"),
                record(2.1, stop=False, tokens=[2], tokens_predicted=2, content=" B"),
                record(2.3, stop=False, tokens=[3], tokens_predicted=3, content=""), final()]
        result = normalize_records(rows)
        self.assertEqual(result["ttftSeconds"], 2)
        self.assertAlmostEqual(result["tpotSeconds"], .15)
        self.assertEqual(result["events"][0][0], 2)
        self.assertEqual(result["output"], "A B")
        self.assertEqual(result["tokenCount"], 3)
        check_reported_sampling(result["final"])

    def test_atomic_arrivals_not_interpolated(self):
        rows = [record(1, stop=False, tokens=[1], tokens_predicted=1, content="A"),
                record(2, stop=False, tokens=[2, 3], tokens_predicted=3, content="BC"), final(2, 3)]
        events = normalize_records(rows)["events"]
        self.assertEqual([event[0] for event in events], [1, 2, 2])
        self.assertEqual(events[-1][1], 0)
        self.assertEqual("".join(event[2] for event in events), "ABC")

    def test_utf8_coalescing_ids_are_not_guessed(self):
        rows = [record(1, stop=False, tokens=[1], tokens_predicted=1, content="A"),
                record(2, stop=False, tokens=[3], tokens_predicted=3, content="中"), final(2, 3)]
        result = normalize_records(rows)
        self.assertEqual(result["unavailableTokenIdCount"], 1)
        self.assertIsNone(result["events"][1][3])
        self.assertIsNone(result["tokenIdsSha256"])
        self.assertEqual(result["events"][1][0], result["events"][2][0])

    def test_incomplete_or_inconsistent_stream_rejected(self):
        first = record(1, stop=False, tokens=[1], tokens_predicted=1, content="A")
        second = record(2, stop=False, tokens=[2], tokens_predicted=2, content="B")
        for rows in ([first], [first, final(count=4)], [second, first, final(count=2)],
                     [first, second, final(count=3)], [first, second, final(count=2), first]):
            with self.assertRaises(ValueError):
                normalize_records(rows)

    def test_sse_frames_and_heartbeat(self):
        stream = io.BytesIO(b': heartbeat\r\n\r\ndata: {"x":\ndata: 1}\n\ndata: [DONE]\n\n')
        result = list(iter_sse(stream, 0, lambda: 10))
        self.assertEqual(json.loads(result[0]["data"]), {"x": 1})
        self.assertEqual(result[1]["data"], "[DONE]")
        self.assertEqual(result[0]["elapsedNs"], 10)
        with self.assertRaises(ValueError):
            list(iter_sse(io.BytesIO(b'data: {}\n'), 0, lambda: 10))

    def test_server_sampling_acknowledgement(self):
        with self.assertRaises(ValueError):
            check_reported_sampling({"generation_settings": {**SAMPLING, "seed": 999}})
        with self.assertRaises(ValueError):
            check_reported_sampling({"generation_settings": {**SAMPLING, "n_predict": 1024}})
        for greedy in ({"temperature": 0, "top_k": 20}, {"temperature": 1, "top_k": 1}):
            with self.assertRaises(ValueError):
                check_reported_sampling({"generation_settings": {**SAMPLING, "n_predict": -1, **greedy}})
        for temperature in (.7, 1.0):
            actual = check_reported_sampling({"generation_settings": {**SAMPLING, "n_predict": -1,
                "temperature": temperature, "top_k": 20, "top_p": .95, "min_p": .05}})
            self.assertEqual(actual['temperature'], temperature)

    def test_token_limit_is_not_natural_eos(self):
        first = record(1, stop=False, tokens=[1], tokens_predicted=1, content="A")
        second = record(2, stop=False, tokens=[2], tokens_predicted=2, content="B")
        stopped = record(2.1, stop=True, tokens=[], content="", tokens_predicted=2,
                         stop_type="limit", truncated=False)
        with self.assertRaises(ValueError):
            normalize_records([first, second, stopped])


if __name__ == "__main__":
    unittest.main()
