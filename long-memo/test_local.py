"""Offline checks. No OpenAI key or network call is used."""
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx

_data = tempfile.TemporaryDirectory()
os.environ["MEMO_DATA_DIR"] = _data.name
os.environ["MEMO_PASSWORD"] = "test-password-long-enough"
os.environ["MEMO_SESSION_SECRET"] = "test-secret-at-least-thirty-two-characters-long"

from fastapi.testclient import TestClient
import app as service
import pipeline


class MemoBetaChecks(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(service.app, base_url="https://testserver")
        login = self.client.post("/api/login", json={"password": os.environ["MEMO_PASSWORD"]})
        self.assertEqual(login.status_code, 200)
        self.headers = {"X-Memo-CSRF": login.json()["csrf"]}

    def tearDown(self):
        self.client.close()

    def test_resumable_upload_and_downloads(self):
        payload = {"title": "Bilingual meeting", "filename": "note.m4a", "size": 8, "mode": "meeting", "terms": "银行"}
        self.assertEqual(self.client.post("/api/jobs", json=payload).status_code, 403)
        created = self.client.post("/api/jobs", json=payload, headers=self.headers)
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["id"]
        url = f"/api/jobs/{job_id}/upload"
        self.assertEqual(self.client.put(url, content=b"abcd", headers={**self.headers, "X-Upload-Offset": "1"}).status_code, 409)
        self.assertEqual(self.client.put(url, content=b"abcd", headers={**self.headers, "X-Upload-Offset": "0"}).json()["offset"], 4)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").json()["offset"], 4)
        self.assertEqual(self.client.put(url, content=b"efgh", headers={**self.headers, "X-Upload-Offset": "4"}).json()["offset"], 8)
        with patch.object(service, "dispatch"):
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/finish", headers=self.headers).status_code, 200)
        job = pipeline.read_job(job_id)
        self.assertEqual((pipeline.folder(job_id) / "original").read_bytes(), b"abcdefgh")
        job["status"] = "ready"
        job["transcribed"] = [{"start": 0, "segments": [
            {"start": 2, "end": 4, "speaker": "Part 1 · speaker_0", "text": "Hello, 你好。"}]}]
        job["report"] = {key: "Test paragraph.\n\nSecond paragraph." for key in pipeline.report_schema()["required"]}
        pipeline.save_job(job)
        pipeline.write_exports(job)
        response = self.client.post(f"/api/jobs/{job_id}/speakers", json={"names": {"Part 1 · speaker_0": "Jayson"}}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Jayson: Hello, 你好。", self.client.get(f"/api/jobs/{job_id}/downloads/transcript.txt").text)
        self.assertEqual(pipeline.read_job(job_id)["report"]["brief_en"], "Test paragraph.\n\nSecond paragraph.")
        archive = self.client.get(f"/api/jobs/{job_id}/downloads/memo-downloads.zip").content
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            self.assertEqual(set(bundle.namelist()), {"transcript.txt", "comprehensive.docx", "brief.docx"})
        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}", headers=self.headers).status_code, 200)

    def test_speaker_ids_remain_part_specific(self):
        job_id = "a" * 32
        path = pipeline.folder(job_id)
        path.mkdir(exist_ok=True)
        (path / "part-0001.m4a").write_bytes(b"mock")
        job = {"id": job_id, "mode": "meeting", "speaker_refs": []}
        with patch.object(pipeline, "_post", return_value={"segments": [
            {"speaker": "speaker_0", "start": 3, "end": 5, "text": "Confirmed."}]}):
            result = pipeline.transcribe_part(job, {"file": "part-0001.m4a", "index": 1, "start": 600})
        self.assertEqual(result["segments"][0]["speaker"], "Part 2 · speaker_0")
        self.assertEqual(result["segments"][0]["start"], 603)

    def test_audio_splits_at_nearby_silence_and_reuses_manifest(self):
        job_id = "b" * 32
        path = pipeline.folder(job_id)
        path.mkdir(exist_ok=True)
        (path / "original").write_bytes(b"mock")
        job = {"id": job_id, "title": "Long audio"}
        pipeline.save_job(job)
        def fake_ffmpeg(*arguments):
            Path(arguments[-1]).write_bytes(b"prepared")
        with patch.object(pipeline, "duration", return_value=1250), patch.object(pipeline.subprocess, "run", return_value=SimpleNamespace(stderr="silence_end: 620.2\nsilence_end: 1190.5")), patch.object(pipeline, "run_ffmpeg", side_effect=fake_ffmpeg):
            parts = pipeline.split_audio(job)
        self.assertEqual([part["start"] for part in parts], [0.0, 620.2, 1190.5])
        with patch.object(pipeline, "run_ffmpeg", side_effect=AssertionError("should use existing parts")):
            self.assertEqual(pipeline.split_audio(pipeline.read_job(job_id)), parts)

    def test_multilingual_request_is_valid_multipart(self):
        job_id = "c" * 32
        path = pipeline.folder(job_id)
        path.mkdir(exist_ok=True)
        (path / "part-0000.m4a").write_bytes(b"mock")
        def inspect_request(_path, **kwargs):
            request = httpx.Client().build_request("POST", "https://example.test", data=kwargs["data"], files=kwargs["files"])
            body = request.read()
            self.assertIn(b'name="languages[]"\r\n\r\ncmn', body)
            self.assertIn(b'name="languages[]"\r\n\r\nyue', body)
            self.assertIn(b'name="file"', body)
            return {"text": "Hello, 你好。"}
        with patch.object(pipeline, "_post", side_effect=inspect_request):
            result = pipeline.transcribe_part({"id": job_id, "mode": "personal", "terms": ""}, {"file": "part-0000.m4a", "index": 0, "start": 0, "seconds": 10})
        self.assertEqual(result["segments"][0]["text"], "Hello, 你好。")

    def test_reviewed_speaker_names_appear_in_report(self):
        job = {"report": {"brief_en": "Part 1 · speaker_0 agreed."},
               "speaker_names": {"Part 1 · speaker_0": "Jayson"}}
        self.assertEqual(pipeline.named_report(job)["brief_en"], "Jayson agreed.")
        self.assertEqual(job["report"]["brief_en"], "Part 1 · speaker_0 agreed.")


if __name__ == "__main__":
    unittest.main()
