"""storage.providers.free_stockdb — 上游引擎访问闸口（0.9.2 批次 3 从 app.py 搬迁）。

统一封装对上游引擎（127.0.0.1:7899）的 HTTP 访问：信号量限并发 + 熔断器（探针路径）。
架构定位（总纲 D3）：上游 free-stockdb 是数据层 provider 之一——应用层只经本模块
访问引擎，将来引擎被替换/镜像时改本模块即可。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import config  # 模块引用（config.STOCKDB_HOST/PORT/STOCKDB_MAX_CONCURRENCY 动态读取）

# 上游访问治理（并发卫生：熔断 + 信号量）。
# 事故复盘（0.6.x 多标签切页打瘫后端）：stockdb 是全部路径的共享依赖，慢/挂时
# 每次探针都等满超时（扇出 3~4 路 × 10s），并发线程无界堆积。
# 治理原则：探针路径（breaker=True）记仇快败；全部路径过信号量限并发；
# 控制路径（启动等待/同步校验）只过信号量、不受熔断牵连。
_gate = threading.Semaphore(config.STOCKDB_MAX_CONCURRENCY)
_breaker: dict = {"fails": 0, "open_until": 0.0,
                  "threshold": 3, "cooldown": 300.0}


def _breaker_open() -> bool:
    return time.time() < _breaker["open_until"]


def fetch(path: str, timeout: float = 10.0, breaker: bool = False,
          base: str | None = None, block: bool = False) -> str:
    """打 stockdb HTTP 的统一闸口。

    - 信号量（全路径）：并发最多 STOCKDB_MAX_CONCURRENCY（默认 8）。默认非阻塞：
      满则立即抛 RuntimeError 由调用方降级（不等待——等待会堆积线程）；
      block=True（MCP 查询路径，0.10.0 C1）：满则排队等待槽位——查询正确性
      优先于快败，与收敛前直连行为一致。
    - 熔断器（breaker=True 的探针路径）：连续 threshold 次失败后 open_until 内
      快速失败，调用方降级取缓存，避免 stockdb 挂/忙时每路都等满超时。
    - base（0.10.0 C1）：完整 host:port 覆盖（默认 None = config 单一配置源）。
      唯一使用方是 MCP 接口层——其独立运行模式默认 host 与 config 不同
      （100.66.1.1 vs 127.0.0.1），双轨收敛时经此参数保持既有行为不漂移。
    """
    if breaker and _breaker_open():
        raise RuntimeError(
            f"stockdb 熔断中（连续 {_breaker['fails']} 次失败，"
            f"降级至 {datetime.fromtimestamp(_breaker['open_until']).strftime('%H:%M:%S')}）")
    if not _gate.acquire(blocking=block):
        raise RuntimeError("stockdb 并发已满（信号量限流），本次降级")
    try:
        import urllib.request
        host_port = base if base else f"{config.STOCKDB_HOST}:{config.STOCKDB_PORT}"
        with urllib.request.urlopen(
                f"http://{host_port}{path}", timeout=timeout) as resp:
            data = resp.read().decode("utf-8", "replace")
        _breaker["fails"] = 0  # 成功即复位
        return data
    except Exception:
        if breaker:
            _breaker["fails"] += 1
            if _breaker["fails"] >= _breaker["threshold"]:
                _breaker["open_until"] = time.time() + _breaker["cooldown"]
        raise
    finally:
        _gate.release()
