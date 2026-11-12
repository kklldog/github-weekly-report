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

    def test_scan_expired_preserves_confirmation_and_does_not_relaunch(self):
        result = {
            "status": "scan_expired",
            "stage": "waiting_publish_scan",
            "error": "扫码等待超时",
            "requires_verification": True,
        }
        confirmed_uid = self.pending["confirmed_uid"]
        package_hash = self.pending["package_hash"]

        def start_publisher(*args, **kwargs):
            Path(kwargs["env"]["WECHAT_RESULT_FILE"]).write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8"
            )
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            return process

        with mock.patch.dict(os.environ, {"WECHAT_PUBLISH_CMD": "mock-publisher"}), \
             mock.patch.object(weekly_pipeline.subprocess, "Popen", side_effect=start_publisher) as popen, \
             mock.patch.object(weekly_pipeline, "notify") as notify, \
             mock.patch.object(weekly_pipeline.MailService, "subscribe_forever") as subscription:
            weekly_pipeline.publish(self.folder)

            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["status"], "scan_expired")
            self.assertEqual(pending["platform_result"], result)
            self.assertEqual(pending["confirmed_uid"], confirmed_uid)
            self.assertEqual(pending["package_hash"], package_hash)

            for _ in range(3):
                weekly_pipeline.poll_once()

        popen.assert_called_once()
        subscription.assert_not_called()
        notify.assert_called_once()
        self.assertRegex(notify.call_args.args[1], r"释放|已退出|已关闭|Chrome")

    def incoming(self, **overrides):
        return {
            "uid": 13260, "messageId": "<reply@qq.com>",
            "from": [{"address": "zhouminjie1986@foxmail.com"}],
            "to": [{"address": "17014842@qq.com"}],
            "subject": "回复：" + weekly_pipeline.preview_subject(self.post["rank"], self.post["editorial"]),
            "text": "确认", "headers": {}, **overrides,
        }

    def preview_pending(self):
        self.pending["status"] = "preview"
        self.pending_path.write_text(json.dumps(self.pending), encoding="utf-8")

    def test_persisted_service_confirmation_survives_restart_and_can_publish(self):
        self.preview_pending()
        weekly_pipeline.store_notification(self.incoming())
        # poll_once reads disk, with no dependency on the subscription process.
        with mock.patch.object(weekly_pipeline, "publish") as publish:
            weekly_pipeline.poll_once(publish_confirmed=False)
            publish.assert_not_called()
            saved = json.loads(self.pending_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "confirmed")
            self.assertEqual(saved["confirmed_uid"], "13260")
            weekly_pipeline.poll_once()
            publish.assert_called_once_with(self.folder)

    def test_duplicate_events_are_saved_once_without_attachments(self):
        mail = self.incoming(attachments=[{"contentBase64": "large-image"}])
        weekly_pipeline.store_notification(mail)
        weekly_pipeline.store_notification({**mail, "id": "new-event-id"})
        files = list((self.data / "mail-inbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("attachments", json.loads(files[0].read_text(encoding="utf-8")))

    def test_changed_package_blocks_confirmation_from_service(self):
        self.preview_pending()
        weekly_pipeline.store_notification(self.incoming())
        (self.folder / "images/01.png").write_bytes(b"changed")
        with mock.patch.object(weekly_pipeline, "publish") as publish:
            with self.assertRaisesRegex(RuntimeError, "内容已发生变化"):
                weekly_pipeline.poll_once()
            publish.assert_not_called()

    def test_unrelated_service_mail_does_not_authorize_publication(self):
        self.preview_pending()
        weekly_pipeline.store_notification(self.incoming(subject="other week"))
        with mock.patch.object(weekly_pipeline, "publish") as publish:
            weekly_pipeline.poll_once()
            publish.assert_not_called()
        self.assertEqual(json.loads(self.pending_path.read_text(encoding="utf-8"))["status"], "preview")

    def test_preview_saves_service_message_id_and_sends_original_images(self):
        self.preview_pending()
        rank = {**self.post["rank"], "repo": "example/repo"}
        with mock.patch.object(weekly_pipeline, "MailService") as service:
            service.return_value.send_mail.return_value = "<actual-service-id@qq.com>"
            weekly_pipeline.send_preview(self.folder, rank, self.post["editorial"])
            args = service.return_value.send_mail.call_args.args
            self.assertEqual(args[3], sorted((self.folder / "images").glob("*.png")))
        saved = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["preview_message_id"], "<actual-service-id@qq.com>")
        self.assertEqual(saved["package_hash"], weekly_pipeline.package_hash(self.folder))

    def test_failed_preview_send_does_not_mark_sent(self):
        self.preview_pending()
        rank = {**self.post["rank"], "repo": "example/repo"}
        with mock.patch.object(weekly_pipeline, "MailService") as service:
            service.return_value.send_mail.side_effect = RuntimeError("HTTP 502")
            with self.assertRaisesRegex(RuntimeError, "502"):
                weekly_pipeline.send_preview(self.folder, rank, self.post["editorial"])
        self.assertNotIn("preview_sent_at", json.loads(self.pending_path.read_text(encoding="utf-8")))

    def test_notifications_route_qr_attachment_through_service(self):
        attachment = self.folder / "images/01.png"
        with mock.patch.object(weekly_pipeline, "MailService") as service:
            weekly_pipeline.notify("扫码", "请扫码", attachment)
            service.return_value.send_mail.assert_called_once_with(
                "zhouminjie1986@foxmail.com", "[GitHub周榜贴图] 扫码", "请扫码", [attachment])

    def test_bad_notification_does_not_block_later_confirmation(self):
        self.preview_pending()
        weekly_pipeline.store_notification(self.incoming())
        (self.data / "mail-inbox/zzz.json").write_text("{broken", encoding="utf-8")
        with self.assertLogs(weekly_pipeline.LOG, level="ERROR"), \
             mock.patch.object(weekly_pipeline, "publish") as publish:
            weekly_pipeline.poll_once(publish_confirmed=False)
            publish.assert_not_called()
        self.assertEqual(json.loads(self.pending_path.read_text(encoding="utf-8"))["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
