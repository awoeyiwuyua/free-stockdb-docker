# 发布纪律（Release Policy）

> 目标：一次发布 = 一批改动 + 一个版本号 + 一次验证 + 一条记录。
> 起因：2026-08-15 一天 6 版（0.6.1→0.6.6），每个 commit 都发版、无 tag、无 CHANGELOG，
> 用户反复部署且无法判断"哪个版本是稳的"。本文件把纪律制度化。
> **0.8.18 修订：开发主线 = 本机 Windows 原生引擎模式（见 `docs/development-guide.md`）；
> docker 镜像为可选发布物，仅版本成熟后才构建。**
> **0.10.0 修订：本文由 docs/webui-spa/ 上提至 docs 根（发布纪律属全项目）；
> 文中「面板版本」措辞 = 应用版本（WEBUI_VERSION，后端整体——0.9.7 起后端即主体）。**

## 1. 版本语义（面板版本 = WEBUI_VERSION）

- `0.6.x`：补丁——修 bug / 小加固；**攒批发布**，不随 commit 走
- `0.7.0`：功能里程碑——新增一个完整能力（如回测引擎、备份/恢复）
- `1.0.0`：正式版——60 天模拟盘验收通过 + 数据契约冻结评审通过
- 版本号 `stockdb-ai/config.py` 的 `WEBUI_VERSION`；tag `vX.Y.Z` 打在本仓库（原生模式
  下 tag 即发布物，无需镜像）

## 2. 发布节奏

- **修复攒批**：小修合入 main 后不发版；攒到一组有意义的修复（或出现"必须现在修"的阻断问题）才发一版
- **功能里程碑**：一个 Phase 完成 → 一版
- **主线验证**：本机引擎跑单测 + 回填/采集验证（`docs/development-guide.md` 配方）
- **镜像低频**：docker 镜像**仅成熟版本**手动触发（build-image.yml 仅 workflow_dispatch，
  日常合并只走 test.yml 轻量门禁，不构建镜像）

## 3. 发布检查单（发版前全部勾选）

- [ ] 前端 Vitest 全绿（含 10 页空载荷挂载防线）
- [ ] Python 全绿（test_ops / test_auction_* / mcp）
- [ ] 本机原生模式验证：引擎连通 + 必要回填/采集冒烟（离线单测不覆盖的路径）
- [ ] 功能清单回归表对应页勾选（docs/history/m2-regression-checklist.md）
- [ ] CHANGELOG.md 更新（本版一行说明）
- [ ] 合并 main → 打 tag `vX.Y.Z`；**若本版同时出镜像**：手动触发 build-image.yml → CI 四 job 绿
- [ ] 部署/验证记录更新 docs/deployments.md（镜像部署时）或本机验证记录（原生模式）

**分支纪律（2026-08-22 起）**：PR 合入 main 后功能分支即删（仓库已开启 GitHub
"自动删除 head 分支"；历史堆积的 53 个陈旧分支已于 0.10.0 后清理，仅保留
main 与确未合入的工作分支）。长期分支只有 main；tag 是发布历史的载体。

## 4. 发布物清单

| 发布物 | 位置 | 说明 |
|---|---|---|
| 版本号 | `stockdb-ai/config.py` 的 WEBUI_VERSION | 每版必更 |
| 变更说明 | CHANGELOG.md | 每版必更 |
| Git 标记 | tag `vX.Y.Z` | 每版必打（原生模式下即最终发布物） |
| 镜像 | `ghcr.io/awoeyiwuyua/stockdb-ai` | **可选**：仅成熟版本手动构建（+ 上游版本 tag） |
| 部署记录 | docs/deployments.md | 镜像部署时更新

## 5. 回滚约定

- 面板级（首选）：compose 环境变量 `WEBUI_UI=legacy` → 旧面板，零停机
- 镜像级：compose 改回上一个已知良好的镜像 digest/tag
