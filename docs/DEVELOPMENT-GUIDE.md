# 开发指南：本机目录关系与运行模式（Windows 原生引擎优先）

> 2026-08-16 定稿（0.8.18 仓库治理版）：开发主线 = 本机 Windows 原生引擎模式；
> docker 镜像封装为可选发布物，仅当版本成熟后再构建（见 `docs/webui-spa/release-policy.md`）。

## 1. 两个目录的分工（严格遵循）

| 目录 | 角色 | 允许操作 |
|---|---|---|
| `C:\Users\75393\Desktop\stockdb` | **原生引擎运行时**：`stockdb.exe`（127.0.0.1:7899）+ `data/*.ldb`（全市场行情 LevelDB）+ `data1/` + `pybao/`（Python 扩展）+ `mydb/`（私有存储 LevelDB） | 只读。引擎由上游 `数据更新.exe` 同步维护，私有代码**不写文件**进此目录 |
| `C:\Users\75393\Desktop\stockdb-private` | **研究成果仓库**（本仓库，唯一版本化对象）：`stockdb-ai/`（app.py + 打板模块 + MCP）+ `docs/` | 全部代码改动在此，走分支 + PR |

**硬性禁止**（原任务书约定，维持有效）：
- 禁止改动/删除原生目录任何文件（`*.ldb` / `data*` / `*.pyd` / `stockdb.exe` 等）
- 禁止把原生目录 git 化；禁止把其中任何文件纳入本仓库 git
- 两个目录互不写文件；唯一的数据流动方向：**私有代码 → pybao 扩展 → 引擎（7899）→ LevelDB（data/ 行情 + mydb/ 私有存储）**

**数据落点说明**：打板回填/采集/收口写 `mydb`（引擎侧 LevelDB），键约定见
`docs/design/auction-collector.md` 存储设计——保留前缀（`竞价快照:` / `打板指标:` /
`打板序列:` / `清单:`）为 AI 禁用区，不得由第三方写入。

## 2. 运行配方（本机实测，缺一不可）

| 环境变量 | 值 | 原因 |
|---|---|---|
| `STOCKDB_HOST` | `127.0.0.1` | MCP 模块默认 host 是 NAS 内网 IP（`100.66.1.1`），不设会打到失效通道 |
| `STOCKDB_PORT` | `7899` | 引擎配置端口（原生目录 `stockdb.conf`） |
| `PYBAO_DIR` | `C:\Users\75393\Desktop\stockdb\pybao` | pybao 扩展实际位置（任务书原约定 D:\stockdb 在本机为 C:\Users\75393\Desktop\stockdb） |
| `PYTHONPATH` | 追加 `C:\Users\75393\Desktop\stockdb\pybao`（+ `stockdb-ai` 视脚本位置） | app.py `_mydb_import` 与 mcp 需要 `import stockdb` |
| `NO_PROXY` / `no_proxy` | `127.0.0.1,localhost` | 系统代理（127.0.0.1:7890）会被 python urllib 拾取，把引擎请求转发到代理 → 间歇 502/5s 超时；必须直连 |

**Python 解释器**：项目声明 3.14（`.python-version`），扩展为 3.14 非 free-threaded ABI
（`pybao/` 内 `3.14t+*.pyd` 为上游 free-threaded 备用件，当前不用）。本机 uv 索引最高
3.14.4，用 `uv venv --python 3.14.4` 即可（测试纯标准库、扩展 ABI 匹配已验证）。

**git 访问**（本机 TLS 特殊性）：Windows schannel 凭据获取失败（SEC_E_NO_CREDENTIALS），
仓库级 config 已固化 `http.sslBackend=openssl` + `http.proxy=http://127.0.0.1:7890`；
新 clone 时需手工加上这两个配置。

## 3. 开发工作流

1. 数据更新：上游 `数据更新.exe` 保持引擎数据最新（`000001` 最新交易日为探针）
2. 代码改动：本仓库分支 → 单测（`stockdb-ai/` 下 `python -m unittest test_auction_collect
   test_auction_metrics test_auction_list test_ops mcp.test_stockdb_mcp_server`，206 全绿）
3. 回填/采集：`auction_run_backfill(days=60)`（app.py）直连引擎写 mydb
4. 验收：异源签字口径见 `docs/design/auction-collector.md` 与 `docs/acceptance/`
5. 发布：CHANGELOG → 分支 → PR → 合并 → tag `vX.Y.Z`；docker 镜像仅成熟后手动触发
   `.github/workflows/build-image.yml`

## 4. 本机已知坑（排查手册）

- **pybao 首次加载慢/HTTP 502**：SDK 客户端初始化会全表预载复权因子，期间引擎忙；
  预热完成后恢复（避免在预热期并发 HTTP）
- **`rd.keys("*")` / `mydb_tables()` 会挂死**：全表扫描分钟级，引擎单连接串行处理；
  生产路径只做前缀通配（`rd.keys(table, "code:*")`）
- **mkdtemp 目录不可写**：Python 3.14 Windows 对 `os.mkdir(0o700)`（tempfile.mkdtemp
  内部）施加受限 ACL——仅影响受限令牌环境；真实终端无此问题
- **系统代理**：任何 python 网络请求默认走 127.0.0.1:7890（Clash），务必 `NO_PROXY`
