# Phase 5 — Web 面板 SPA 重构：架构与实施路径

> 状态：方案待确认（技术选型一项待用户拍板）
> 目标版本：`WEBUI_VERSION 0.6.0`
> 本文同时作为**学习材料**：每个技术名词第一次出现时给出白话解释。

---

## 1. 目标与边界

**要解决的两个问题**

1. **"杂"**：面板功能是五个 Phase 逐步堆上去的（5 页签 + 2 子页签 + 全局状态条 + 散落卡片），没有总览聚合、没有统一视觉。
2. **"改不动"**：前端约 1300 行 HTML/CSS/JS 塞在 `app.py` 的 `PAGE` 字符串里（文件已 4771 行），每次迭代都要改巨型字符串，转义易错。

**本次重构的范围（明确边界）**

- ✅ 前端整体重写为 SPA，后端 `/api/*` 与 `/mcp` **一个字节不动**（223 个 Python 测试全绿是硬性验收）。
- ✅ 信息架构重排：侧边栏分组导航 + 新增"总览"首页看板。
- ✅ 旧面板保留为 `/legacy` 逃生通道（环境变量开关），一个版本周期后移除。
- ❌ 不做：后端重构、新业务功能、MCP 协议改动、交易逻辑改动。
- ❌ 不破坏：单镜像、纯 Python 运行时、NAS 部署方式（`pull && up -d`）、隐私不变量（apikey 掩码、交易只走 webui）。

---

## 2. 现在的技术账（为什么 C 值得）

| | 现状（原生 JS 毛坯房） | 目标（SPA 标准做法） |
|---|---|---|
| 界面组织 | 一个 PAGE 字符串，780 行 JS 手动 getElementById | 组件化：卡片/表格/抽屉都是可复用积木 |
| 数据更新 | 每加一个数字手写一行 DOM 赋值 | 响应式：数据变了界面自动变 |
| 页面切换 | 手写 showTab()，全在一个页面里堆 | 路由：每个页面有独立 URL，可收藏可回退 |
| 样式 | 182 行全局 CSS，牵一发动全身 | 组件级样式 + 主题变量（暗色/浅色可切） |
| 图表 | 无 | ECharts：审计报告净值曲线、MCP 统计图 |
| 改造成本 | — | 一次性大，之后每次迭代又快又稳 |

---

## 3. 技术选型（每个名词一句话）

### 3.1 框架：**Vue 3**（推荐）

- **是什么**：写界面的积木系统。界面拆成组件（`.vue` 文件 = 模板+逻辑+样式三合一），数据与界面双向自动同步。
- **为什么选它**：中文文档全行业最丰富、中文社区最大；`Vue3 + Vite + Element Plus` 是国内后台管理面板的事实标准栈（vue-element-admin 体系），示例和教程最多——**对学习者最友好**。
- **替代方案 React 18**：同样成熟，英文生态更主流；若你未来想往国外社区/公司靠，可换 React + Ant Design。两者能力等价，本方案所有页面设计不绑定框架。

### 3.2 构建工具：**Vite**

- **是什么**：开发时的"即时预览服务器"（本地改一行代码浏览器毫秒级热更新）+ 发布时的"打包器"（把源码编译成浏览器能跑的静态文件）。
- **关键认知**：**Node 只出现在开发机和 CI 的构建期**，编译产物 `dist/` 是纯静态文件，最终镜像里仍然只有 Python。NAS 运行方式不变。

### 3.3 组件库：**Element Plus**（Vue 版）/ **Ant Design**（React 版）

- **是什么**：别人写好的高质量 UI 积木（表格、表单、抽屉、通知、骨架屏……），我们直接拼装。
- **为什么**：比自己写快、好看、稳定；中文文档好。

### 3.4 路由：**Vue Router**

- **是什么**：管理"网址 ↔ 页面"的对应关系。`/paper` 打开模拟盘、`/overview` 打开总览，浏览器前进后退、刷新、收藏都正常。

### 3.5 图表：**ECharts**

- **是什么**：百度开源的可视化库，金融面板标配（折线/柱状/K 线都能画）。
- **用在哪**：审计报告净值 vs 基准曲线、MCP 调用统计、数据滞后趋势。

### 3.6 状态：**Pinia**（Vue 官方状态库）

- **是什么**：跨页面共享数据的"全局仓库"。比如顶栏的告警红点、数据新鲜度，所有页面都要读，放这里一份即可。

### 3.7 其余

