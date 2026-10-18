# -*- coding: utf-8 -*-
"""玩家售卖指令文字 → 结晶 Id 的解析测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.names import plort_id_from_text, display_name  # noqa: E402


class TestPlortFromText(unittest.TestCase):
    def test_empty_and_all(self):
        self.assertEqual(plort_id_from_text(""), "")
        self.assertEqual(plort_id_from_text("全部"), "")
        self.assertEqual(plort_id_from_text("全部结晶"), "")
        self.assertEqual(plort_id_from_text("所有"), "")
        self.assertEqual(plort_id_from_text("all"), "")

    def test_chinese_full_name(self):
        self.assertEqual(plort_id_from_text("粉色结晶"), "PINK_PLORT")
        self.assertEqual(plort_id_from_text("荧光结晶"), "PHOSPHOR_PLORT")
        self.assertEqual(plort_id_from_text("蜂蜜结晶"), "HONEY_PLORT")

    def test_short_prefix(self):
        self.assertEqual(plort_id_from_text("粉色"), "PINK_PLORT")
        self.assertEqual(plort_id_from_text("岩石"), "ROCK_PLORT")
        self.assertEqual(plort_id_from_text("蜂蜜"), "HONEY_PLORT")

    def test_whole_phrase(self):
        self.assertEqual(plort_id_from_text("卖粉色结晶"), "PINK_PLORT")
        self.assertEqual(plort_id_from_text("把荧光结晶卖了"), "PHOSPHOR_PLORT")

    def test_english(self):
        self.assertEqual(plort_id_from_text("PINK_PLORT"), "PINK_PLORT")
        self.assertEqual(plort_id_from_text("pink"), "PINK_PLORT")

    def test_unknown(self):
        self.assertIsNone(plort_id_from_text("黄金结晶"))
        self.assertIsNone(plort_id_from_text("随便什么"))

    def test_display_name(self):
        self.assertEqual(display_name("PINK_PLORT"), "粉色结晶")
        self.assertEqual(display_name("GOLD_SLIME"), "金史莱姆")
        self.assertEqual(display_name("CARROT_VEGGIE"), "胡萝卜")


if __name__ == "__main__":
    unittest.main()
