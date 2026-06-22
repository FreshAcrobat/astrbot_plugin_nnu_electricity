# main.py
import os
import hashlib
import time
import json
import asyncio
from typing import Dict, Any, Tuple, List

import httpx
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain

# ----------------------------- 配置 -----------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
        SECRET_KEY = config.get("SECRET_KEY", "")
        GETBALANCE_URL = config.get("GETBALANCE_URL", "")
        REQUEST_TIMEOUT = config.get("REQUEST_TIMEOUT", 10)
        # 新增配置读取
        THRESHOLD = config.get("THRESHOLD", 30.0)
        DONGQU_ITEM_NUM = config.get("DONGQU_ITEM_NUM", "34")
        ZONE_CONFIGS = config.get("ZONE_RANGES", [])          # 列表
        SPECIAL_BUILDINGS_RAW = config.get("SPECIAL_BUILDINGS", {})
        NEW_NORTH_SUFFIX_RAW = config.get("NEW_NORTH_SUFFIX_MAP", {})
else:
    logger.error(f"配置文件 {CONFIG_FILE} 不存在")
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
            raw_parts.append(json.dumps(v, separators=(',', ':')))
        else:
            raw_parts.append(str(v))
    raw_str = "|".join(raw_parts) + "|" + secret_key
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

