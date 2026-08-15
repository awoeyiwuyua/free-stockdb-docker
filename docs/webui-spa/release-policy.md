# 发布纪律（Release Policy）

> 目标：一次发布 = 一批改动 + 一个版本号 + 一次构建 + 一次部署 + 一条记录。
> 起因：2026-08-15 一天 6 版（0.6.1→0.6.6），每个 commit 都发版、无 tag、无 CHANGELOG，
> 用户反复部署且无法判断"哪个版本是稳的"。本文件把纪律制度化。

## 1. 版本语义（面板版本 = WEBUI_VERSION）

- `0.6.x`：补丁——修 bug / 小加固；**攒批发布**，不随 commit 走
- `0.7.0`：功能里程碑——新增一个完整能力（如回测引擎、备份/恢复）
- `1.0.0`：正式版——60 天模拟盘验收通过 + 数据契约冻结评审通过
- 镜像 tag 继续跟随上游引擎版本（`docker/Dockerfile` 的 ARG VERSION），与面板版本分离

## 2. 发布节奏

- **修复攒批**：小修合入 main 后不发版；攒到一组有意义的修复（或出现"必须现在修"的阻断问题）才发一版
- **功能里程碑**：一个 Phase 完成 → 一版
- **构建低频**：镜像构建只手动触发（build-image.yml 仅 workflow_dispatch）；日常合并走 test.yml 轻量门禁（无镜像）

## 3. 发布检查单（发版前全部勾选）

- [ ] 前端 Vitest 全绿（含 10 页空载荷挂载防线）
- [ ] Python 全绿（test_ops / test_paper / mcp）
- [ ] `npm run build` 通过；本地双通道冒烟（app.py + dist；11 深链 + 旧路径重定向）
- [ ] 功能清单回归表对应页勾选（docs/webui-spa/m2-regression-checklist.md）
- [ ] CHANGELOG.md 更新（本版一行说明）
- [ ] 合并 main → 打 tag `v0.x.y` → 触发镜像构建 → CI 四 job 绿
- [ ] 通知用户部署；部署后验证并更新 docs/DEPLOYMENTS.md

## 4. 发布物清单

| 发布物 | 位置 |
|---|---|
| 版本号 | `docker/webui/app.py` 的 WEBUI_VERSION |
| 变更说明 | CHANGELOG.md |
| Git 标记 | tag `v0.x.y` |
| 镜像 | `ghcr.io/awoeyiwuyua/free-stockdb:latest`（+ 上游版本 tag） |
| 部署记录 | docs/DEPLOYMENTS.md |

## 5. 回滚约定

- 面板级（首选）：compose 环境变量 `WEBUI_UI=legacy` → 旧面板，零停机
- 镜像级：compose 改回上一个已知良好的镜像 digest/tag
