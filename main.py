"""
main.py - 插件入口
"""

import asyncio

try:
    from aiocqhttp.exceptions import ApiNotAvailable
except ImportError: 
    ApiNotAvailable = ()

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.command import CommandHandler
from .core.service import ElectricityService
from .core.store import DataStore


class ElectricityPlugin(Star):
    """电费查询插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.config = config
        self.check_hour = self.config.get("CHECK_HOUR", 7)
        self.check_minute = self.config.get("CHECK_MINUTE", 0)
        self.request_timeout = self.config.get("REQUEST_TIMEOUT", 10)
        self.threshold = self.config.get("THRESHOLD", 30.0)

        retry_cfg = self.config.get("SUB_RETRY", {})
        self.sub_retry_times = retry_cfg.get("retry_times", 2)
        self.sub_retry_delay = retry_cfg.get("retry_delay", 2.0)
        self.sub_retry_backoff = retry_cfg.get("retry_backoff", 1.5)

        # 1. 获取持久化数据目录 (返回的是 pathlib.Path 对象)
        plugin_data_dir = StarTools.get_data_dir(self.name)

        # 2. 组装各组件
        self._store = DataStore(
            data_file=plugin_data_dir / "plugin_data.json",
            cache_file=plugin_data_dir / "dongqu_cache.json",
        )

        self._store.load_dongqu_cache()
        self._store.load_plugin_data()

        self._service = ElectricityService(
            store=self._store,
            request_timeout=self.request_timeout,
            threshold=self.threshold,
            check_hour=self.check_hour,
            check_minute=self.check_minute,
            sub_retry_times=self.sub_retry_times,
            sub_retry_delay=self.sub_retry_delay,
            sub_retry_backoff=self.sub_retry_backoff,
        )

        self._commands = CommandHandler(store=self._store, service=self._service)

        # 启动定时任务
        self.timer_task = asyncio.create_task(
            self._service.daily_check_loop(self._send_reminder)
        )

    async def _send_reminder(self, umo: str, message: str) -> bool:
        try:
            remind_success = await self.context.send_message(
                umo,
                MessageChain().message(message),
            )
        except ApiNotAvailable:
            logger.warning(
                "消息平台未连接，发送失败: %s", umo
            )
            return False
        except Exception:
            logger.exception("向会话 %s 发送提醒失败", umo)
            return False
        if not remind_success:
            logger.warning(
                "找不到匹配的平台，提醒未送达: %s", umo
            )
        return remind_success

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("suball")
    async def command_suball(self, event: AstrMessageEvent):
        """列出所有会话的订阅"""
        async for result in self._commands.handle_suball(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("billstat")
    async def command_billstat(self, event: AstrMessageEvent):
        """列出所有房间的查询信息"""
        async for result in self._commands.handle_billstat(event):
            yield result

    @filter.command("bill")
    async def command_bill(self, event: AstrMessageEvent):
        """查询 订阅 黑名单"""
        async for result in self._commands.handle_bill(event):
            yield result

    @filter.command("b")
    async def command_b(self, event: AstrMessageEvent):
        """快速查询当前用户上次调用的宿舍"""
        async for result in self._commands.handle_b(event):
            yield result

    async def terminate(self):
        """插件卸载时的清理工作"""
        if hasattr(self, "timer_task"):
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
        logger.info("电费查询插件已卸载，定时任务已终止。")
