"""
游戏内配置热更新与动态调参系统 - 核心模块

功能：
- watch_config:       配置文件监听（防抖 + 临时文件过滤）
- reload_config:      配置重载（验证 + 原子性）
- dynamic_tuning:     动态调参（范围检查 + 变更通知）
- version_management: 版本管理（历史 + diff + 回滚）
- balance_preset:     平衡性预设（保存 / 加载 / 分享 / 测试）
"""
import copy
import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class Config:
    pass


class ConfigHotReload:
    # 触发监听时需要忽略的临时 / 备份文件后缀
    TEMP_SUFFIXES = (".tmp", ".bak", ".swp", ".orig", ".backup", "~")
    # 默认不允许为负数的参数名关键字
    NON_NEGATIVE_KEYS = ("damage", "hp", "health", "cooldown", "range", "speed")

    def __init__(self, config: Optional[Dict] = None):
        self.config: Dict = copy.deepcopy(config) if config else {}
        self._state: Dict = {}
        self._history: List = []

        self._watchers: Dict[str, Dict] = {}
        self._param_ranges: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self._validators: List[Callable] = []
        self._listeners: List[Callable] = []
        self._notifications: List[Dict] = []

        self._versions: List[Dict] = []
        self._version_counter = 0
        self._presets: Dict[str, Dict] = {}
        self.reload_count = 0

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _flatten(config: Dict, prefix: str = "") -> Dict:
        flat = {}
        for key, value in config.items():
            full_key = f"{prefix}{key}"
            if isinstance(value, dict):
                flat.update(ConfigHotReload._flatten(value, full_key + "."))
            else:
                flat[full_key] = value
        return flat

    @staticmethod
    def _get_nested(config: Dict, path: str):
        node = config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    @staticmethod
    def _set_nested(config: Dict, path: str, value):
        node = config
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @classmethod
    def _is_ignored_file(cls, path: str) -> bool:
        name = os.path.basename(path)
        if name.startswith(".") or name.startswith("#"):
            return True
        return any(name.endswith(suffix) for suffix in cls.TEMP_SUFFIXES)

    @staticmethod
    def _mtime(path: str) -> Optional[float]:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 1. 配置监听（防抖 + 过滤）
    # ------------------------------------------------------------------
    def watch_config(self, path: str, debounce: float = 0.1,
                     auto_reload: bool = True) -> bool:
        """开始监听配置文件。debounce 秒内的连续修改只触发一次重载。"""
        if self._is_ignored_file(path):
            return False
        self._watchers[path] = {
            "mtime": self._mtime(path),
            "debounce": debounce,
            "auto_reload": auto_reload,
            "pending_since": None,
        }
        return True

    def unwatch_config(self, path: str) -> bool:
        return self._watchers.pop(path, None) is not None

    def poll(self, now: Optional[float] = None) -> List[str]:
        """检查所有监听文件，返回本次实际触发重载的文件列表。"""
        now = time.time() if now is None else now
        reloaded = []
        for path, watcher in self._watchers.items():
            if self._is_ignored_file(path):
                continue
            mtime = self._mtime(path)
            if mtime is None:
                continue
            if mtime != watcher["mtime"]:
                watcher["mtime"] = mtime
                watcher["pending_since"] = now  # 每次修改都重置防抖计时
            if (watcher["pending_since"] is not None
                    and now - watcher["pending_since"] >= watcher["debounce"]):
                watcher["pending_since"] = None
                if watcher["auto_reload"]:
                    if self.reload_config(path):
                        reloaded.append(path)
                else:
                    reloaded.append(path)
        return reloaded

    def update(self, dt: float):
        self.poll()

    # ------------------------------------------------------------------
    # 2. 配置重载（验证 + 原子性）
    # ------------------------------------------------------------------
    def add_validator(self, validator: Callable[[Dict], Tuple[bool, str]]):
        self._validators.append(validator)

    def validate_config(self, config) -> Tuple[bool, List[str]]:
        errors: List[str] = []
        if not isinstance(config, dict):
            return False, ["配置必须是字典"]
        for key, value in self._flatten(config).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            leaf = key.split(".")[-1]
            lo, hi = self._param_ranges.get(leaf, (None, None))
            if lo is not None and value < lo:
                errors.append(f"{key}={value} 低于下限 {lo}")
            if hi is not None and value > hi:
                errors.append(f"{key}={value} 高于上限 {hi}")
            if value < 0 and any(k in leaf.lower() for k in self.NON_NEGATIVE_KEYS):
                errors.append(f"{key}={value} 不允许为负数")
        for validator in self._validators:
            ok, message = validator(config)
            if not ok:
                errors.append(message)
        return (not errors), errors

    def reload_config(self, path: Optional[str] = None,
                      new_config: Optional[Dict] = None) -> bool:
        """原子重载：先完整加载并验证，全部通过后才替换当前配置。"""
        backup = self.config
        try:
            if new_config is None:
                if path is None:
                    return False
                with open(path, "r", encoding="utf-8") as fp:
                    new_config = json.load(fp)
            ok, _errors = self.validate_config(new_config)
            if not ok:
                self.config = backup
                return False
            self.config = copy.deepcopy(new_config)
            self.reload_count += 1
            self._record_version(f"reload:{path}" if path else "reload")
            return True
        except Exception:
            self.config = backup  # 加载中途失败，配置保持原样
            return False

    # ------------------------------------------------------------------
    # 3. 动态调参（范围检查 + 通知）
    # ------------------------------------------------------------------
    def set_param_range(self, name: str, min_value: Optional[float],
                        max_value: Optional[float]):
        self._param_ranges[name] = (min_value, max_value)

    def add_listener(self, listener: Callable[[str, object, object], None]):
        self._listeners.append(listener)

    def get_notifications(self) -> List[Dict]:
        return list(self._notifications)

    def dynamic_tuning(self, param: str, value) -> bool:
        """运行时调整参数，立即生效并通知所有监听者。"""
        leaf = param.split(".")[-1]
        lo, hi = self._param_ranges.get(leaf, self._param_ranges.get(param, (None, None)))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if lo is not None and value < lo:
                return False
            if hi is not None and value > hi:
                return False
            if value < 0 and any(k in leaf.lower() for k in self.NON_NEGATIVE_KEYS):
                return False
        old_value = self._get_nested(self.config, param)
        self._set_nested(self.config, param, value)
        notification = {
            "param": param,
            "old": old_value,
            "new": value,
            "timestamp": time.time(),
        }
        self._notifications.append(notification)
        for listener in self._listeners:
            listener(param, old_value, value)
        return True

    # ------------------------------------------------------------------
    # 4. 版本管理（历史 + diff + 回滚）
    # ------------------------------------------------------------------
    def _record_version(self, description: str = "") -> int:
        self._version_counter += 1
        self._versions.append({
            "id": self._version_counter,
            "config": copy.deepcopy(self.config),
            "description": description,
            "timestamp": time.time(),
        })
        return self._version_counter

    def commit_version(self, description: str = "") -> int:
        return self._record_version(description)

    def get_history(self) -> List[Dict]:
        return [
            {"id": v["id"], "description": v["description"], "timestamp": v["timestamp"]}
            for v in self._versions
        ]

    def _find_version_config(self, version_id: int) -> Optional[Dict]:
        for version in self._versions:
            if version["id"] == version_id:
                return version["config"]
        return None

    def diff_versions(self, version_a: int, version_b: int) -> Dict:
        """返回 {参数路径: (旧值, 新值)}，缺失的一侧为 None。"""
        config_a = self._find_version_config(version_a)
        config_b = self._find_version_config(version_b)
        if config_a is None or config_b is None:
            raise KeyError("版本不存在")
        flat_a = self._flatten(config_a)
        flat_b = self._flatten(config_b)
        diff = {}
        for key in set(flat_a) | set(flat_b):
            old, new = flat_a.get(key), flat_b.get(key)
            if old != new:
                diff[key] = (old, new)
        return diff

    def rollback(self, version_id: int) -> bool:
        config = self._find_version_config(version_id)
        if config is None:
            return False
        self.config = copy.deepcopy(config)
        self._record_version(f"rollback to v{version_id}")
        return True

    # ------------------------------------------------------------------
    # 5. 平衡性预设（保存 / 加载 / 分享 / 测试）
    # ------------------------------------------------------------------
    def save_preset(self, name: str, config: Optional[Dict] = None) -> bool:
        self._presets[name] = copy.deepcopy(config if config is not None else self.config)
        return True

    def load_preset(self, name: str) -> bool:
        if name not in self._presets:
            return False
        return self.reload_config(new_config=copy.deepcopy(self._presets[name]))

    def list_presets(self) -> List[str]:
        return sorted(self._presets)

    def export_preset(self, name: str) -> Optional[str]:
        """导出为 JSON 字符串，用于服务器之间共享。"""
        if name not in self._presets:
            return None
        return json.dumps({"name": name, "config": self._presets[name]},
                          ensure_ascii=False)

    def import_preset(self, data: str) -> bool:
        try:
            payload = json.loads(data)
            name = payload["name"]
            config = payload["config"]
        except (ValueError, KeyError, TypeError):
            return False
        ok, _errors = self.validate_config(config)
        if not ok:
            return False
        self._presets[name] = config
        return True

    def test_preset(self, name: str) -> Tuple[bool, Dict]:
        """试运行：验证预设并给出相对当前配置的差异，不修改线上配置。"""
        if name not in self._presets:
            return False, {"error": f"预设 {name} 不存在"}
        preset = self._presets[name]
        ok, errors = self.validate_config(preset)
        flat_current = self._flatten(self.config)
        flat_preset = self._flatten(preset)
        changes = {}
        for key in set(flat_current) | set(flat_preset):
            old, new = flat_current.get(key), flat_preset.get(key)
            if old != new:
                changes[key] = (old, new)
        return ok, {"errors": errors, "changes": changes}

    # ------------------------------------------------------------------
    def reset(self):
        self._state = {}
        self._history = []
        self._watchers = {}
        self._listeners = []
        self._notifications = []
        self._versions = []
        self._version_counter = 0
        self._presets = {}
        self.reload_count = 0
