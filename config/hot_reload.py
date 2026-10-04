"""
游戏内配置热更新与动态调参系统 - 核心模块

功能：
- watch_config():     配置文件监听（防抖 + 临时/备份文件过滤）
- reload_config():    配置重载（验证 + 原子性）
- dynamic_tuning():   动态调参（范围检查 + 通知）
- version_management(): 版本管理（历史 + diff + 回滚）
- balance_preset():   平衡性预设（保存/加载/分享/测试）
"""
import copy
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


# 临时文件 / 备份文件后缀，监听时直接过滤
IGNORED_SUFFIXES = (".tmp", ".bak", ".swp", ".swo", ".swx", "~", ".orig", ".rej", ".part")

# 数值型参数的默认合法范围（key 命中任一关键词时生效）
NON_NEGATIVE_KEYWORDS = (
    "damage", "health", "hp", "speed", "cooldown", "rate",
    "ammo", "count", "range", "armor", "shield", "mana",
)


def _is_ignored_file(path: str) -> bool:
    """判断是否为临时文件 / 备份文件 / 隐藏文件。"""
    name = os.path.basename(path)
    if not name:
        return True
    if name.startswith(".") or name.startswith("#"):
        return True
    lowered = name.lower()
    return lowered.endswith(IGNORED_SUFFIXES)


def _flatten(config: Dict, prefix: str = "") -> Dict[str, object]:
    """把嵌套配置拍平成 dot-path -> value，用于 diff 与校验。"""
    flat = {}
    for key, value in config.items():
        path = "{}.{}".format(prefix, key) if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _set_by_path(config: Dict, dotted_key: str, value) -> None:
    parts = dotted_key.split(".")
    node = config
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _get_by_path(config: Dict, dotted_key: str, default=None):
    node = config
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


