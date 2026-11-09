import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import weekly_pipeline


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data = Path(self.tempdir.name)
        self.folder = self.data / "week"
        images = self.folder / "images"
        images.mkdir(parents=True)
        for index in range(1, 4):
            (images / f"{index:02d}.png").write_bytes(f"image-{index}".encode())
        self.post_path = self.folder / "post.json"
        self.post = {
            "rank": {"week_id": "2026-11-02-11-08", "url": "https://github.com/example/repo"},
            "editorial": {"title": "周榜项目", "content": "本周项目介绍。"},
            "author": "MJZHOU",
            "status": "preview",
        }
        self.post_path.write_text(json.dumps(self.post, ensure_ascii=False), encoding="utf-8")
        self.pending_path = self.data / "pending.json"
        self.pending = {
            "week_id": self.post["rank"]["week_id"],
            "folder": str(self.folder),
            "status": "confirmed",
            "confirmed_uid": "13236",
            "package_hash": weekly_pipeline.package_hash(self.folder),
        }
        self.pending_path.write_text(json.dumps(self.pending), encoding="utf-8")
        self.data_patch = mock.patch.object(weekly_pipeline, "DATA", self.data)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_zero_exit_without_published_result_is_not_success(self):
        process = mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaises(RuntimeError):
                weekly_pipeline.publish(self.folder)

        popen.assert_called_once()
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(pending["status"], "failed")

    def test_stale_published_result_is_removed_before_current_run(self):
        result_path = self.folder / "wechat-result.json"
        result_path.write_text(json.dumps({"status": "published", "public_url": "old"}), encoding="utf-8")
        process = mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaises(RuntimeError):
                weekly_pipeline.publish(self.folder)

        popen.assert_called_once()
        self.assertFalse(result_path.exists())
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(pending["status"], "failed")

    def test_package_hash_mismatch_blocks_publisher(self):
        self.pending["package_hash"] = "wrong-hash"
        self.pending_path.write_text(json.dumps(self.pending), encoding="utf-8")
        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "邮件确认"):
                weekly_pipeline.publish(self.folder)

        popen.assert_not_called()

    def test_pending_for_a_different_folder_blocks_publisher(self):
        self.pending["folder"] = str(self.data / "another-week")
        self.pending_path.write_text(json.dumps(self.pending), encoding="utf-8")
        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "期次与发表目录不一致"):
                weekly_pipeline.publish(self.folder)

        popen.assert_not_called()

    def test_caption_over_1000_utf16_units_including_author_is_rejected(self):
        self.post["editorial"]["content"] = "x" * 990
        self.post_path.write_text(json.dumps(self.post, ensure_ascii=False), encoding="utf-8")
        caption = self.post["editorial"]["content"] + "\n\n作者：" + self.post["author"]
        self.assertGreater(len(caption.encode("utf-16-le")) // 2, 1000)
        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "1000字"):
                weekly_pipeline.publish(self.folder)

        popen.assert_not_called()

    def test_published_package_is_not_submitted_again(self):
        self.post["status"] = "published"
        self.post_path.write_text(json.dumps(self.post, ensure_ascii=False), encoding="utf-8")
        self.pending_path.unlink()
        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen") as popen:
            weekly_pipeline.publish(self.folder)

        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
