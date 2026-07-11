# main.py
import os
import hashlib
import time
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, List

import httpx
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig

# ----------------------------- 配置 -----------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
        SECRET_KEY = config.get("SECRET_KEY", "")
        GETBALANCE_URL = config.get("GETBALANCE_URL", "")
        DONGQU_ITEM_NUM = config.get("DONGQU_ITEM_NUM", "34")
        ZONE_CONFIGS = config.get("ZONE_RANGES", [])  # 列表
        SPECIAL_BUILDINGS_RAW = config.get("SPECIAL_BUILDINGS", {})
        NEW_NORTH_SUFFIX_RAW = config.get("NEW_NORTH_SUFFIX_MAP", {})
else:
    logger.error(
        "配置文件 %s 不存在",
        CONFIG_FILE,
    )
    raise ValueError("配置文件缺失，请确保 config.json 存在于插件目录下")

# 转换键为整数，便于后续使用
SPECIAL_BUILDINGS = {int(k): v for k, v in SPECIAL_BUILDINGS_RAW.items()}
NEW_NORTH_SUFFIX_MAP = {int(k): v for k, v in NEW_NORTH_SUFFIX_RAW.items()}
# ----------------------------------------------------------------


def generate_sign(params: Dict[str, Any], secret_key: str) -> str:
    """生成签名（与原网页保持一致）"""
    cleaned = {}
    for k, v in params.items():
        if v is None or v == "" or (isinstance(v, list) and len(v) == 0):
            continue
        cleaned[k] = v

    sorted_keys = sorted(cleaned.keys())
    raw_parts = []
    for k in sorted_keys:
        v = cleaned[k]
        if isinstance(v, dict):
            raw_parts.append(json.dumps(v, separators=(",", ":")), ensure_ascii=False)
        else:
            raw_parts.append(str(v))
    raw_str = "|".join(raw_parts) + "|" + secret_key
    return hashlib.md5(raw_str.encode("utf-8")).hexdigest()


