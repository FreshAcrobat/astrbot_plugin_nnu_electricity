"""
api.py - 接口的签名与请求层
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Dict

import httpx
from astrbot.api import logger

from .static_config import GETBALANCE_URL, SECRET_KEY


def generate_sign(params: Dict[str, Any], secret_key: str) -> str:
    """
    生成签名（与原网页保持一致）
    """
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
            raw_parts.append(json.dumps(v, separators=(",", ":"), ensure_ascii=False))
        else:
            raw_parts.append(str(v))
    raw_str = "|".join(raw_parts) + "|" + secret_key
    return hashlib.md5(raw_str.encode("utf-8")).hexdigest()


async def request_balance(
    client: httpx.AsyncClient, item_num: str, node_id: str
) -> Dict[str, Any]:
    """
    发送查询请求
    """
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
    """
    普通单次请求
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await request_balance(client, item_num, node_id)


class SubscriptionQueryExecutor:
    """
    订阅查询执行器
    """

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
