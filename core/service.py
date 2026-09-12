"""
service.py -
"""

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional, Tuple

import httpx
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .api import SubscriptionQueryExecutor, query_balance_once
from .resolver import resolve_room_info
from .store import DataStore

SendMessage = Callable[[str, str], Awaitable[None]]


class ElectricityService:
    def __init__(
        self,
        store: DataStore,
        *,
        request_timeout: int = 10,
        threshold: float = 30.0,
        check_hour: int = 7,
        check_minute: int = 0,
        sub_retry_times: int = 2,
        sub_retry_delay: float = 2.0,
        sub_retry_backoff: float = 1.5,
    ):
        self._store = store
        self.request_timeout = request_timeout
        self.threshold = threshold
        self.check_hour = check_hour
        self.check_minute = check_minute
        self.sub_retry_times = sub_retry_times
        self.sub_retry_delay = sub_retry_delay
        self.sub_retry_backoff = sub_retry_backoff

    async def fetch_balance(
        self,
        building: int,
        room_str: str,
        executor: Optional[SubscriptionQueryExecutor] = None,
    ) -> Tuple[bool, str, float]:
        """核心查询方法，返回 (是否成功, 提示/错误信息, 剩余电量)"""
        try:
            item_num, node_id, display_name = resolve_room_info(
                self._store.dongqu_cache, building, room_str
            )

            if executor is None:
                result = await query_balance_once(
                    item_num, node_id, timeout=self.request_timeout
                )
            else:
                result = await executor.query(item_num, node_id)

            if result.get("code") == "1":
                used_amp_str = result.get("data", {}).get("usedAmp", "0")
                used_amp = float(used_amp_str)
                return True, f"⚡️ {display_name} 剩余电量：{used_amp_str} 度", used_amp
            else:
                msg = result.get("msg", "未知错误")
                return False, f"❌ {display_name} 查询失败：{msg}", 0.0

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            return False, "⏰ 请求超时或网络连接失败，请稍后重试。", 0.0
        except httpx.HTTPStatusError as e:
            return (
                False,
                f"🔌 服务器异常 (HTTP {e.response.status_code})，请稍后重试。",
                0.0,
            )
        except ValueError as e:
            return False, f"❌ {str(e)}", 0.0
        except Exception:
            logger.exception("查询未知错误")
            return False, "💥 内部错误，请联系管理员查看日志。", 0.0

    async def perform_daily_checks(self, send_message: SendMessage):
        """订阅检查与推送逻辑"""
        logger.info("开始执行电费定时订阅检查...")

        query_cache = {}

        async with self._store.lock:
            subs_copy = {umo: rooms.copy() for umo, rooms in self._store.subs.items()}

        # 用 context manager 包裹批量查询
        async with SubscriptionQueryExecutor(
            timeout=self.request_timeout,
            retry_times=self.sub_retry_times,
            retry_delay=self.sub_retry_delay,
            retry_backoff=self.sub_retry_backoff,
        ) as executor:
            for umo, rooms in subs_copy.items():
                if not rooms:
                    continue

                low_balance_msgs = []
                failed_msgs = []

                for room_info in rooms:
                    building = room_info.get("building")
                    room = room_info.get("room")
                    cache_key = (building, room)

                    if not building or not room:
                        logger.warning(
                            "会话 %s 的订阅信息不完整，跳过。",
                            umo,
                        )
                        continue

                    if cache_key not in query_cache:
                        (success, msg, balance) = await self.fetch_balance(
                            building, room, executor=executor
                        )

                        await asyncio.sleep(5)

                        if not success:  # 如果查询失败，记录错误信息
                            failed_msgs.append(f"{building}栋{room}室")
                            logger.warning(
                                "查询 %s栋%s室 失败: %s, 跳过处理。",
                                building,
                                room,
                                msg,
                            )
                            continue

                        query_cache[cache_key] = (success, msg, balance)
                    else:
                        success, msg, balance = query_cache[cache_key]

                    logger.info(
                        "从缓存中查询到 %s",
                        msg,
                    )

                    if success and balance < self.threshold:
                        low_balance_msgs.append(msg)

                notifications = []

                if low_balance_msgs:
                    notifications.append(
                        "【电费不足提醒】\n"
                        + "\n".join(low_balance_msgs)
                        + "\n请及时充值以免断电！"
                    )

                if failed_msgs:
                    notifications.append(
                        f"【查询异常提醒】\n本次查询中有 {len(failed_msgs)} 个宿舍查询失败：\n"
                        + "\n".join(failed_msgs)
                    )

                if notifications:
                    combined_msg = "\n\n".join(notifications)
                    await send_message(umo, combined_msg)

    async def daily_check_loop(self, send_message: SendMessage):
        """定时循环"""
        try:
            while True:
                now = datetime.now()
                target = now.replace(
                    hour=self.check_hour,
                    minute=self.check_minute,
                    second=0,
                    microsecond=0,
                )

                if now >= target:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()

                await asyncio.sleep(wait_seconds)

                try:
                    await self.perform_daily_checks(send_message)
                except Exception:
                    logger.exception("执行每日检查时发生错误，将在下次继续尝试。")
        except asyncio.CancelledError:
            logger.info("定时检查任务已取消。")
            raise
