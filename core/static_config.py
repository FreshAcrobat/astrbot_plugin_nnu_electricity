"""
config.py - 静态配置加载
"""

import json
from pathlib import Path

from astrbot.api import logger

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PLUGIN_DIR / "config.json"


def _load_static_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        logger.error(
            "配置文件 %s 不存在",
            CONFIG_FILE,
        )
        raise ValueError("配置文件缺失，请确保 config.json 存在于插件目录下") from None


_config = _load_static_config()

SECRET_KEY = _config.get("SECRET_KEY", "")
GETBALANCE_URL = _config.get("GETBALANCE_URL", "")
DONGQU_ITEM_NUM = _config.get("DONGQU_ITEM_NUM", "34")
ZONE_CONFIGS = _config.get("ZONE_RANGES", [])  # 列表
SPECIAL_BUILDINGS_RAW = _config.get("SPECIAL_BUILDINGS", {})
NEW_NORTH_SUFFIX_RAW = _config.get("NEW_NORTH_SUFFIX_MAP", {})

# 转换键为整数，便于后续使用
SPECIAL_BUILDINGS = {int(k): v for k, v in SPECIAL_BUILDINGS_RAW.items()}
NEW_NORTH_SUFFIX_MAP = {int(k): v for k, v in NEW_NORTH_SUFFIX_RAW.items()}