- HTTP：浏览器原生 `fetch` 封装一个 `api/http.js`（超时、错误码→提示、重试），不引 axios，减少依赖。
- 语言：JavaScript（不上 TypeScript，降低学习门槛；本项目规模 JS 足够）。
- 测试：Vitest + @vue/test-utils（前端冒烟测试）。

---

## 4. 总体架构

### 4.1 运行时（NAS 上，和现在一致）

```
浏览器 (http://NAS:8081)
   │  首次加载：GET / → app.py 返回 static/index.html
   │  之后：/assets/*.js|css → app.py 从 /opt/webui/static/ 吐静态文件
   │
   └─ 数据请求：GET/POST /api/* → app.py（纯标准库，API 不变）
                     ├── SQLite（paper.sqlite3 / mydb）
                     ├── 上游 GitHub（版本检查）
                     └── 东方财富妙想（仅模拟盘、仅启用交易时）
   /mcp → MCP 路由（不变）        /legacy → 旧面板（逃生通道）
```

### 4.2 构建时（开发机 / CI，Node 只在此时出现）

```
stockdb-ai/spa/  (Vue 源码)
   │  npm run dev  → Vite 本地服务器，代理 /api 到本地 webui（学习/开发用，热更新）
   │  npm run build → spa/dist/（纯静态文件）
   │
Dockerfile 多阶段：
   阶段1  node:22-alpine  ── 编译出 dist/
   阶段2  python:3.14-*    ── COPY dist → /opt/webui/static/（最终镜像只有 Python）
```

---

## 5. 目录结构（新增/改动一览）

```
stockdb-ai/
├── app.py                      # 改动：删 PAGE 字符串 → 静态服务 + SPA fallback + /legacy + /api/overview
├── paper_*.py / mx_client.py   # 不动
├── mcp/                        # 不动
├── test_ops.py 等              # 不动（全部 API 层，无 PAGE 依赖）
├── static/                     # 新增：SPA 构建产物（镜像内 /opt/webui/static/）
│   └── legacy/index.html       # 旧 PAGE 搬家到这里（/legacy 逃生通道）
└── spa/                        # 新增：Vue 源码（不进镜像，只进构建期）
    ├── package.json / vite.config.js / index.html
    └── src/
        ├── main.js             # 入口：挂载 App + 路由 + Element Plus
        ├── App.vue             # 根布局：侧边栏 + 顶栏(全局状态条) + 内容区
        ├── router/index.js     # 路由表 = 左侧导航
        ├── api/                # http.js + status.js/data.js/paper.js/ops.js/mcp.js/version.js
        ├── stores/             # Pinia：全局状态（新鲜度/告警红点/模拟盘摘要）
        ├── components/         # 通用积木：StatCard/DataTable/EmptyState/Skeleton/RefreshButton
        ├── views/              # 页面（见第 6 节 IA）
        └── styles/             # 主题变量（金融风、深色可切）
```

---

## 6. 信息架构（治"杂"的核心设计）

### 6.1 侧边栏分组（5 页签 → 4 组 10 页）

| 分组 | 页面（路由） | 内容 | 来源 |
|---|---|---|---|
| **总览** | `/overview` | **新**：一张屏看全——数据新鲜度卡、今日模拟盘时间轴进度、情绪投递状态、告警摘要、MCP 健康、版本 | 聚合 `/api/overview` |
| **数据** | `/data/sync` | 数据源同步 + 港股同步（原"数据同步"页签） | 现有 |
| | `/data/mydb` | 私有存储查询（原子页签升格） | 现有 |
| **模拟盘** | `/paper` | 状态卡 + 开关 + 今日时间轴 + 持仓/净值 + 决策/订单/成交明细 | 现有 |
| | `/paper/audit` | 审计报告（净值 vs 159915 基准曲线 → ECharts） | 现有 |
| | `/paper/signal` | 情绪信号体检 + 连通性 + API Key 面板（敏感操作独立成页） | 现有 |
| **运维** | `/ops/system` | 系统健康 / 日志 / 容器 | 现有 |
| | `/ops/alerts` | 通知中心（原"通知"页签） | 现有 |
| | `/ops/mcp` | MCP 观测（成功率/耗时 p95/按工具统计 → ECharts） | 现有 |
| | `/ops/version` | 上游版本对比卡 | 现有 |

### 6.2 全局框架（每页共享）

- **顶栏**：全局状态条（数据滞后天数、模拟盘状态徽标、告警红点、当前时间）+ 刷新节拍（统一 30s 轮询，页签失焦自动降频）。
- **总览看板**是"一屏看全"，其余页面是"深挖一处"——先总后分，解决"杂"。

