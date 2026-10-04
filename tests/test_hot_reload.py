"""
游戏内配置热更新与动态调参系统 - 验收测试
运行方式：python -m pytest tests/test_hot_reload.py -q
共 14 个测试用例
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.hot_reload import ConfigHotReload


def write_config(path, data):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp)


class TestConfigHotReload:
    def setup_method(self):
        self.system = ConfigHotReload({"weapons": {"sword": {"damage": 50}}})

    # ---- 配置监听 ----

    def test_case_01(self, tmp_path):
        """监听实际生效：修改配置文件后触发热更新"""
        cfg = tmp_path / "balance.json"
        write_config(cfg, {"weapons": {"sword": {"damage": 50}}})
        assert self.system.watch_config(str(cfg), debounce=0.1)
        write_config(cfg, {"weapons": {"sword": {"damage": 80}}})
        assert self.system.poll(now=1000.0) == []      # 发现变更，进入防抖窗口
        reloaded = self.system.poll(now=1000.0 + 0.2)  # 窗口结束，触发热更新
        assert reloaded == [str(cfg)]
        assert self.system.config["weapons"]["sword"]["damage"] == 80

    def test_case_02(self, tmp_path):
        """防抖：快速连续修改只触发一次重载"""
        cfg = tmp_path / "balance.json"
        write_config(cfg, {"damage": 1})
        self.system.watch_config(str(cfg), debounce=0.5)
        write_config(cfg, {"damage": 2})
        assert self.system.poll(now=10.0) == []        # 修改落盘，进入防抖窗口
        write_config(cfg, {"damage": 3})
        assert self.system.poll(now=10.2) == []        # 窗口内再次修改，重置计时
        assert self.system.poll(now=10.6) == []        # 距上次修改不足 0.5s
        reloaded = self.system.poll(now=10.8)          # 防抖窗口结束
        assert reloaded == [str(cfg)]
        assert self.system.reload_count == 1
        assert self.system.config["damage"] == 3

    def test_case_03(self, tmp_path):
        """过滤：临时文件和备份文件不触发更新"""
        for name in ("balance.json.tmp", "balance.json.bak", ".balance.json.swp"):
            ignored = tmp_path / name
            write_config(ignored, {"damage": 999})
            assert not self.system.watch_config(str(ignored))
        assert self.system.poll(now=1.0) == []
        assert self.system.reload_count == 0

    # ---- 配置重载 ----

    def test_case_04(self):
        """重载实际生效：新配置替换旧配置"""
        assert self.system.reload_config(new_config={"weapons": {"sword": {"damage": 120}}})
        assert self.system.config["weapons"]["sword"]["damage"] == 120

    def test_case_05(self):
        """重载验证：非法配置被拒绝"""
        before = dict(self.system.config)
        assert not self.system.reload_config(new_config={"weapons": {"sword": {"damage": -10}}})
        assert not self.system.reload_config(new_config="not a dict")
        assert self.system.config == before

    def test_case_06(self, tmp_path):
        """原子性：加载到一半失败，配置保持原样"""
        before = dict(self.system.config)
        broken = tmp_path / "broken.json"
        broken.write_text('{"weapons": {"sword": ', encoding="utf-8")  # 截断的 JSON
        assert not self.system.reload_config(str(broken))
        assert self.system.config == before
        missing = tmp_path / "missing.json"
        assert not self.system.reload_config(str(missing))
        assert self.system.config == before

    # ---- 动态调参 ----

    def test_case_07(self):
        """调参立即生效：游戏里读到新数值"""
        assert self.system.dynamic_tuning("weapons.sword.damage", 75)
        assert self.system.config["weapons"]["sword"]["damage"] == 75

    def test_case_08(self):
        """范围检查：越界和负数伤害被拒绝"""
        self.system.set_param_range("damage", 0, 100)
        assert not self.system.dynamic_tuning("weapons.sword.damage", -5)
        assert not self.system.dynamic_tuning("weapons.sword.damage", 101)
        assert self.system.config["weapons"]["sword"]["damage"] == 50
        assert self.system.dynamic_tuning("weapons.sword.damage", 100)

    def test_case_09(self):
        """变更通知：监听器收到旧值和新值"""
        events = []
        self.system.add_listener(lambda p, old, new: events.append((p, old, new)))
        self.system.dynamic_tuning("weapons.sword.damage", 60)
        assert events == [("weapons.sword.damage", 50, 60)]
        notes = self.system.get_notifications()
        assert len(notes) == 1
        assert notes[0]["param"] == "weapons.sword.damage"
        assert notes[0]["old"] == 50 and notes[0]["new"] == 60

    # ---- 版本管理 ----

    def test_case_10(self):
        """版本历史：每次修改都有记录"""
        v1 = self.system.commit_version("初始平衡")
        self.system.dynamic_tuning("weapons.sword.damage", 70)
        v2 = self.system.commit_version("加强剑")
        history = self.system.get_history()
        assert [h["id"] for h in history] == [v1, v2]
        assert history[0]["description"] == "初始平衡"
        assert history[1]["description"] == "加强剑"

    def test_case_11(self):
        """版本 diff：两个版本之间的差异可见"""
        v1 = self.system.commit_version("v1")
        self.system.dynamic_tuning("weapons.sword.damage", 90)
        v2 = self.system.commit_version("v2")
        diff = self.system.diff_versions(v1, v2)
        assert diff == {"weapons.sword.damage": (50, 90)}
        with pytest.raises(KeyError):
            self.system.diff_versions(v1, 999)

    def test_case_12(self):
        """回滚：改坏了可以恢复到历史版本"""
        v1 = self.system.commit_version("稳定版")
        self.system.dynamic_tuning("weapons.sword.damage", 999)
        assert self.system.rollback(v1)
        assert self.system.config["weapons"]["sword"]["damage"] == 50
        assert not self.system.rollback(999)

    # ---- 平衡性预设 ----

    def test_case_13(self):
        """预设保存与加载"""
        self.system.save_preset("pvp", {"weapons": {"sword": {"damage": 40}}})
        self.system.save_preset("pve", {"weapons": {"sword": {"damage": 60}}})
        assert self.system.list_presets() == ["pve", "pvp"]
        assert self.system.load_preset("pvp")
        assert self.system.config["weapons"]["sword"]["damage"] == 40
        assert self.system.load_preset("pve")
        assert self.system.config["weapons"]["sword"]["damage"] == 60
        assert not self.system.load_preset("不存在的预设")

    def test_case_14(self):
        """预设分享与应用前测试"""
        self.system.save_preset("赛季配置", {"weapons": {"sword": {"damage": 65}}})
        exported = self.system.export_preset("赛季配置")
        assert isinstance(exported, str)
        other_server = ConfigHotReload()
        assert other_server.import_preset(exported)  # 服务器之间共享
        assert "赛季配置" in other_server.list_presets()
        ok, report = other_server.test_preset("赛季配置")
        assert ok
        assert report["changes"] == {"weapons.sword.damage": (None, 65)}
        assert other_server.config == {}  # 试运行不修改线上配置
        ok, report = other_server.test_preset("不存在")
        assert not ok and "error" in report


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
