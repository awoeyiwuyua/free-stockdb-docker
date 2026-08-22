"""config — 应用层配置单一入口（0.9.1 四层架构框架）。

所有环境变量读取收敛于此；各层（interfaces/services/core/storage/ops）只引用本模块，
不直接读 os.environ。本模块零依赖（纯 stdlib），可被任何层导入。

0.9.2 搬迁说明：app.py 中与特定功能耦合的配置（STATIC_DIR/WEBUI_UI/MIRROR_PAGE_URL/
IMAGE_TAG 等）随对应模块搬迁时一并归位。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """环境变量整数解析：非法/缺失回退默认并告警（0.9.11——此前裸 int() 崩溃，
    import 阶段 ValueError 直接让 webui 起不来且无提示，与 auction_env_time 对齐）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"config: 环境变量 {name}={raw!r} 非法，回退默认 {default}", file=sys.stderr)
        return default


# ---- 引擎与部署（原生引擎 127.0.0.1:7899；容器内同进程） ----
STOCKDB_HOST: str = os.environ.get("STOCKDB_HOST", "127.0.0.1")
STOCKDB_PORT: int = _env_int("STOCKDB_PORT", 7899)
DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "/data"))
LISTEN_PORT: int = _env_int("WEBUI_PORT", 8080)

# 引擎进程控制（同步器/重启检测）
STOCKDB_PIDFILE: Path = Path(os.environ.get("STOCKDB_PIDFILE", "/data/stockdb.pid"))
STOCKDB_PAUSE: Path = Path(os.environ.get("STOCKDB_PAUSE_FLAG", "/data/.stockdb-paused"))
STOCKDB_LOG_FILE: Path = Path(os.environ.get("STOCKDB_LOG_FILE", "/data/log.txt"))

# 版本号（发布物标识，见 docs/release-policy.md）
WEBUI_VERSION: str = "0.10.3"

# ---- 打板调度触发点（HH:MM，非法值回退默认） ----
# 独立函数保留（0.9.2 随调度模块归位）；默认值与历史行为一致
def auction_env_time(name: str, default: str) -> str:
    """环境变量 HH:MM 校验：非法格式回退默认；合法值补零规范化。

    0.9.11：调度比较依赖补零字典序（now_hm 恒为 strftime 补零形），用户设
    '9:26' 时 '10:00'>='9:26' 为 False → 采集只在 09:26-09:59 触发、'8:30'
    收口永不触发——静默失效无日志。统一规范为 '09:26' 形。
    """
    v = os.environ.get(name, default)
    try:
        parsed = datetime.strptime(v, "%H:%M")
    except (TypeError, ValueError):
        return default
    return parsed.strftime("%H:%M")  # 补零规范化（'9:26' → '09:26'）


AUCTION_COLLECT_TIME: str = auction_env_time("AUCTION_COLLECT_TIME", "09:26")
AUCTION_CLOSE_TIME: str = auction_env_time("AUCTION_CLOSE_TIME", "16:30")

# ---- 仓库层（0.10.0 列式仓库：Parquet 事实沉淀 + DuckDB 查询，D12） ----
# 总开关（回滚演练用）：0 = 调度/接口整体关闭，53 个既有工具不受影响
WAREHOUSE_ENABLED: bool = os.environ.get("WAREHOUSE_ENABLED", "1") not in ("0", "false", "no")
# 沉淀任务触发点（HH:MM）：置于打板 close 16:30 与数据同步之后
WAREHOUSE_SEDIMENT_TIME: str = auction_env_time("WAREHOUSE_SEDIMENT_TIME", "16:40")
# 仓库根目录（facts/ 分区 + warehouse.duckdb + backups/）
WAREHOUSE_DIR: Path = Path(os.environ.get("WAREHOUSE_DIR", str(DATA_DIR / "warehouse")))
# run_sql 结果行数上限（超出截断，信封 truncated 承载）
WAREHOUSE_ROW_CAP: int = int(os.environ.get("WAREHOUSE_ROW_CAP", "5000"))
# 单条语句执行超时（秒）
WAREHOUSE_QUERY_TIMEOUT: int = int(os.environ.get("WAREHOUSE_QUERY_TIMEOUT", "30"))

# ---- 上游访问闸门（并发上限，0.6.4 熔断器配套） ----
STOCKDB_MAX_CONCURRENCY: int = _env_int("STOCKDB_MAX_CONCURRENCY", 8)
