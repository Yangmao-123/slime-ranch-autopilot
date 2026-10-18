"""观望扫描、文件交接和 SDK 提交回归；不连接真实游戏或启动热键。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.bridge import BridgeIOController
from core.comms import Comms
from core.main_controller import Brain
from core.settings import Settings

spec = importlib.util.spec_from_file_location("slime_watch_test_entry", ROOT / "__init__.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def nearby(**changes):
    value = {"ok": True, "zone": "测试区域", "slimes": [{"id": "PINK_SLIME", "n": 2}],
             "plorts": [], "foods": []}
    value.update(changes)
    return value


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comms = Comms(self.tmp.name)
        self.brain = Brain(Settings(data_dir=self.tmp.name, backend="sim"), self.comms)
        self.brain.io = SimpleNamespace(scan_near=Mock(return_value=nearby()))
        self.clock = patch("core.main_controller.time.monotonic", return_value=0.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def scan(self, at):
        self.now.return_value = at
        self.brain._watch_tick()

    def messages(self):
        path = Path(self.comms.outbox_path)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_first_scan_combines_species_zone_and_summary_once(self):
        self.scan(0)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("首次来到「测试区域」", messages[0]["text"])
        self.assertIn("第一次见到粉色史莱姆", messages[0]["text"])
        self.assertIn("粉色史莱姆×2", messages[0]["text"])
        self.assertEqual(len(self.comms.get_state()["reports"]), 1)

    def test_known_area_reports_counts_food_and_plort_changes(self):
        self.scan(0)
        self.brain.io.scan_near.return_value = nearby(
            slimes=[{"id": "PINK_SLIME", "n": 20}],
            plorts=[{"id": "PINK_PLORT", "n": 15}], foods=[{"id": "CARROT_VEGGIE", "n": 8}])
        self.scan(8)
        text = self.messages()[-1]["text"]
        for part in ("粉色史莱姆×20", "结晶×15", "食物×8"):
            self.assertIn(part, text)
        self.assertNotIn("第一次见到", text)
        self.scan(16)
        self.assertEqual(len(self.messages()), 2)
        self.brain.io.scan_near.return_value = nearby(slimes=[])
        self.scan(24)
        self.assertIn("没有扫描到", self.messages()[-1]["text"])

    def test_restart_reports_current_surroundings_even_if_already_in_catalog(self):
        self.scan(0)
        restarted = Brain(Settings(data_dir=self.tmp.name, backend="sim"), self.comms)
        restarted.io = self.brain.io
        restarted._watch_tick()
        self.assertEqual(len(self.messages()), 2)
        self.assertNotIn("第一次见到", self.messages()[-1]["text"])

    def test_scheduler_uses_seconds_and_rare_cooldown_uses_seconds(self):
        self.brain.io.scan_near.return_value = nearby(slimes=[{"id": "GOLD_SLIME", "n": 1}])
        self.scan(0)
        for at in (0, 1, 2, 4, 7.99):
            self.scan(at)
        self.assertEqual(self.brain.io.scan_near.call_count, 1)
        for at in range(8, 65, 8):
            self.scan(at)
        self.assertEqual(self.brain.io.scan_near.call_count, 9)
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("【稀有】", self.messages()[-1]["text"])

    def test_failed_outbox_write_does_not_consume_discovery(self):
        with patch.object(self.comms, "emit", return_value=False):
            self.scan(0)
        self.assertIsNone(self.brain._watch_snapshot)
        self.assertEqual(self.brain._disc["seen_slimes"], [])
        self.assertFalse((Path(self.tmp.name) / "discoveries.json").exists())
        self.scan(8)
        self.assertIn("第一次见到", self.messages()[0]["text"])

    def test_scan_failure_warns_recovers_and_does_not_clear_snapshot(self):
        self.scan(0)
        snapshot = self.brain._watch_snapshot
        self.brain.io.scan_near.return_value = {"ok": False, "note": "unknown action scan_near"}
        self.scan(8)
        self.scan(16)
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("SlimeBridge", self.messages()[-1]["text"])
        self.assertEqual(self.brain._watch_snapshot, snapshot)
        self.brain.io.scan_near.return_value = nearby(slimes=[{"id": "ROCK_SLIME", "n": 1}])
        self.scan(24)
        self.assertIn("岩石史莱姆", self.messages()[-1]["text"])

    def test_catalog_with_invalid_shape_is_recovered(self):
        (Path(self.tmp.name) / "discoveries.json").write_text('[1,2]', encoding="utf-8")
        self.scan(0)
        self.assertEqual(len(self.messages()), 1)

    def test_full_loop_deducts_io_time_from_scan_schedule(self):
        elapsed = [0.0]
        starts = []
        self.now.side_effect = lambda: elapsed[0]

        def scan(radius):
            self.assertEqual(radius, 30.0)
            starts.append(elapsed[0])
            elapsed[0] += 1.0
            return nearby()

        def money():
            elapsed[0] += 0.5
            return 0

        self.brain.io = SimpleNamespace(scan_near=scan, read_money=money, enter_game=lambda: {"ok": True})
        self.brain._hotkey = SimpleNamespace(start=lambda: False, stop=lambda: None)
        with patch("core.main_controller.time.sleep", side_effect=lambda sec: elapsed.__setitem__(0, elapsed[0] + sec)):
            self.brain.run(lambda: len(starts) >= 3)
        self.assertEqual(starts, [0.5, 8.5, 16.5])

    def test_backend_without_scan_keeps_normal_poll_interval(self):
        self.brain.io = SimpleNamespace(read_money=lambda: 0, enter_game=lambda: {"ok": True})
        self.brain._hotkey = SimpleNamespace(start=lambda: False, stop=lambda: None)
        with patch("core.main_controller.time.sleep") as sleep:
            self.brain.run(lambda: self.brain.ticks >= 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 2.0])

    def test_return_to_watch_scans_immediately(self):
        self.scan(0)
        self.now.return_value = 1.0
        self.brain._set_mode("ranch")
        self.brain._set_mode("watch")
        self.scan(1)
        self.assertEqual(self.brain.io.scan_near.call_count, 2)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = self.make_plugin()
        self.plugin._prepare_outbox()
        self.comms = Comms(self.tmp.name)

    def make_plugin(self):
        with patch.object(entry.tempfile, "gettempdir", return_value=self.tmp.name):
            plugin = entry.SlimeRanchAutopilotPlugin()
        plugin._runtime_dir = self.tmp.name
        plugin.push_message = Mock(return_value={"submitted": True})
        return plugin

    def test_rejection_retries_then_success_is_not_replayed_after_restart(self):
        self.comms.report_discovery("one", "周边发现")
        self.plugin.push_message.return_value = {"submitted": False, "reason": "transport_unavailable"}
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin._outbox_offset, 0)
        restarted = self.make_plugin()
        restarted._prepare_outbox()
        restarted._drain_outbox()
        self.assertEqual(restarted.push_message.call_count, 1)
        self.assertGreater(restarted._outbox_offset, 0)
        again = self.make_plugin()
        again._prepare_outbox()
        again._drain_outbox()
        again.push_message.assert_not_called()

    def test_failed_item_keeps_following_messages_in_order(self):
        self.comms.say("一")
        self.comms.say("二")
        self.plugin.push_message.side_effect = [RuntimeError("transport"), {"submitted": True}, {"submitted": True}]
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)
        self.plugin._drain_outbox()
        texts = [call.kwargs["parts"][0]["text"] for call in self.plugin.push_message.call_args_list]
        self.assertEqual(texts, ["一", "一", "二"])

    def test_failure_notification_is_throttled_and_recovery_reported(self):
        self.comms.say("等待投递")
        self.plugin.logger = Mock()
        self.plugin.push_message.return_value = {"submitted": False}
        self.plugin._drain_outbox()
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.logger.warning.call_count, 1)
        self.plugin.push_message.return_value = {"submitted": True}
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.logger.info.call_count, 1)

    def test_first_upgrade_archives_legacy_log_only_once(self):
        Path(self.tmp.name, "outbox.cursor.json").unlink()
        self.comms.say("旧版本日志")
        self.plugin._prepare_outbox()
        self.assertTrue(Path(self.tmp.name, "outbox.prev.jsonl").exists())
        self.plugin._drain_outbox()
        self.plugin.push_message.assert_not_called()
        self.comms.say("新版本消息")
        self.plugin._prepare_outbox()
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)

    def test_comms_disk_failure_does_not_mark_event_reported(self):
        with patch("core.comms.open", side_effect=OSError("disk"), create=True):
            self.assertFalse(self.comms.report_discovery("one", "周边发现"))
        self.assertEqual(self.comms.get_state()["reports"], [])
        self.assertTrue(self.comms.report_discovery("one", "周边发现"))
        self.assertTrue(self.comms.report_discovery("one", "周边发现"))
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)

    def test_incomplete_utf8_line_waits_for_writer(self):
        path = Path(self.comms.outbox_path)
        row = json.dumps({"text": "周边发现"}, ensure_ascii=False).encode("utf-8") + b"\n"
        path.write_bytes(row[:-3])
        self.plugin._drain_outbox()
        self.plugin.push_message.assert_not_called()
        with path.open("ab") as f:
            f.write(row[-3:])
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)

    def test_invalid_complete_line_does_not_block_following_message(self):
        Path(self.comms.outbox_path).write_bytes(b"bad-json\n")
        self.comms.say("有效消息")
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)

    def test_checkpoint_failure_leaves_event_available_for_retry(self):
        self.comms.say("待确认")
        with patch.object(entry.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.plugin._drain_outbox()
        self.assertEqual(self.plugin._outbox_offset, 0)
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 2)

    def test_watch_event_reaches_sdk_once_with_model_response_requested(self):
        brain = Brain(Settings(data_dir=self.tmp.name, backend="sim"), self.comms)
        brain.io = SimpleNamespace(scan_near=lambda radius: nearby())
        brain._watch_tick()
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)
        self.assertEqual(self.plugin.push_message.call_args.kwargs["ai_behavior"], "respond")
        self.assertIn("测试区域", self.plugin.push_message.call_args.kwargs["parts"][0]["text"])
        self.plugin._drain_outbox()
        self.assertEqual(self.plugin.push_message.call_count, 1)

    def test_http_scan_to_sdk_submission(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                result = {"ok": True, "payload": json.dumps(nearby())}
                body = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            brain = Brain(Settings(data_dir=self.tmp.name, backend="sim"), self.comms)
            brain.io = BridgeIOController(port=server.server_address[1])
            brain._watch_tick()
            self.plugin._drain_outbox()
            self.assertEqual(requests, [{"action": "scan_near", "kind": "30.0"}])
            self.assertEqual(self.plugin.push_message.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


class ScanProtocolTests(unittest.TestCase):
    def test_valid_payload_and_radius(self):
        bridge = BridgeIOController()
        bridge.client.post_json = Mock(return_value={"ok": True, "payload": json.dumps(nearby())})
        result = bridge.scan_near(30.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["slimes"], [{"id": "PINK_SLIME", "n": 2}])
        self.assertEqual(bridge.client.post_json.call_args.args[1], {"action": "scan_near", "kind": "30.0"})

    def test_invalid_payload_is_not_an_empty_successful_scan(self):
        bridge = BridgeIOController()
        for payload in ("", "broken", "[]", "{}", json.dumps(nearby(foods="bad"))):
            with self.subTest(payload=payload):
                bridge.client.post_json = Mock(return_value={"ok": True, "payload": payload})
                self.assertFalse(bridge.scan_near()["ok"])


if __name__ == "__main__":
    unittest.main()
