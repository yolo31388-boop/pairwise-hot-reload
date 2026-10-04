"""
游戏内配置热更新与动态调参系统 - 验收测试
运行方式：python -m pytest tests/test_hot_reload.py -q
共 14 个测试用例
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.hot_reload import ConfigHotReload


def write_config(path, payload, mtime=None):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestConfigHotReload:
    def setup_method(self):
        self.system = ConfigHotReload()

    # --- 配置监听：实际生效 ---
    def test_case_01(self, tmp_path):
        cfg = tmp_path / "weapon.json"
        write_config(cfg, {"damage": 10}, mtime=1000.0)
        assert self.system.watch_config(str(cfg)) is True

        write_config(cfg, {"damage": 20}, mtime=2000.0)
        self.system.update(0.3)
        self.system.update(0.3)

        assert self.system.config == {"damage": 20}

    # --- 配置监听：防抖（快速连续修改只触发一次，且窗口内不触发） ---
    def test_case_02(self, tmp_path):
        cfg = tmp_path / "weapon.json"
        write_config(cfg, {"damage": 10}, mtime=1000.0)
        self.system.watch_config(str(cfg))

        write_config(cfg, {"damage": 20}, mtime=2000.0)
        self.system.update(0.1)
        reloads = [n for n in self.system.notifications if n["event"] == "config_reloaded"]
        assert reloads == []

        write_config(cfg, {"damage": 30}, mtime=3000.0)
        self.system.update(0.1)
        reloads = [n for n in self.system.notifications if n["event"] == "config_reloaded"]
        assert reloads == []

        self.system.update(0.3)
        reloads = [n for n in self.system.notifications if n["event"] == "config_reloaded"]
        assert len(reloads) == 1
        assert self.system.config == {"damage": 30}

    # --- 配置监听：过滤临时文件和备份文件 ---
    def test_case_03(self, tmp_path):
        for name in ("weapon.bak", "weapon.tmp", "weapon~", ".weapon.json", "weapon.swp"):
            f = tmp_path / name
            write_config(f, {"damage": 99})
            assert self.system.watch_config(str(f)) is False
        assert self.system.watched_files() == []

    # --- 配置重载：改了配置立即生效 ---
    def test_case_04(self, tmp_path):
        cfg = tmp_path / "weapon.json"
        write_config(cfg, {"damage": 10, "speed": 5})
        assert self.system.reload_config(str(cfg)) is True
        assert self.system.config == {"damage": 10, "speed": 5}

    # --- 配置重载：错误配置不通过验证，不会被加载 ---
    def test_case_05(self, tmp_path):
        cfg = tmp_path / "weapon.json"
        write_config(cfg, {"damage": -50})
        assert self.system.reload_config(str(cfg)) is False
        assert self.system.config == {}
        assert any(n["event"] == "reload_failed" for n in self.system.notifications)

    # --- 配置重载：加载失败保持旧配置不变（原子性） ---
    def test_case_06(self, tmp_path):
        good = tmp_path / "good.json"
        bad = tmp_path / "bad.json"
        write_config(good, {"damage": 10})
        self.system.reload_config(str(good))

        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        assert self.system.reload_config(str(bad)) is False
        assert self.system.config == {"damage": 10}

    # --- 动态调参：调了立即生效 ---
    def test_case_07(self):
        system = ConfigHotReload({"damage": 10})
        assert system.dynamic_tuning("damage", 42) is True
        assert system.config["damage"] == 42

    # --- 动态调参：负数伤害被范围检查拒绝 ---
    def test_case_08(self):
        system = ConfigHotReload({"damage": 10})
        assert system.dynamic_tuning("damage", -1) is False
        assert system.config["damage"] == 10
        reject = [n for n in system.notifications if n["event"] == "tuning_rejected"]
        assert len(reject) == 1 and reject[0]["key"] == "damage"

    # --- 动态调参：参数变化有通知 ---
    def test_case_09(self):
        system = ConfigHotReload({"damage": 10})
        received = []
        system.add_listener(lambda msg: received.append(msg))

        system.dynamic_tuning("damage", 30)

        applied = [m for m in received if m["event"] == "tuning_applied"]
        assert len(applied) == 1
        assert applied[0]["key"] == "damage"
        assert applied[0]["old_value"] == 10
        assert applied[0]["value"] == 30

    # --- 版本管理：回滚到历史版本 ---
    def test_case_10(self):
        system = ConfigHotReload({"damage": 10})
        system.dynamic_tuning("damage", 20)
        system.dynamic_tuning("damage", 30)
        assert system.config["damage"] == 30

        assert system.version_management("rollback", 1) is True
        assert system.config == {"damage": 10}

    # --- 版本管理：有历史记录 ---
    def test_case_11(self):
        system = ConfigHotReload({"damage": 10})
        system.dynamic_tuning("damage", 20)
        system.dynamic_tuning("damage", 30)

        history = system.version_management("history")
        assert [h["version"] for h in history] == [1, 2, 3]
        assert all("timestamp" in h and "description" in h for h in history)
        assert "20" in history[1]["description"]

    # --- 版本管理：两个版本之间可看 diff ---
    def test_case_12(self):
        system = ConfigHotReload({"damage": 10})
        system.dynamic_tuning("damage", 20)
        system.dynamic_tuning("speed", 5)

        diff = system.version_management("diff", 1, 3)
        assert diff == {
            "damage": {"old": 10, "new": 20},
            "speed": {"old": None, "new": 5},
        }

    # --- 平衡性预设：保存、加载、跨服务器分享 ---
    def test_case_13(self):
        system = ConfigHotReload({"damage": 10, "speed": 5})
        assert system.balance_preset("save", "balanced_v1") is True

        system.dynamic_tuning("damage", 999)
        assert system.balance_preset("load", "balanced_v1") is True
        assert system.config == {"damage": 10, "speed": 5}

        payload = system.balance_preset("share", "balanced_v1")
        other = ConfigHotReload()
        assert other.balance_preset("import", "balanced_v1", payload) is True
        assert other.balance_preset("load", "balanced_v1") is True
        assert other.config == {"damage": 10, "speed": 5}

    # --- 平衡性预设：应用前可测试（dry-run），不改变当前配置 ---
    def test_case_14(self):
        system = ConfigHotReload({"damage": 10})
        system.balance_preset("save", "test_preset")
        system.dynamic_tuning("damage", 80)

        result = system.balance_preset("test", "test_preset")
        assert result is not None
        assert result["valid"] is True
        assert result["diff"] == {"damage": {"old": 80, "new": 10}}
        assert system.config == {"damage": 80}


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
