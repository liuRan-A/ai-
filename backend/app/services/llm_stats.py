# -*- coding: utf-8 -*-
"""LLM 调用的错误分类 + 运行统计 + 本地限流。

设计原则：
1. 错误分类（LLMErrorKind）是全后端统一的"错误语言"，
   监控接口、日志、降级决策都基于它，而不是散落的字符串匹配；
2. 统计全部放内存（进程级），不引入 Redis 等外部依赖；
   重启即清零，对本地/单实例部署足够；
3. 本地限流用滑动窗口，超限直接拒绝本次调用（按 RATE_LIMIT 归类），
   避免把 DeepSeek 侧 429 打爆。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from enum import Enum


class LLMErrorKind(str, Enum):
    """LLM 调用失败分类。RETRYABLE 决定 _chat_json 是否重试。"""
    NOT_CONFIGURED = "not_configured"   # 未配置 API Key（不算故障，记 info）
    TIMEOUT = "timeout"                 # 网络/读取超时          —— 可重试
    NETWORK = "network"                 # 连接错误/DNS/重置      —— 可重试
    RATE_LIMIT = "rate_limit"           # 429 或本地限流拦截      —— 可重试（带退避）
    AUTH = "auth"                       # 401/403 密钥无效       —— 不重试，需人工换 Key
    BALANCE = "balance"                 # 402/余额不足           —— 不重试，需人工充值
    BAD_REQUEST = "bad_request"         # 400/422 参数错误       —— 不重试，属代码 bug，日志要响
    SERVER = "server"                   # 5xx 上游故障           —— 可重试一次
    PARSE = "parse"                     # 200 但 JSON 解析失败   —— 不重试
    UNKNOWN = "unknown"


# 哪些错误值得自动重试（其余直接降级，重试只会浪费配额）
RETRYABLE: frozenset[LLMErrorKind] = frozenset({
    LLMErrorKind.TIMEOUT,
    LLMErrorKind.NETWORK,
    LLMErrorKind.RATE_LIMIT,
    LLMErrorKind.SERVER,
})


def classify_http_error(status_code: int, body: str = "") -> LLMErrorKind:
    """按 HTTP 状态码 + 响应体特征归类错误。"""
    if status_code in (401, 403):
        return LLMErrorKind.AUTH
    if status_code == 402 or "Insufficient Balance" in body:
        return LLMErrorKind.BALANCE
    if status_code == 429:
        return LLMErrorKind.RATE_LIMIT
    if status_code in (400, 404, 422):
        return LLMErrorKind.BAD_REQUEST
    if status_code >= 500:
        return LLMErrorKind.SERVER
    return LLMErrorKind.UNKNOWN


def classify_exception(exc: Exception) -> LLMErrorKind:
    """按 httpx 异常类型归类（不 import httpx，用类名判断，避免循环依赖）。"""
    name = type(exc).__name__
    if "Timeout" in name:                       # ConnectTimeout/ReadTimeout/PoolTimeout
        return LLMErrorKind.TIMEOUT
    if any(k in name for k in ("ConnectError", "NetworkError", "ProtocolError")):
        return LLMErrorKind.NETWORK
    return LLMErrorKind.UNKNOWN


class LLMStats:
    """进程内 LLM 调用统计（线程安全）。监控接口的数据源。"""

    def __init__(self, recent_size: int = 50):
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._recent_size = recent_size
        self._total = 0
        self._success = 0
        self._retried = 0                       # 发生过重试的调用次数
        self._fail_by_kind: dict[str, int] = {}
        self._fail_by_caller: dict[str, int] = {}
        self._latency_sum = 0.0
        self._latency_max = 0.0
        self._recent: deque[dict] = deque(maxlen=recent_size)
        self._window: deque[float] = deque()    # 本地限流滑动窗口（调用时间戳）

    # —— 本地限流 ——
    def allow(self, rpm: int) -> bool:
        """rpm<=0 表示不限流；否则检查最近 60s 窗口内调用数。"""
        if rpm <= 0:
            return True
        now = time.time()
        with self._lock:
            while self._window and now - self._window[0] > 60:
                self._window.popleft()
            if len(self._window) >= rpm:
                return False
            self._window.append(now)
            return True

    # —— 记录 ——
    def record(self, *, caller: str, ok: bool, latency_s: float,
               kind: LLMErrorKind | None = None, detail: str = "",
               retried: bool = False):
        with self._lock:
            self._total += 1
            self._latency_sum += latency_s
            self._latency_max = max(self._latency_max, latency_s)
            if retried:
                self._retried += 1
            if ok:
                self._success += 1
                return
            k = (kind or LLMErrorKind.UNKNOWN).value
            self._fail_by_kind[k] = self._fail_by_kind.get(k, 0) + 1
            self._fail_by_caller[caller] = self._fail_by_caller.get(caller, 0) + 1
            self._recent.append({
                "ts": time.strftime("%H:%M:%S"),
                "caller": caller,
                "kind": k,
                "detail": detail[:200],
                "latency_s": round(latency_s, 2),
            })

    # —— 快照（监控接口直接序列化）——
    def snapshot(self) -> dict:
        with self._lock:
            fail = self._total - self._success
            return {
                "uptime_s": int(time.time() - self._started_at),
                "total": self._total,
                "success": self._success,
                "failed": fail,
                "degraded": fail,               # 失败即调用方走规则降级
                "success_rate": round(self._success / self._total, 4) if self._total else None,
                "retried_calls": self._retried,
                "fail_by_kind": dict(self._fail_by_kind),
                "fail_by_caller": dict(self._fail_by_caller),
                "latency": {
                    "avg_s": round(self._latency_sum / self._total, 2) if self._total else None,
                    "max_s": round(self._latency_max, 2),
                },
                "recent_errors": list(self._recent)[::-1],   # 最新的在前
            }


# 全局单例：llm.py 记录，monitor.py 读取
llm_stats = LLMStats(recent_size=50)