---

## 7. 后端 app.py 的最小改动（additive，绝不动现有 API）

1. **PAGE 外置**：1300 行前端字符串整体搬到 `static/legacy/index.html`，`/legacy` 路由原样渲染（一个字节不改）。
2. **静态服务**：`/` 与 `/assets/*` 从 `/opt/webui/static/` 读取；非 `/api`、非 `/mcp` 的 GET 全部回退 `index.html`（SPA 路由需要）。安全：`os.path.realpath` 校验必须落在 static 目录内（防路径穿越）；正确 Content-Type + `Cache-Control`（assets 带 hash 可长缓存）。
3. **切换开关**：环境变量 `WEBUI_UI=spa|legacy|both`，默认 `spa`。`/api/version` 增加 `ui_mode` 字段。**出问题一条 env 切回旧面板，零停机**。
4. **新增 `/api/overview`**：把总览页需要的 5 个现有接口聚合成一次调用（减少轮询次数），内部复用现有函数，不写新业务逻辑。
5. **版本**：`WEBUI_VERSION = "0.6.0"`。
6. **测试**：现有 223 测试不动；test_ops 追加几条静态服务/fallback/legacy/overview 的新用例（预计 +8~12 条）。

---

## 8. Docker 与 CI 变化

**Dockerfile（增量 ~10 行）**

```dockerfile
# ===== SPA 构建（Node 仅构建期存在，产物进入最终镜像） =====
FROM node:22-alpine AS spa-build
WORKDIR /build
COPY stockdb-ai/spa/package.json stockdb-ai/spa/package-lock.json ./
RUN npm ci
COPY stockdb-ai/spa/ ./
RUN npm run build

# 最终阶段追加：
COPY --from=spa-build /build/dist /opt/webui/static/
COPY stockdb-ai/static/legacy/ /opt/webui/static/legacy/
```

多架构：`node:22-alpine` 有官方 amd64+arm64 manifest，双架构构建不受影响。

**CI（build-image.yml 增量）**

- `build-stockdb` job 自动获得多阶段构建（buildx 天然支持）。
- 新增 `test-spa` job：`npm ci && npm run test`（Vitest 冒烟）+ `npm run build`（保证可编译）。
- `test-mcp` job 追加：跑 `test_ops`（此前只在本地跑）——顺手把 223 全绿变成 CI 门禁。

---

## 9. 实施路径（M0 → M3，每个里程碑含学习点与验收门）

### M0 — 脚手架与双轨打通（"Hello SPA"）

- 做：初始化 `spa/` 工程；app.py 静态服务 + `/legacy`；Dockerfile 多阶段；CI 新 job。
- 学习点：npm/package.json 是干什么的；Vite 热更新；`.vue` 单文件组件三件套（template/script/style）；Docker 多阶段构建；SPA 回退路由。
- 验收：本地 `npm run dev` 打开新壳（Hello 页）；本地构建镜像后 `/` 出新壳、`/legacy` 出旧面板、`/api/*` 全绿；CI 三+一 job 全绿。

### M1 — 布局/路由/组件基座（"骨架"）

- 做：App.vue 侧边栏+顶栏布局；router 9 个路由（空页面占位）；Element Plus 主题（金融风 + 深色切换）；`api/http.js` 封装；StatCard/EmptyState/Skeleton 基础积木；顶栏状态条接真实 `/api/health` 轮询。
- 学习点：响应式 `ref/reactive/computed`；组件与 Props；Vue Router；`onMounted/onUnmounted` 与轮询生命周期；fetch 封装。
- 验收：9 个路由可导航、URL 可收藏刷新；状态条实时显示数据滞后与模拟盘状态；暗色切换正常。

### M2 — 页面搬迁（四小步，每步一个验收）

- 做：按 IA 逐域搬迁，**先建功能清单回归表**（把现有每页每个功能逐项列出作为验收标准）：
  - M2a 总览看板（新，聚合 `/api/overview`）
  - M2b 数据域：同步 + mydb
  - M2c 模拟盘域：状态/时间轴/明细 + 审计报告（ECharts 净值曲线）+ 信号体检 + API Key 面板
  - M2d 运维域：系统/通知中心/MCP 观测（ECharts 统计）/版本