async def request_balance(
    client: httpx.AsyncClient, item_num: str, node_id: str
) -> Dict[str, Any]:
    """发送查询请求"""
    current_time = time.strftime("%Y%m%d%H%M%S")
    params = {
        "itemNum": item_num,
        "nodeId": node_id,
        "time": current_time,
    }
    sign = generate_sign(params, SECRET_KEY)
    payload = {**params, "sign": sign}

    resp = await client.post(GETBALANCE_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


async def query_balance_once(
    item_num: str, node_id: str, timeout: int = 10
) -> Dict[str, Any]:
    """普通单次请求"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await request_balance(client, item_num, node_id)


class SubscriptionQueryExecutor:
    """订阅查询执行器"""

    def __init__(
        self, timeout: int, retry_times: int, retry_delay: float, retry_backoff: float
    ):
        self.timeout = timeout
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self.retry_backoff = retry_backoff
        self.client: httpx.AsyncClient = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()

    async def query(self, item_num: str, node_id: str) -> Dict[str, Any]:
        delay = self.retry_delay

        for attempt in range(1, self.retry_times + 1):
            try:
                return await request_balance(self.client, item_num, node_id)
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as e:
                if attempt == self.retry_times:
                    logger.error(
                        "订阅查询最终失败 [%s]: 已达到最大重试次数 (%d)。",
                        node_id,
                        self.retry_times,
                    )
                    raise

                logger.warning(
                    "订阅查询失败 [%s] (%d/%d): %s, %.1f 秒后重试。",
                    node_id,
                    attempt,
                    self.retry_times,
                    str(e),
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= self.retry_backoff


def get_zone_info(building: int) -> Tuple[str, str, str]:
    # 优先检查特殊楼栋
    if building in SPECIAL_BUILDINGS:
        info = SPECIAL_BUILDINGS[building]
        return info["zone"], info["item_num"], info["rule"]

    # 遍历普通区域配置
    for cfg in ZONE_CONFIGS:
        if cfg["start"] <= building < cfg["end"]:
            return cfg["zone"], cfg["item_num"], "normal"

    raise ValueError(f"楼栋号 {building} 不存在")


def parse_room_normal(building: int, room_str: str, rule_type: str) -> Dict[str, Any]:
    if rule_type == "normal":
        if not room_str.isdigit() or len(room_str) != 3:
            raise ValueError("宿舍号必须是三位数字")
        return {"floor": room_str[0], "room_full": room_str, "building_suffix": None}
    elif rule_type == "north_south":
        if not room_str.isdigit() or len(room_str) != 3:
            raise ValueError("宿舍号必须是三位数字")
        suffix = "南" if (int(room_str[-1]) % 2 == 1) else "北"
        return {"floor": room_str[0], "room_full": room_str, "building_suffix": suffix}
    elif rule_type == "north_mid_south":
        if not room_str.isdigit() or len(room_str) != 4:
            raise ValueError(f"新北 {building} 栋宿舍号必须是四位数字")
        first_digit = int(room_str[0])
        if first_digit not in NEW_NORTH_SUFFIX_MAP:
            raise ValueError("宿舍号第一位必须是1、2、3（对应南/中/北）")
        suffix = NEW_NORTH_SUFFIX_MAP[first_digit]
        floor = room_str[1]
        room_full = floor + room_str[2:]
        return {"floor": floor, "room_full": room_full, "building_suffix": suffix}
    else:
        raise ValueError(f"未知的规则类型：{rule_type}")


class ElectricityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self._data_lock = asyncio.Lock()

        self.config = config
        self.check_hour = self.config.get("CHECK_HOUR", 7)
        self.check_minute = self.config.get("CHECK_MINUTE", 0)
        self.request_timeout = self.config.get("REQUEST_TIMEOUT", 10)
        self.threshold = self.config.get("THRESHOLD", 30.0)

        retry_cfg = self.config.get("SUB_RETRY", {})
        self.sub_retry_times = retry_cfg.get("retry_times", 2)
        self.sub_retry_delay = retry_cfg.get("retry_delay", 2.0)
        self.sub_retry_backoff = retry_cfg.get("retry_backoff", 1.5)

        # 1. 使用官方 API 获取持久化数据目录 (返回的是 pathlib.Path 对象)
        plugin_data_dir = StarTools.get_data_dir(self.name)

        # 2. 绑定具体的数据文件路径
        self.data_file = plugin_data_dir / "plugin_data.json"
        self.cache_file = plugin_data_dir / "dongqu_cache.json"

        self.dongqu_cache = {}
        self.subs = {}  # 订阅信息: {umo: [{"building": x, "room": y}]}
        self.blacklist = []  # 黑名单列表: [umo_1, umo_2...]
        self.last_queries = {}  # 持久化用户查询缓存: {user_id: {"building": x, "room": y}}
        self.room_queries_info = {}  # 宿舍查询统计: {room_key: {"count": int, "last_query_time": str, "last_query_user": str, "last_query_umo": str}}

        self.load_dongqu_cache()
        self.load_plugin_data()

        # 启动定时任务
        self.timer_task = asyncio.create_task(self.daily_check_loop())

    def load_dongqu_cache(self):
        # 替换为 self.cache_file
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

    def _get_data_snapshot(self) -> Dict[str, Any]:
        """必须在持有 _data_lock 时调用"""
        return {
            "subs": dict(self.subs),
            "blacklist": list(self.blacklist),
            "last_queries": dict(self.last_queries),
            "room_queries_info": dict(self.room_queries_info),
        }

    async def _save_snapshot(self, snapshot: Dict[str, Any]) -> bool:
        """异步保存快照到文件（不持锁）"""
        try:
            await asyncio.to_thread(self._write_data_to_file, snapshot)
            return True
        except Exception:
            logger.exception("保存插件数据失败")
            return False

    def load_plugin_data(self):
        """加载订阅、黑名单和用户查询记录"""
        # 替换为 self.data_file
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

    def resolve_dorm_info(self, building: int, room_str: str) -> Tuple[str, str, str]:
        # 1. 东区逻辑 (1-6栋)
        if 1 <= building <= 6:
            if not self.dongqu_cache:
                raise ValueError(
                    "东区缓存未加载，请检查插件数据目录下的 dongqu_cache.json 文件"
                )

            if not room_str.isdigit() or len(room_str) != 3:
                raise ValueError("东区宿舍号必须是三位数字")

            cache_key = f"{building}-{room_str}"
            if cache_key not in self.dongqu_cache:
                raise ValueError(f"楼栋号 {building} 不存在")

            node_id = self.dongqu_cache[cache_key]
            return DONGQU_ITEM_NUM, node_id, f"{building}栋{room_str}室"

        # 2. 非东区（普通 + 新北）逻辑
        zone_name, item_num, rule_type = get_zone_info(building)
        room_info = parse_room_normal(building, room_str, rule_type)

        # 构造普通区 nodeId
        suffix = room_info.get("building_suffix")
        if suffix:
            node_id = f"{zone_name},{building}栋{suffix},{room_info['floor']}层,{room_info['room_full']}"
            display_name = f"{building}栋{suffix}{room_info['room_full']}室"
        else:
            node_id = f"{zone_name},{building}栋,{room_info['floor']}层,{room_info['room_full']}"
            display_name = f"{building}栋{room_str}室"

        return item_num, node_id, display_name

    async def fetch_balance(
        self, building: int, room_str: str, executor: Any = None
    ) -> Tuple[bool, str, float]:
        """核心查询方法，返回 (是否成功, 提示/错误信息, 剩余电量)"""
        try:
            item_num, node_id, display_name = self.resolve_dorm_info(building, room_str)

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

    async def _perform_daily_checks(self):
        """订阅检查与推送逻辑"""
        logger.info("开始执行电费定时订阅检查...")

        query_cache = {}

        async with self._data_lock:
            subs_copy = {umo: rooms.copy() for umo, rooms in self.subs.items()}

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

                        await asyncio.sleep(2)

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
                    try:
                        await self.context.send_message(
                            umo,
                            MessageChain().message(combined_msg),
                        )
                    except Exception:
                        logger.exception(
                            "向会话 %s 发送提醒失败",
                            umo,
                        )

    async def daily_check_loop(self):
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
                    await self._perform_daily_checks()
                except Exception:
                    logger.exception("执行每日检查时发生错误，将在下次继续尝试。")
        except asyncio.CancelledError:
            logger.info("定时检查任务已取消。")
            raise

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("suball")
    async def test(self, event: AstrMessageEvent):
        """列出所有会话的订阅"""
        async with self._data_lock:
            subs_copy = {umo: rooms.copy() for umo, rooms in self.subs.items()}

        if not subs_copy:
            yield event.plain_result("ℹ️ 当前没有任何订阅。")
            return

        msg_lines = ["📋 当前所有会话的订阅列表:"]
        for umo, rooms in subs_copy.items():
            msg_lines.append(f"{umo}:")
            for r in rooms:
                msg_lines.append(f"- {r['building']}栋{r['room']}室")

        yield event.plain_result("\n".join(msg_lines))
        return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("billstat")
    async def command_billstat(self, event: AstrMessageEvent):
        """列出所有房间的查询信息"""
        async with self._data_lock:
            room_info_copy = dict(self.room_queries_info)

        if not room_info_copy:
            yield event.plain_result("ℹ️ 当前没有任何房间查询记录。")
            return

        msg_lines = ["📋 当前所有房间的查询信息:"]
        for room_key, info in room_info_copy.items():
            msg_lines.append(
                f"{room_key}: 查询次数 {info['count']}, "
                f"\n最后查询时间 {info['last_query_time']}, "
                f"\n最后查询用户 {info['last_query_user']}, "
                f"\n最后查询会话 {info['last_query_umo']}"
            )

        yield event.plain_result("\n".join(msg_lines))
        return

    @filter.command("bill")
    async def command_bill(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin

        # ---------------- 1. 会话黑名单检查 ----------------
        message_text = event.message_str.strip()
        parts = message_text.split()

        # 拦截开启/关闭指令 (这部分不受黑名单约束)
        if len(parts) == 2:
            action = parts[1].lower()
            if action == "off":
                async with self._data_lock:
                    if umo not in self.blacklist:
                        self.blacklist.append(umo)
                        snapshot = self._get_data_snapshot()
                    else:
                        snapshot = None
                if snapshot:
                    await self._save_snapshot(snapshot)
                yield event.plain_result("🚫 已在此会话禁用电费查询指令。")
                return
            elif action == "on":
                async with self._data_lock:
                    if umo in self.blacklist:
                        self.blacklist.remove(umo)
                        snapshot = self._get_data_snapshot()
                    else:
                        snapshot = None
                if snapshot:
                    await self._save_snapshot(snapshot)
                yield event.plain_result("✅ 已在此会话启用电费查询指令。")
                return

        # 如果当前会话在黑名单中，静默退出
        async with self._data_lock:
            blocked = umo in self.blacklist
        if blocked:
            yield event.plain_result("⚠️ 当前会话已禁用电费查询指令，请联系管理员启用。")
            return

        # ---------------- 2. 指令解析与路由 ----------------
        if len(parts) == 1:
            help_msg = (
                "🔌 电费查询插件使用帮助:\n"
                "• /bill <楼栋> <宿舍> - 查询电费\n"
                "• /b - 快速查询上次查找的宿舍\n"
                "• /bill sub <楼栋> <宿舍> - 订阅每日低电量提醒\n"
                "• /bill unsub <楼栋> <宿舍> - 取消订阅\n"
                "• /bill on/off - 启用/禁用当前群组响应"
            )
            yield event.plain_result(help_msg)
            return

        action_or_building = parts[1]

        # 处理订阅指令
        if action_or_building.lower() == "sub":
            if len(parts) < 4:
                yield event.plain_result(
                    "❌ 参数不足！请使用格式：/bill sub 楼栋号 宿舍号"
                )
                return
            building_str, room_str = parts[2], parts[3]
            if not building_str.isdigit():
                yield event.plain_result("❌ 楼栋号必须是数字！")
                return

            building = int(building_str)

            # 先试探性查询一次确认存在
            yield event.plain_result(f"🔍 正在验证宿舍信息，请稍候……")
            success, msg, balance = await self.fetch_balance(building, room_str)
            if not success:
                yield event.plain_result(f"❌ 订阅失败，原因：\n{msg}")
                return

            new_sub = {"building": building, "room": room_str}

            async with self._data_lock:
                if umo not in self.subs:
                    self.subs[umo] = []

                if new_sub not in self.subs[umo]:
                    self.subs[umo].append(new_sub)
                    snapshot = self._get_data_snapshot()
                    added = True
                else:
                    snapshot = None
                    added = False

            if added:
                if snapshot is None:
                    raise RuntimeError("数据快照不应该为空。")
                saved = await self._save_snapshot(snapshot)
                if saved:
                    yield event.plain_result(
                        f"✅ 订阅成功！当前余额：{balance} 度\n该会话已订阅 {len(self.subs[umo])} 个宿舍。\n每天{self.check_hour}点{self.check_minute}分若电量低于 {self.threshold} 度将自动提醒。"
                    )
                else:
                    yield event.plain_result(
                        "⚠️ 订阅成功，但保存数据失败，请联系管理员。"
                    )
                return
            else:
                yield event.plain_result(
                    f"ℹ️ 该宿舍已在订阅列表中。\n当前余额：{balance} 度"
                )
                return

        # 处理退订指令
        elif action_or_building.lower() == "unsub":
            if len(parts) < 4:
                yield event.plain_result(
                    "❌ 参数不足！请使用格式：/bill unsub 楼栋号 宿舍号"
                )
                return
            building_str, room_str = parts[2], parts[3]
            if not building_str.isdigit():
                yield event.plain_result("❌ 楼栋号必须是数字！")
                return

            building = int(building_str)
            target_sub = {"building": building, "room": room_str}

            async with self._data_lock:
                if umo in self.subs and target_sub in self.subs[umo]:
                    self.subs[umo].remove(target_sub)
                    if not self.subs[umo]:  # 如果列表为空，删除该会话的订阅记录
                        del self.subs[umo]
                    snapshot = self._get_data_snapshot()
                    removed = True
                else:
                    snapshot = None
                    removed = False
            if removed:
                if snapshot is None:
                    raise RuntimeError("数据快照不应该为空。")
                saved = await self._save_snapshot(snapshot)
                if saved:
                    yield event.plain_result(
                        f"✅ 已成功取消订阅 {building_str}栋{room_str}室 的电量提醒。"
                    )
                else:
                    yield event.plain_result(
                        "⚠️ 取消订阅成功，但保存数据失败，请联系管理员。"
                    )
                return
            else:
                yield event.plain_result(f"ℹ️ 该宿舍未在订阅列表中。")
                return

        # ---------------- 3. 常规电费查询 ----------------
        if len(parts) < 3:
            yield event.plain_result("❌ 参数不足！\n正确格式：/bill 楼栋号 宿舍号")
            return

        building_str, room_str = parts[1], parts[2]

        if building_str == "114" and room_str == "514":
            yield event.plain_result("呜诶(＃°Д°)，好臭的数字（恼")
            return

        if not building_str.isdigit():
            yield event.plain_result("❌ 楼栋号必须是纯数字！")
            return

        building = int(building_str)
        user_id = str(event.message_obj.sender.user_id)

        yield event.plain_result("🔍 正在查询电费，请稍候……")
        success, msg, balance = await self.fetch_balance(building, room_str)

        if success:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            room_key = f"{building}-{room_str}"

            async with self._data_lock:
                self.last_queries[user_id] = {
                    "building": building,
                    "room": room_str,
                }

                record = self.room_queries_info.setdefault(
                    room_key,
                    {
                        "count": 0,
                        "last_query_time": "",
                        "last_query_user": "",
                        "last_query_umo": "",
                    },
                )
                record["count"] += 1
                record["last_query_time"] = now
                record["last_query_user"] = user_id
                record["last_query_umo"] = umo

                snapshot = self._get_data_snapshot()
            await self._save_snapshot(snapshot)

        yield event.plain_result(msg)
        return

    @filter.command("b")
    async def quick_query(self, event: AstrMessageEvent):
        """快速查询当前用户上次调用的宿舍"""
        umo = event.unified_msg_origin
        user_id = str(event.message_obj.sender.user_id)

        # 黑名单拦截 (依然保留群聊维度的黑名单控制)
        async with self._data_lock:
            blocked = umo in self.blacklist
        if blocked:
            yield event.plain_result("⚠️ 当前会话已禁用电费查询指令，请联系管理员启用。")
            return

        async with self._data_lock:
            if user_id not in self.last_queries:
                yield event.plain_result(
                    "❌ 您还没有历史查询记录，请先使用 /bill <楼栋> <宿舍> 查询一次。"
                )
                return

            record = self.last_queries[user_id].copy()

        building = record["building"]
        room_str = record["room"]

        yield event.plain_result("⚡ 正在快速查询您上次记录的宿舍...")
        success, msg, _ = await self.fetch_balance(building, room_str)
        yield event.plain_result(msg)
        return

    async def terminate(self):
        """插件卸载时的清理工作"""
        if hasattr(self, "timer_task"):
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
        logger.info("电费查询插件已卸载，定时任务已终止。")
