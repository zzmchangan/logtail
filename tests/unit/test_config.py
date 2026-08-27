"""config 单元测试: 加载/校验/CLI 覆盖/日期占位/保存回写(保注释)."""

import os
import tempfile
import unittest

from logtail.config import (
    Config, ConfigError, apply_cli, expand_date, load_config, save_config,
)
from logtail.models import SourceConfig


def write_cfg(text: str) -> str:
    fd, p = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return p


MINIMAL = """\
log_sources:
  - name: a
    path: /tmp
    pattern: "*.log"
blacklist: ["hb"]
keywords: ["err"]
"""


class TestLoadConfig(unittest.TestCase):
    def test_none_path_empty(self):
        cfg = load_config(None)
        self.assertEqual(cfg.sources, [])
        self.assertEqual(cfg.keywords, [])

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/xx.yaml")

    def test_bad_yaml(self):
        p = write_cfg("log_sources: [unclosed")
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_top_level_not_map(self):
        p = write_cfg("- a\n- b\n")
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_minimal(self):
        p = write_cfg(MINIMAL)
        cfg = load_config(p)
        self.assertEqual(len(cfg.sources), 1)
        self.assertEqual(cfg.sources[0].name, "a")
        self.assertEqual(cfg.blacklist, ["hb"])
        self.assertEqual(cfg.keywords, ["err"])

    def test_correlation_keys(self):
        p = write_cfg(MINIMAL + """
correlation_keys:
  - name: player
    extract:
      - "Guid[:=] *(\\\\d+)"
""")
        cfg = load_config(p)
        self.assertEqual(len(cfg.correlation_keys), 1)
        self.assertEqual(cfg.correlation_keys[0]["name"], "player")

    def test_date_placeholder(self):
        p = write_cfg("""\
log_sources:
  - name: d
    path: /tmp/{date}/
    pattern: "{YYYY}_{MM}_{DD}.log"
""")
        cfg = load_config(p, date="2026-08-26")
        self.assertEqual(cfg.sources[0].path, "/tmp/2026-08-26/")
        self.assertEqual(cfg.sources[0].pattern, "2026_08_26.log")

    def test_dx_source(self):
        p = write_cfg("""\
log_sources:
  - name: s
    dx: "dx log Srv {date}"
""")
        cfg = load_config(p, date="2026-08-26")
        self.assertEqual(cfg.sources[0].dx, "dx log Srv 2026-08-26")

    def test_non_dict_source(self):
        p = write_cfg("log_sources:\n  - just_a_string\n")
        with self.assertRaises(ConfigError):
            load_config(p)


class TestValidate(unittest.TestCase):
    def src(self, **kw) -> Config:
        base = dict(name="n", path="/tmp", pattern="*.log")
        base.update(kw)
        return Config(sources=[SourceConfig(**base)])

    def test_no_sources(self):
        with self.assertRaises(ConfigError):
            Config().validate()

    def test_missing_path_and_dx(self):
        with self.assertRaises(ConfigError):
            self.src(path="").validate()

    def test_dir_not_exist(self):
        with self.assertRaises(ConfigError):
            self.src(path="/nonexistent_dir_xyz").validate()

    def test_missing_pattern(self):
        with self.assertRaises(ConfigError):
            self.src(pattern="").validate()

    def test_dx_skips_dir_check(self):
        self.src(dx="echo x", path="/nonexistent_dir_xyz").validate()  # 不抛


class TestExpandDate(unittest.TestCase):
    def test_placeholders(self):
        out = expand_date("/{date}/{YYYY}/{MM}/{DD}/", "2026-01-02")
        self.assertEqual(out, "/2026-01-02/2026/01/02/")

    def test_empty_date_uses_today(self):
        import re
        self.assertRegex(expand_date("{date}", ""), r"^\d{4}-\d{2}-\d{2}$")

    def test_empty_text(self):
        self.assertEqual(expand_date("", "2026-01-01"), "")


class TestApplyCli(unittest.TestCase):
    def test_three_parts(self):
        cfg = apply_cli(Config(), ["n:/tmp:*.log"], 0, 0)
        self.assertEqual(cfg.sources[0].name, "n")
        self.assertEqual(cfg.sources[0].pattern, "*.log")

    def test_two_parts_default_pattern(self):
        cfg = apply_cli(Config(), ["n:/tmp"], 0, 0)
        self.assertEqual(cfg.sources[0].pattern, "*.log")

    def test_one_part_rejected(self):
        with self.assertRaises(ConfigError):
            apply_cli(Config(), ["nopathsep"], 0, 0)

    def test_history_context(self):
        cfg = apply_cli(Config(), None, 10, 3)
        self.assertEqual((cfg.history, cfg.context_n), (10, 3))
        cfg = apply_cli(Config(), None, 0, 0)
        self.assertEqual((cfg.history, cfg.context_n), (0, 5))       # 默认不动

    def test_appends_not_replaces(self):
        cfg = Config(sources=[SourceConfig("old", "/tmp", "*.log")])
        apply_cli(cfg, ["new:/tmp"], 0, 0)
        self.assertEqual([s.name for s in cfg.sources], ["old", "new"])


class TestSaveConfig(unittest.TestCase):
    FULL = """\
# 顶部注释
log_sources:
  - name: a
    path: /tmp            # 行内注释要保留
    pattern: "*.log"

blacklist:
  - "hb"  # 心跳
  - "keepalive"

keywords:
  - "err"
"""

    def test_no_path(self):
        with self.assertRaises(ConfigError):
            save_config(None, [], [])

    def read(self, p):
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def test_roundtrip_preserves_everything_else(self):
        p = write_cfg(self.FULL)
        save_config(p, keywords=["k1", "k2"], blacklist=["b1"])
        text = self.read(p)
        self.assertIn("# 顶部注释", text)                             # 注释保留
        self.assertIn('path: /tmp            # 行内注释要保留', text)
        self.assertIn("log_sources:", text)
        self.assertNotIn('"hb"', text)                               # 旧黑名单被换
        self.assertIn("- b1", text)
        self.assertIn("- k1", text)
        self.assertIn("- k2", text)
        # 重新加载语义正确
        cfg = load_config(p)
        self.assertEqual(cfg.blacklist, ["b1"])
        self.assertEqual(cfg.keywords, ["k1", "k2"])

    def test_inline_comment_reapplied(self):
        # 带引号旧项 ('"hb"') 与运行时裸词 ('hb') 视为同值 -> 行内注释贴回
        p = write_cfg(self.FULL)
        save_config(p, keywords=["err"], blacklist=["hb", "new"])
        text = self.read(p)
        self.assertIn("# 心跳", text)

    def test_missing_key_appended(self):
        p = write_cfg("log_sources:\n  - name: a\n    path: /tmp\n")
        save_config(p, keywords=["x"], blacklist=[])
        cfg = load_config(p)
        self.assertEqual(cfg.keywords, ["x"])
        self.assertEqual(cfg.blacklist, [])

    def test_idempotent(self):
        p = write_cfg(self.FULL)
        save_config(p, keywords=["a", "b"], blacklist=["c"])
        once = self.read(p)
        save_config(p, keywords=["a", "b"], blacklist=["c"])
        self.assertEqual(self.read(p), once)                          # 幂等


if __name__ == "__main__":
    unittest.main()
