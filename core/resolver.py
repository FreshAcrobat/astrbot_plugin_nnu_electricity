"""
resolver.py - 解析宿舍号
"""

from typing import Any, Dict, Tuple

from .static_config import (
    DONGQU_ITEM_NUM,
    NEW_NORTH_SUFFIX_MAP,
    SPECIAL_BUILDINGS,
    ZONE_CONFIGS,
)


def _match_zone_info(building: int) -> Tuple[str, str, str]:
    # 优先检查特殊楼栋
    if building in SPECIAL_BUILDINGS:
        info = SPECIAL_BUILDINGS[building]
        return info["zone"], info["item_num"], info["rule"]

    # 遍历普通区域配置
    for cfg in ZONE_CONFIGS:
        if cfg["start"] <= building < cfg["end"]:
            return cfg["zone"], cfg["item_num"], "normal"

    raise ValueError(f"楼栋号 {building} 不存在")


def _parse_room_normal(building: int, room_str: str, rule_type: str) -> Dict[str, Any]:
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


def resolve_room_info(
    dongqu_cache: Dict[str, str], building: int, room_str: str
) -> Tuple[str, str, str]:
    # 1. 东区逻辑 (1-6栋)
    if 1 <= building <= 6:
        if not dongqu_cache:
            raise ValueError(
                "东区缓存未加载，请检查插件数据目录下的 dongqu_cache.json 文件"
            )

        if not room_str.isdigit() or len(room_str) != 3:
            raise ValueError("东区宿舍号必须是三位数字")

        cache_key = f"{building}-{room_str}"
        if cache_key not in dongqu_cache:
            raise ValueError(f"楼栋号 {building} 不存在")

        node_id = dongqu_cache[cache_key]
        return DONGQU_ITEM_NUM, node_id, f"{building}栋{room_str}室"

    # 2. 非东区（普通 + 新北）逻辑
    zone_name, item_num, rule_type = _match_zone_info(building)
    room_info = _parse_room_normal(building, room_str, rule_type)

    # 构造普通区 nodeId
    suffix = room_info.get("building_suffix")
    if suffix:
        node_id = f"{zone_name},{building}栋{suffix},{room_info['floor']}层,{room_info['room_full']}"
        display_name = f"{building}栋{suffix}{room_info['room_full']}室"
    else:
        node_id = (
            f"{zone_name},{building}栋,{room_info['floor']}层,{room_info['room_full']}"
        )
        display_name = f"{building}栋{room_str}室"

    return item_num, node_id, display_name
