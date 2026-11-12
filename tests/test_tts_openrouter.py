"""The openrouter TTS engine, with the HTTP call mocked (no network, no real key)."""
import contextlib
import io
import json
import math
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tts  # noqa: E402

FAKE_KEY = "synthetic-openrouter-test-value"


def tone_pcm(seconds=0.5, rate=24000):
    """Raw s16le mono PCM, as OpenRouter returns for response_format pcm."""
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n))


class FakeResponse:
    def __init__(self, data):
        self.data, self.headers = data, {"Content-Type": "audio/pcm"}

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, data):
        self.data, self.requests = data, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return FakeResponse(self.data)


class OpenRouterEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, opener, *extra, key=FAKE_KEY):
        argv = ["tts.py", "--engine", "openrouter", "--voice", "Kore", "--text", "Hello there.", "--id", "VO1",
                "--out", str(self.path), *extra]
        out = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(tts.keys, "get", return_value=key) as get, \
                patch.object(tts.urllib.request, "build_opener", return_value=opener), contextlib.redirect_stdout(out):
            tts.main()
        return get, out.getvalue()

    def test_request_shape_and_key_loader(self):
        opener = FakeOpener(tone_pcm())
        captured = {}
        def fake_to_wav(data, dst):
            captured["data"] = data
            Path(dst).write_bytes(data)
            return 0.5
        with patch.object(tts, "to_wav", side_effect=fake_to_wav):
            get, out = self.run_main(opener, "--style", "warm and curious")
        get.assert_called_with("OPENROUTER_API_KEY")         # the same keys.get loader the gemini engine uses
        self.assertNotIn(FAKE_KEY, out)                      # the key is never printed
        (req,) = opener.requests
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/audio/speech")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "google/gemini-3.8-flash-tts")
        self.assertEqual(body["voice"], "Kore")
        self.assertEqual(body["response_format"], "pcm")
        self.assertEqual(body["input"], "Hello there.")
        self.assertEqual(body["instructions"], "warm and curious")
        with wave.open(io.BytesIO(captured["data"])) as w:   # PCM reaches ffmpeg as a proper WAV
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 24000))
            self.assertEqual(w.getnframes(), 12000)
        log = json.loads((self.path / "tts-log.json").read_text(encoding="utf-8"))
        self.assertEqual(log[0]["engine"], "openrouter")
        self.assertEqual(log[0]["file"], str(self.path / "VO1.wav"))
        self.assertEqual(log[0]["seconds"], 0.5)
        self.assertFalse((self.path / ".VO1.new.wav").exists())

    def test_missing_key_sends_nothing(self):
        opener = FakeOpener(tone_pcm())
        with self.assertRaises(SystemExit) as stop:
            self.run_main(opener, key=None)
        self.assertIn("OPENROUTER_API_KEY", str(stop.exception))
        self.assertEqual(opener.requests, [])

    def test_echoed_key_is_redacted(self):
        err = tts.urllib.error.HTTPError(tts.OPENROUTER_URL, 401, "no", {}, io.BytesIO(f"bad key {FAKE_KEY}".encode()))
        class Failing(FakeOpener):
            def open(self, req, timeout=None):
                raise err
        with patch.object(tts.urllib.request, "build_opener", return_value=Failing(b"")):
            with self.assertRaises(RuntimeError) as e:
                tts.post(tts.OPENROUTER_URL, {}, {"Authorization": f"Bearer {FAKE_KEY}"})
        self.assertNotIn(FAKE_KEY, str(e.exception))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
    def test_output_matches_gemini_format(self):
        """Same file, 48 kHz mono 24-bit, and the measured length the captions are timed from."""
        _, out = self.run_main(FakeOpener(tone_pcm(seconds=1.0)))
        dst = self.path / "VO1.wav"
        with wave.open(str(dst)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 3, 48000))
        log = json.loads((self.path / "tts-log.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(log[0]["seconds"], 1.0, delta=0.02)
        self.assertIn("VO1.wav", out)


if __name__ == "__main__":
    unittest.main()