async def query_balance(item_num: str, node_id: str) -> Dict[str, Any]:
    """查询电费余额"""
    current_time = time.strftime("%Y%m%d%H%M%S")
    params = {
        "itemNum": item_num,
        "nodeId": node_id,
        "time": current_time,
    }
    sign = generate_sign(params, SECRET_KEY)
    payload = {**params, "sign": sign}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(GETBALANCE_URL, json=payload)
        resp.raise_for_status()
        return resp.json()

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
    def __init__(self, context: Context):
        super().__init__(context)

        # 1. 使用官方 API 获取持久化数据目录 (返回的是 pathlib.Path 对象)
        plugin_data_dir = StarTools.get_data_dir(self.name)
        
        # 2. 绑定具体的数据文件路径
        self.data_file = plugin_data_dir / "plugin_data.json"
        self.cache_file = plugin_data_dir / "dongqu_cache.json"

        self.dongqu_cache = {}
        self.subs = {}             # 订阅信息: {session_id: {"building": x, "room": y}}
        self.blacklist = []        # 黑名单列表: [session_id_1, session_id_2...]
        self.last_queries = {}     # 持久化用户查询缓存: {user_id: {"building": x, "room": y}}
        
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
                logger.info(f"成功加载东区缓存表，共 {len(self.dongqu_cache)} 个房间。")
            except Exception as e:
                logger.error(f"加载东区缓存表失败: {e}")
        else:
            logger.warning(f"东区缓存文件 {self.cache_file} 不存在，东区查询功能可能受限。")

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
                logger.info(f"成功加载插件数据：{len(self.subs)} 个订阅，{len(self.blacklist)} 个黑名单，{len(self.last_queries)} 条查询记录。")
            except Exception as e:
                logger.error(f"加载插件数据失败: {e}")
        else:
            self.save_plugin_data()

    def save_plugin_data(self):
        """保存订阅、黑名单和用户查询记录"""
        try:
            # 替换为 self.data_file
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump({
                    "subs": self.subs,
                    "blacklist": self.blacklist,
                    "last_queries": self.last_queries
                }, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存插件数据失败: {e}")
    
    def resolve_dorm_info(self, building: int, room_str: str) -> Tuple[str, str, str]:
        # 1. 东区逻辑 (1-6栋)
        if 1 <= building <= 6:
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

    async def fetch_balance(self, building: int, room_str: str) -> Tuple[bool, str, float]:
        """核心查询方法，返回 (是否成功, 提示/错误信息, 剩余电量)"""
        try:
            item_num, node_id, display_name = self.resolve_dorm_info(building, room_str)
            result = await query_balance(item_num, node_id)
            
            if result.get("code") == "1":
                used_amp_str = result.get("data", {}).get("usedAmp", "0")
                used_amp = float(used_amp_str)
                return True, f"⚡️ {display_name} 剩余电量：{used_amp_str} 度", used_amp
            else:
                msg = result.get("msg", "未知错误")
                return False, f"❌ {display_name} 查询失败：{msg}", 0.0
                
        except httpx.TimeoutException:
            return False, "⏰ 请求超时，请稍后重试。", 0.0
        except httpx.HTTPStatusError as e:
            return False, f"🔌 服务器异常 (HTTP {e.response.status_code})，请稍后重试。", 0.0
        except ValueError as e:
            return False, f"❌ {str(e)}", 0.0
        except Exception as e:
            logger.error(f"查询未知错误: {e}")
            return False, "💥 内部错误，请联系管理员查看日志。", 0.0

    async def daily_check_loop(self):
        """定时任务：每天早7点查询订阅并提醒"""
        while True:
            now = time.localtime()
            # 判断是否是早 7 点 00 分
            if now.tm_hour == 7 and now.tm_min == 0:
                logger.info("开始执行电费定时订阅检查...")
                for session_id, info in self.subs.items():
                    building = info.get("building")
                    room = info.get("room")
                    
                    success, msg, balance = await self.fetch_balance(building, room)
                    if success and balance < THRESHOLD:
                        try:
                            # 注意：Astrbot 的主动推送方法会根据接入的平台适配器有所不同
                            # 此处使用通用适配器的 broadcast 或上下文向 session 发送消息的方法
                            await self.context.send_message(session_id, [Plain(f"⚠️ 【电费不足提醒】\n{msg}\n请及时充值以免断电！")])
                        except Exception as e:
                            logger.error(f"向会话 {session_id} 发送电费提醒失败: {e}")
                
                # 检查完毕后休眠 60 秒，避免一分钟内重复触发
                await asyncio.sleep(60)
            else:
                # 每 30 秒检查一次时间
                await asyncio.sleep(30)

    @filter.command("bill")
    async def command_bill(self, event: AstrMessageEvent):
        session_id = event.message_obj.session_id
        
        # ---------------- 1. 会话黑名单检查 ----------------
        message_text = event.message_str.strip()
        parts = message_text.split()
        
        # 拦截开启/关闭指令 (这部分不受黑名单约束)
        if len(parts) == 2:
            action = parts[1].lower()
            if action == "off":
                if session_id not in self.blacklist:
                    self.blacklist.append(session_id)
                    self.save_plugin_data()
                yield event.plain_result("🚫 已在此会话禁用电费查询指令。")
                return
            elif action == "on":
                if session_id in self.blacklist:
                    self.blacklist.remove(session_id)
                    self.save_plugin_data()
                yield event.plain_result("✅ 已在此会话启用电费查询指令。")
                return
        
        # 如果当前会话在黑名单中，静默退出
        if session_id in self.blacklist:
            return

        # ---------------- 2. 指令解析与路由 ----------------
        if len(parts) == 1:
            help_msg = (
                "🔌 电费查询插件使用帮助:\n"
                "• /bill <楼栋> <宿舍> - 查询电费\n"
                "• /b - 快速查询上次查找的宿舍\n"
                "• /bill sub <楼栋> <宿舍> - 订阅每日低电量提醒\n"
                "• /bill unsub - 取消订阅\n"
                "• /bill on/off - 启用/禁用当前群组响应"
            )
            yield event.plain_result(help_msg)
            return

        action_or_building = parts[1]

        # 处理订阅指令
        if action_or_building.lower() == "sub":
            if len(parts) < 4:
                yield event.plain_result("❌ 参数不足！请使用格式：/bill sub 楼栋号 宿舍号")
                return
            building_str, room_str = parts[2], parts[3]
            if not building_str.isdigit():
                yield event.plain_result("❌ 楼栋号必须是数字！")
                return
                
            # 先试探性查询一次确认存在
            yield event.plain_result(f"🔍 正在验证宿舍信息，请稍候……")
            success, msg, _ = await self.fetch_balance(int(building_str), room_str)
            if success:
                self.subs[session_id] = {"building": int(building_str), "room": room_str}
                self.save_plugin_data()
                yield event.plain_result(f"✅ 订阅成功！当前余额：{msg.split('：')[1]}\n每天早7点若电量低于 {THRESHOLD} 度将自动提醒。")
            else:
                yield event.plain_result(f"❌ 订阅失败，原因：\n{msg}")
            return
            
        # 处理退订指令
        elif action_or_building.lower() == "unsub":
            if session_id in self.subs:
                del self.subs[session_id]
                self.save_plugin_data()
                yield event.plain_result("✅ 已成功取消电量提醒订阅。")
            else:
                yield event.plain_result("ℹ️ 当前会话尚未订阅提醒。")
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

        # 获取发送者的 UID
        user_id = str(event.message_obj.sender.user_id)

        # 缓存本次调用的参数到个人记录，并写入持久化文件
        self.last_queries[user_id] = {"building": building, "room": room_str}
        self.save_plugin_data()

        yield event.plain_result("🔍 正在查询电费，请稍候……")
        success, msg, _ = await self.fetch_balance(building, room_str)
        yield event.plain_result(msg)

    @filter.command("b")
    async def quick_query(self, event: AstrMessageEvent):
        """快速查询当前用户上次调用的宿舍"""
        session_id = event.message_obj.session_id
        user_id = str(event.message_obj.sender.user_id)
        
        # 黑名单拦截 (依然保留群聊维度的黑名单控制)
        if session_id in self.blacklist:
            return
            
        # 根据用户 UID 查找历史记录
        if user_id not in self.last_queries:
            yield event.plain_result("❌ 您还没有历史查询记录，请先使用 /bill <楼栋> <宿舍> 查询一次。")
            return
            
        record = self.last_queries[user_id]
        building = record["building"]
        room_str = record["room"]
        
        yield event.plain_result("⚡ 正在快速查询您上次记录的宿舍...")
        success, msg, _ = await self.fetch_balance(building, room_str)
        yield event.plain_result(msg)

    async def terminate(self):
        """插件卸载时的清理工作"""
        if hasattr(self, 'timer_task') and not self.timer_task.done():
            self.timer_task.cancel()
        logger.info("电费查询插件已卸载，定时任务已终止。")