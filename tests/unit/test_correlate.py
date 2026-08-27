"""correlate 单元测试: 归一化、多正则抽取、预设、坏正则跳过."""

import unittest

from logtail.correlate import CorrelationKeys, normalize


class TestNormalize(unittest.TestCase):
    def test_whitespace_stripped(self):
        self.assertEqual(normalize(" 123 "), "123")
        self.assertEqual(normalize("a b"), "ab")

    def test_leading_zeros(self):
        self.assertEqual(normalize("007"), "7")
        self.assertEqual(normalize("000"), "0")                     # 全零不空串

    def test_non_numeric_untouched(self):
        self.assertEqual(normalize("16bd7af3"), "16bd7af3")


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.ck = CorrelationKeys([
            {"name": "player", "extract": [r"Guid[:=] *(\d+)",
                                           r"roleId[:=] *(\d+)",
                                           r"player[:=] *(\d+)"]},
        ], presets=False)

    def test_first_pattern_wins(self):
        self.assertEqual(
            self.ck.extract1("[Player Guid:123] x", "player"), "123")

    def test_fallback_patterns(self):
        self.assertEqual(self.ck.extract1("roleId: 42 out", "player"), "42")
        self.assertEqual(self.ck.extract1("player=007 ok", "player"), "7")  # 归一化

    def test_no_match(self):
        self.assertIsNone(self.ck.extract1("nothing", "player"))
        self.assertIsNone(self.ck.extract1("x", "undefined_key"))

    def test_is_defined(self):
        self.assertTrue(self.ck.is_defined("player"))
        self.assertFalse(self.ck.is_defined("session"))             # 未开预设且未配置


class TestPresets(unittest.TestCase):
    def test_default_presets_available(self):
        ck = CorrelationKeys()
        self.assertTrue(ck.is_defined("player"))
        self.assertTrue(ck.is_defined("session"))

    def test_session_extracts_token(self):
        ck = CorrelationKeys()
        self.assertEqual(ck.extract1("s=16bd7af3&c=1", "session"), "16bd7af3")

    def test_scene_preset_covers_three_spellings(self):
        """scene 预设要认三种写法: scene: (guild) / sceneId: (scene) / scene_id=."""
        ck = CorrelationKeys()
        self.assertEqual(ck.extract1("[Info] scene:273224638266229618 x", "scene"),
                         "273224638266229618")
        self.assertEqual(ck.extract1("sceneId:123 abc", "scene"), "123")
        self.assertEqual(ck.extract1("scene_id=45", "scene"), "45")
        self.assertIsNone(ck.extract1("no scene here", "scene"))

    def test_config_overrides_preset(self):
        ck = CorrelationKeys([{"name": "player", "extract": [r"uid=(\d+)"]}])
        self.assertEqual(ck.extract1("uid=9", "player"), "9")
        self.assertIsNone(ck.extract1("player=9", "player"))        # 预设被覆盖

    def test_presets_off(self):
        ck = CorrelationKeys(presets=False)
        self.assertEqual(ck.keys(), [])

    def test_value_of_multi_keys(self):
        ck = CorrelationKeys()
        v = ck.value_of("player=5 s=abc&c=1")
        self.assertEqual(v.get("player"), "5")
        self.assertEqual(v.get("session"), "abc")


class TestBadInput(unittest.TestCase):
    def test_bad_regex_skipped(self):
        ck = CorrelationKeys([
            {"name": "k", "extract": ["[invalid", r"ok=(\d+)"]},
        ])
        # 坏正则跳过, 好正则仍可用
        self.assertEqual(ck.extract1("ok=3", "k"), "3")

    def test_empty_entries_skipped(self):
        ck = CorrelationKeys([{"name": "", "extract": ["x"]},
                              {"name": "y"}], presets=False)
        self.assertEqual(ck.keys(), [])


class TestCaseSensitive(unittest.TestCase):
    def test_extract_respects_case(self):
        ck = CorrelationKeys([{"name": "p", "extract": [r"Player[:=] *(\d+)"]}],
                             presets=False, case_sensitive=True)
        self.assertEqual(ck.extract1("Player=5 x", "p"), "5")
        self.assertIsNone(ck.extract1("player=5 x", "p"))           # 小写不匹配
        # 默认不敏感
        ck2 = CorrelationKeys([{"name": "p", "extract": [r"Player[:=] *(\d+)"]}],
                              presets=False)
        self.assertEqual(ck2.extract1("player=5 x", "p"), "5")


if __name__ == "__main__":
    unittest.main()