class ConfigHotReload:
    def __init__(self, config: Optional[Dict] = None,
                 ranges: Optional[Dict[str, Tuple[float, float]]] = None,
                 debounce_interval: float = 0.2):
        self.config: Dict = copy.deepcopy(config) if config else {}
        self._state = {}
        self._history: List[Dict] = []
        self._version = 0
        # 显式范围表：dot-path -> (min, max)，优先级高于关键词推断
        self._ranges: Dict[str, Tuple[float, float]] = dict(ranges) if ranges else {}
        self._debounce_interval = float(debounce_interval)
        self._watched: Dict[str, float] = {}     # path -> 最近一次已处理的 mtime
        self._pending: Dict[str, float] = {}     # path -> 最近一次变更发生的内部时钟时刻
        self._listeners: List[Callable] = []
        self.notifications: List[Dict] = []
        self._presets: Dict[str, Dict] = {}
        self._clock = 0.0
        if self.config:
            self._record_version("初始配置")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _record_version(self, description: str) -> int:
        self._version += 1
        self._history.append({
            "version": self._version,
            "timestamp": time.time(),
            "description": description,
            "config": copy.deepcopy(self.config),
        })
        return self._version

    def _notify(self, event: str, **payload) -> None:
        message = {"event": event, "time": time.time()}
        message.update(payload)
        self.notifications.append(message)
        for listener in list(self._listeners):
            listener(message)

    def add_listener(self, listener: Callable) -> None:
        """注册变更通知回调，回调签名为 listener(message: dict)。"""
        self._listeners.append(listener)

    def _check_value(self, dotted_key: str, value) -> Optional[str]:
        """校验单个数值，返回错误信息；合法时返回 None。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return "参数 {} 的值不是有限数: {!r}".format(dotted_key, value)
            if dotted_key in self._ranges:
                low, high = self._ranges[dotted_key]
                if value < low or value > high:
                    return "参数 {} 的值 {} 超出范围 [{}, {}]".format(dotted_key, value, low, high)
            else:
                leaf = dotted_key.split(".")[-1].lower()
                if any(word in leaf for word in NON_NEGATIVE_KEYWORDS) and value < 0:
                    return "参数 {} 不允许为负数: {}".format(dotted_key, value)
        return None

    def validate_config(self, config) -> Tuple[bool, List[str]]:
        """校验整份配置，返回 (是否合法, 错误列表)。"""
        errors: List[str] = []
        if not isinstance(config, dict):
            return False, ["配置必须是 dict，实际是 {}".format(type(config).__name__)]
        for key, value in _flatten(config).items():
            err = self._check_value(key, value)
            if err:
                errors.append(err)
        return not errors, errors

    # ------------------------------------------------------------------
    # 配置监听：防抖 + 过滤
    # ------------------------------------------------------------------
    def watch_config(self, path: str) -> bool:
        """注册一个配置文件进行监听。临时/备份文件会被过滤，返回 False。"""
        if _is_ignored_file(path):
            return False
        if not os.path.isfile(path):
            return False
        self._watched[path] = os.path.getmtime(path)
        return True

    def watched_files(self) -> List[str]:
        return sorted(self._watched)

    def update(self, dt: float):
        """每帧调用：轮询被监听文件，变更经过防抖窗口后触发重载。"""
        self._clock += dt
        for path in list(self._watched):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime != self._watched[path]:
                # 记录变更时刻（连续修改会不断刷新，实现防抖）
                self._pending[path] = self._clock
                self._watched[path] = mtime
        for path in list(self._pending):
            if self._clock - self._pending[path] >= self._debounce_interval:
                del self._pending[path]
                self.reload_config(path)

    # ------------------------------------------------------------------
    # 配置重载：验证 + 原子性
    # ------------------------------------------------------------------
    def reload_config(self, source) -> bool:
        """
        重载配置。source 可以是配置文件路径（JSON）或 dict。
        先完整加载并验证，全部通过后才原子替换；任何失败都保持旧配置不变。
        """
        if isinstance(source, dict):
            new_config = copy.deepcopy(source)
            origin = "memory"
        else:
            origin = str(source)
            try:
                with open(source, "r", encoding="utf-8") as fh:
                    new_config = json.load(fh)
            except (OSError, ValueError) as exc:
                self._notify("reload_failed", source=origin, reason="读取/解析失败: {}".format(exc))
                return False

        ok, errors = self.validate_config(new_config)
        if not ok:
            self._notify("reload_failed", source=origin, reason="; ".join(errors))
            return False

        # 验证全部通过后才替换，保证原子性
        old_config = self.config
        self.config = new_config
        version = self._record_version("重载配置({})".format(origin))
        self._notify("config_reloaded", source=origin, version=version,
                     changes=self.diff_configs(old_config, new_config))
        return True

    # ------------------------------------------------------------------
    # 动态调参：范围检查 + 通知
    # ------------------------------------------------------------------
    def dynamic_tuning(self, key: str, value) -> bool:
        """运行时调整单个参数（支持 dot-path）。非法值会被拒绝并通知。"""
        err = self._check_value(key, value)
        if err:
            self._notify("tuning_rejected", key=key, value=value, reason=err)
            return False
        old_value = _get_by_path(self.config, key)
        _set_by_path(self.config, key, value)
        version = self._record_version("调参 {} = {!r}".format(key, value))
        self._notify("tuning_applied", key=key, old_value=old_value,
                     value=value, version=version)
        return True

    # ------------------------------------------------------------------
    # 版本管理：历史 + diff + 回滚
    # ------------------------------------------------------------------
    @staticmethod
    def diff_configs(old: Dict, new: Dict) -> Dict[str, Dict]:
        """比较两份配置，返回 {dot-path: {"old":..., "new":...}}。"""
        old_flat = _flatten(old) if isinstance(old, dict) else {}
        new_flat = _flatten(new) if isinstance(new, dict) else {}
        diff = {}
        for key in sorted(set(old_flat) | set(new_flat)):
            in_old = key in old_flat
            in_new = key in new_flat
            if in_old and in_new and old_flat[key] == new_flat[key]:
                continue
            diff[key] = {
                "old": old_flat.get(key) if in_old else None,
                "new": new_flat.get(key) if in_new else None,
            }
        return diff

    def version_management(self, action: str, *args):
        """
        版本管理入口：
        - ("history",)            -> 版本历史列表（不含完整配置快照）
        - ("diff", v1, v2)        -> 两个版本之间的差异
        - ("rollback", version)   -> 回滚到指定版本，返回 bool
        """
        if action == "history":
            return [
                {"version": item["version"],
                 "timestamp": item["timestamp"],
                 "description": item["description"]}
                for item in self._history
            ]

        if action == "diff":
            v1, v2 = args
            c1 = self._find_version(v1)
            c2 = self._find_version(v2)
            if c1 is None or c2 is None:
                return None
            return self.diff_configs(c1, c2)

        if action == "rollback":
            (target,) = args
            snapshot = self._find_version(target)
            if snapshot is None:
                self._notify("rollback_failed", version=target, reason="版本不存在")
                return False
            old_config = self.config
            self.config = copy.deepcopy(snapshot)
            version = self._record_version("回滚到版本 {}".format(target))
            self._notify("rolled_back", target=target, version=version,
                         changes=self.diff_configs(old_config, self.config))
            return True

        raise ValueError("未知的版本管理操作: {!r}".format(action))

    def _find_version(self, version: int) -> Optional[Dict]:
        for item in self._history:
            if item["version"] == version:
                return item["config"]
        return None

    # ------------------------------------------------------------------
    # 平衡性预设：保存 / 加载 / 分享 / 测试
    # ------------------------------------------------------------------
    def balance_preset(self, action: str, *args):
        """
        平衡性预设入口：
        - ("save", name)            -> 把当前配置保存为预设
        - ("load", name)            -> 应用预设（带验证与原子性），返回 bool
        - ("list",)                 -> 预设名列表
        - ("share", name)           -> 导出为 JSON 字符串，便于跨服务器分享
        - ("import", name, payload) -> 从 JSON 字符串导入预设
        - ("test", name)            -> 试运行：返回与当前配置的 diff，不实际应用
        """
        if action == "save":
            (name,) = args
            self._presets[name] = copy.deepcopy(self.config)
            self._notify("preset_saved", name=name)
            return True

        if action == "load":
            (name,) = args
            preset = self._presets.get(name)
            if preset is None:
                self._notify("preset_load_failed", name=name, reason="预设不存在")
                return False
            ok, errors = self.validate_config(preset)
            if not ok:
                self._notify("preset_load_failed", name=name, reason="; ".join(errors))
                return False
            old_config = self.config
            self.config = copy.deepcopy(preset)
            version = self._record_version("应用预设 {}".format(name))
            self._notify("preset_loaded", name=name, version=version,
                         changes=self.diff_configs(old_config, self.config))
            return True

        if action == "list":
            return sorted(self._presets)

        if action == "share":
            (name,) = args
            preset = self._presets.get(name)
            if preset is None:
                return None
            return json.dumps({"name": name, "config": preset},
                              ensure_ascii=False, sort_keys=True)

        if action == "import":
            name, payload = args
            try:
                data = json.loads(payload)
                config = data["config"]
            except (TypeError, ValueError, KeyError) as exc:
                self._notify("preset_import_failed", name=name,
                             reason="解析失败: {}".format(exc))
                return False
            ok, errors = self.validate_config(config)
            if not ok:
                self._notify("preset_import_failed", name=name,
                             reason="; ".join(errors))
                return False
            self._presets[name] = copy.deepcopy(config)
            self._notify("preset_imported", name=name)
            return True

        if action == "test":
            (name,) = args
            preset = self._presets.get(name)
            if preset is None:
                return None
            ok, errors = self.validate_config(preset)
            return {
                "valid": ok,
                "errors": errors,
                "diff": self.diff_configs(self.config, preset),
            }

        raise ValueError("未知的预设操作: {!r}".format(action))

    # ------------------------------------------------------------------
    def reset(self):
        self._state = {}
        self._history = []
        self._version = 0
        self._pending = {}
        self.notifications = []