- 学习点：Element Plus 表格/表单/抽屉；ECharts 绑定数据；Pinia 跨页状态；错误码→提示文案的统一映射。
- 验收：功能清单回归表逐项打勾；每页新旧面板并排核对（`/legacy` 对照）。

### M3 — 打磨与收口（"上架"）

- 做：空态/加载态/错误态统一；轮询合并与失焦降频；图表按需加载；响应式（手机浏览器基本可用）；`docs/webui-spa.md` 开发指南 + 各里程碑**导读笔记**（给你的学习材料）；README/版本 0.6.0；PR → CI → 镜像。
- 学习点：构建产物长什么样；按需引入与包体积；回归的完整流程。
- 验收：223+ 新测试全绿；CI 四 job 全绿；你 NAS 上 `pull && up -d` 后实测新面板 + 确认 `/legacy` 逃生通道可用。

---

## 10. 安全网（为什么这个重构敢上）

1. **API 零改动**：223 个 Python 测试全绿 = 后端行为与今天完全一致。
2. **功能清单回归表**：M2 开始前先列出旧面板全部功能，逐项核对，杜绝"重构丢功能"（上轮 Phase 4.5 就发生过丢功能，这次用清单制度防住）。
3. **`/legacy` 逃生通道 + `WEBUI_UI` 开关**：你任何时候一条 env 切回旧面板，NAS 上零停机回滚。
4. **双轨验证**：本地 `npm run dev`（代理到本地 webui）先验，镜像后验，NAS 实测最后。
5. **隐私不变量复查**：apikey 掩码、交易仅 webui、静态目录穿越防护，三项在新前端逐条复验。

---

## 11. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 重构丢功能（历史教训） | 高 | 功能清单回归表 + 新旧并排核对 |
| 静态服务引入路径穿越 | 低 | realpath 校验 + 测试用例 |
| 图表/组件库体积 | 低 | 局域网加载无压力；按需引入，首屏 <500KB 目标 |
| 上游 app.py 同步 | 低 | 前端已彻底独立，上游页面改动不再直接可用；后端 API 维持现状手动同步（与今日相同） |
| 学习成本 | — | 每个里程碑附导读笔记；本地 `npm run dev` 边看边改 |

---

## 12. 你的学习路径（配合实施）

- 你的 Mac 已有 Node v25.8.1 ✓，装依赖后 `cd stockdb-ai/spa && npm run dev` 即可边看边学。
- 每完成一个里程碑，我出一份《导读笔记》：这轮写了什么、每个文件干什么、关键概念对照表、你可以改哪里玩。
- 推荐外部资料（按里程碑配）：M0 看 Vite 官方文档前 3 节；M1 看 Vue 官方"快速上手+响应式基础"；M2 看 Element Plus 组件页 + ECharts 示例；M3 看"构建与部署"。

---

## 已确认

- [x] 框架选型：**Vue 3 + Vite + Element Plus + ECharts**（2026-08 用户拍板）
- [x] M0：spa/ 脚手架、app.py 静态服务（SPA 回退 + /legacy + WEBUI_UI + /api/overview）、
      Dockerfile 多阶段（node:22-alpine 构建期）、CI 新 job（test-spa + ops/paper 并入）、
      229 Python 测试 + 9 前端测试全绿、端到端冒烟通过。导读见 `docs/webui-spa/guide-m0.md`。
- [x] M1：布局基座（SideNav 分组导航 + StatusBar 顶栏状态条 + App 轮询/失焦降频）、
      Pinia 全局 store（/api/overview 单一数据源）、深色/浅色主题（localStorage 持久化）、
      StatCard/EmptyState/ThemeToggle 组件、nav.js 路由与侧边栏同源、
      前端测试 18 例全绿 + Python 229 全绿 + 端到端冒烟通过。导读见 `docs/webui-spa/guide-m1.md`。
- [x] M2+M3：十页搬迁（总览/数据同步/私有存储/模拟盘/审计报告*/信号体检*/系统/通知/MCP 观测/版本，
      *为旧面板缺失 UI 的补位页）+ api 四域封装 + EChart 按需封装 + 三态/危险确认/响应式打磨 +
      路由懒加载与 vendor 分包（index 主包 16KB）+ 功能清单回归表 +
      前端 56 例全绿 + Python 229 全绿 + 10 深链冒烟通过。导读见 `docs/webui-spa/guide-m2.md`。
- [x] 镜像低频策略：新增 `.github/workflows/test.yml` 轻量门禁（push/PR 只跑测试不建镜像）；
      镜像构建仅在 Phase 5 收口时手动触发一次。
