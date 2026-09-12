"""
command.py - 指令解析
"""

from astrbot.api.event import AstrMessageEvent

from .service import ElectricityService
from .store import DataStore


class CommandHandler:
    def __init__(self, store: DataStore, service: ElectricityService):
        self._store = store
        self._service = service

    async def handle_suball(self, event: AstrMessageEvent):
        """列出所有会话的订阅"""
        async with self._store.lock:
            subs_copy = {umo: rooms.copy() for umo, rooms in self._store.subs.items()}

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

    async def handle_billstat(self, event: AstrMessageEvent):
        """列出所有房间的查询信息"""
        async with self._store.lock:
            room_info_copy = dict(self._store.room_queries_info)

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

    async def handle_bill(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin

        # ---------------- 1. 会话黑名单检查 ----------------
        message_text = event.message_str.strip()
        parts = message_text.split()

        # 拦截开启/关闭指令 (这部分不受黑名单约束)
        if len(parts) == 2:
            action = parts[1].lower()
            if action == "off":
                async with self._store.lock:
                    if umo not in self._store.blacklist:
                        self._store.blacklist.append(umo)
                        snapshot = self._store.get_snapshot()
                    else:
                        snapshot = None
                if snapshot:
                    await self._store.save_snapshot(snapshot)
                yield event.plain_result("🚫 已在此会话禁用电费查询指令。")
                return
            elif action == "on":
                async with self._store.lock:
                    if umo in self._store.blacklist:
                        self._store.blacklist.remove(umo)
                        snapshot = self._store.get_snapshot()
                    else:
                        snapshot = None
                if snapshot:
                    await self._store.save_snapshot(snapshot)
                yield event.plain_result("✅ 已在此会话启用电费查询指令。")
                return

        # 如果当前会话在黑名单中，静默退出
        async with self._store.lock:
            blocked = umo in self._store.blacklist
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
            success, msg, balance = await self._service.fetch_balance(
                building, room_str
            )
            if not success:
                yield event.plain_result(f"❌ 订阅失败，原因：\n{msg}")
                return

            new_sub = {"building": building, "room": room_str}

            async with self._store.lock:
                if umo not in self._store.subs:
                    self._store.subs[umo] = []

                if new_sub not in self._store.subs[umo]:
                    self._store.subs[umo].append(new_sub)
                    snapshot = self._store.get_snapshot()
                    added = True
                else:
                    snapshot = None
                    added = False

            if added:
                if snapshot is None:
                    raise RuntimeError("数据快照不应该为空。")
                saved = await self._store.save_snapshot(snapshot)
                if saved:
                    yield event.plain_result(
                        f"✅ 订阅成功！当前余额：{balance} 度\n该会话已订阅 {len(self._store.subs[umo])} 个宿舍。\n每天{self._service.check_hour}点{self._service.check_minute}分若电量低于 {self._service.threshold} 度将自动提醒。"
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

            async with self._store.lock:
                if umo in self._store.subs and target_sub in self._store.subs[umo]:
                    self._store.subs[umo].remove(target_sub)
                    if not self._store.subs[umo]:  # 如果列表为空，删除该会话的订阅记录
                        del self._store.subs[umo]
                    snapshot = self._store.get_snapshot()
                    removed = True
                else:
                    snapshot = None
                    removed = False
            if removed:
                if snapshot is None:
                    raise RuntimeError("数据快照不应该为空。")
                saved = await self._store.save_snapshot(snapshot)
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
        success, msg, balance = await self._service.fetch_balance(building, room_str)

        if success:
            async with self._store.lock:
                self._store.last_queries[user_id] = {
                    "building": building,
                    "room": room_str,
                }

                self._store.update_room_query_info(
                    building,
                    room_str,
                    user_id,
                    umo,
                    balance,
                )

                snapshot = self._store.get_snapshot()
            await self._store.save_snapshot(snapshot)
        else:
            msg = await self._store.append_history_cache(
                building,
                room_str,
                msg,
            )

        yield event.plain_result(msg)
        return

    async def handle_b(self, event: AstrMessageEvent):
        """快速查询当前用户上次调用的宿舍"""
        umo = event.unified_msg_origin
        user_id = str(event.message_obj.sender.user_id)

        # 黑名单拦截 (依然保留群聊维度的黑名单控制)
        async with self._store.lock:
            blocked = umo in self._store.blacklist
        if blocked:
            yield event.plain_result("⚠️ 当前会话已禁用电费查询指令，请联系管理员启用。")
            return

        async with self._store.lock:
            if user_id not in self._store.last_queries:
                yield event.plain_result(
                    "❌ 您还没有历史查询记录，请先使用 /bill <楼栋> <宿舍> 查询一次。"
                )
                return

            record = self._store.last_queries[user_id].copy()

        building = record["building"]
        room_str = record["room"]

        yield event.plain_result("⚡ 正在快速查询您上次记录的宿舍...")
        success, msg, balance = await self._service.fetch_balance(building, room_str)
        if success:
            async with self._store.lock:
                self._store.update_room_query_info(
                    building,
                    room_str,
                    user_id,
                    umo,
                    balance,
                )

                snapshot = self._store.get_snapshot()
            await self._store.save_snapshot(snapshot)
        else:
            msg = await self._store.append_history_cache(
                building,
                room_str,
                msg,
            )

        yield event.plain_result(msg)
        return
