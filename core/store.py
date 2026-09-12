"""
store.py - 数据持久化
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from astrbot.api import logger


class DataStore:
    def __init__(self, data_file: Path, cache_file: Path):
        self.data_file = data_file
        self.cache_file = cache_file
        self.lock = asyncio.Lock()

        self.dongqu_cache: Dict[str, str] = {}  # 东区 nodeId
        self.subs: Dict[str, list[Dict[str, Any]]] = {}  # {umo: [{"building", "room"}]}
        self.blacklist: List[str] = []  # [umo, ...]
        self.last_queries: Dict[
            str, Dict[str, Any]
        ] = {}  # {user_id: {"building": x, "room": y}}
        self.room_queries_info: Dict[
            str, Dict[str, Any]
        ] = {}  # {room_key: {"count": int, "last_query_time": str, "last_query_user": str, "last_query_umo": str, "last_query_balance": float}}

    def load_dongqu_cache(self):
        """加载东区缓存"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.dongqu_cache = json.load(f)
                logger.info(
                    "成功加载东区缓存表，共 %d 个房间。",
                    len(self.dongqu_cache),
                )
            except Exception:
                logger.exception("加载东区缓存表失败")
        else:
            logger.error(
                "东区缓存文件 %s 不存在，东区查询功能可能受限。",
                self.cache_file,
            )

    def load_plugin_data(self):
        """加载订阅、黑名单和用户查询记录"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.subs = data.get("subs", {})
                    self.blacklist = data.get("blacklist", [])
                    self.last_queries = data.get("last_queries", {})
                    self.room_queries_info = data.get("room_queries_info", {})
                logger.info(
                    "成功加载插件数据：%d 个订阅，%d 个黑名单，%d 条查询记录，%d 条房间查询信息。",
                    len(self.subs),
                    len(self.blacklist),
                    len(self.last_queries),
                    len(self.room_queries_info),
                )
            except Exception:
                logger.exception("加载插件数据失败")

                self.subs = {}
                self.blacklist = []
                self.last_queries = {}
                self.room_queries_info = {}

    def get_snapshot(self) -> Dict[str, Any]:
        """必须在持有 self.lock 时调用"""
        return {
            "subs": dict(self.subs),
            "blacklist": list(self.blacklist),
            "last_queries": dict(self.last_queries),
            "room_queries_info": dict(self.room_queries_info),
        }

    async def save_snapshot(self, snapshot: Dict[str, Any]) -> bool:
        """异步保存快照到文件（不持锁）"""
        try:
            await asyncio.to_thread(self._write_data_to_file, snapshot)
            return True
        except Exception:
            logger.exception("保存插件数据失败")
            return False

    def _write_data_to_file(self, data: Dict[str, Any]):
        """同步写入到文件"""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)

        tmp_file = self.data_file.with_suffix(self.data_file.suffix + ".tmp")

        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_file, self.data_file)
            logger.info(
                "插件数据已成功保存到 %s",
                self.data_file,
            )
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

    def update_room_query_info(
        self,
        building: int,
        room_str: str,
        user_id: str,
        umo: str,
        balance: float,
    ):
        """更新房间查询信息"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        room_key = f"{building}-{room_str}"

        record = self.room_queries_info.setdefault(
            room_key,
            {
                "count": 0,
                "last_query_time": "",
                "last_query_user": "",
                "last_query_umo": "",
                "last_query_balance": 0.0,
            },
        )

        record["count"] += 1
        record["last_query_time"] = now
        record["last_query_user"] = user_id
        record["last_query_umo"] = umo
        record["last_query_balance"] = balance

    async def append_history_cache(
        self,
        building: int,
        room_str: str,
        msg: str,
    ) -> str:
        """追加历史查询信息"""
        room_key = f"{building}-{room_str}"

        async with self.lock:
            history = self.room_queries_info.get(room_key)

        if not history:
            return msg

        last_time = history.get("last_query_time")
        if not last_time:
            return msg

        last_balance = history.get("last_query_balance", "未知")

        return (
            msg
            + "\n\n📋 [历史缓存] 该宿舍上一次成功查询信息："
            + f"\n⏰ 查询时间：{last_time}"
            + f"\n⚡️ 历史电费：{last_balance} 度"
        )
