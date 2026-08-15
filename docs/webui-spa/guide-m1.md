# 导读笔记 M1 — 布局骨架与全局状态（"控制台长这样了"）

> 配套里程碑：Phase 5 SPA 重构 M1
> 阅读对象：想跟着学的你
> 配套资料：Pinia 官方入门（https://pinia.vuejs.org/zh/introduction.html）、Element Plus 菜单/徽标文档（https://element-plus.org/zh-CN/）

## 这一轮做了什么

1. **三栏外壳**：App.vue 从 M0 的"一行顶栏"升级为 `SideNav 侧边栏 + StatusBar 顶栏 + 路由出口` 的完整控制台骨架，10 个占位页（1+2+3+4）全部挂进侧边栏 4 个分组。
2. **SideNav 侧边栏**：用 Element Plus 的 `el-menu` **router 模式**渲染——点菜单项 = 跳路由；菜单按 `NAV_GROUPS` 分组（总览/数据/模拟盘/运维），当前路由自动高亮，支持折叠成图标条。
3. **StatusBar 顶栏状态条**：接 `/api/overview` 聚合接口的真实数据——数据新鲜度（滞后天数着色）、模拟盘徽标（交易开启/已暂停/观察期/引擎缺失）、告警红点（`el-badge`）、MCP 健康率、webui 版本、最近刷新时间、秒级时钟。
4. **Pinia 全局状态**：`stores/global.js` 三件套（state/getters/actions）——一次请求 `/api/overview`，顶栏和总览页共用同一份数据；`api/overview.js` 把 5 次轮询合并成 1 次聚合请求。
5. **单一数据源**：`layout/nav.js` 是唯一导航配置——路由表（router/index.js）和侧边栏（SideNav.vue）都由它生成，改导航只改一个文件。
6. **主题**：根元素切 `html.dark` 类 + Element Plus 官方暗色 `css-vars.css`，全局换肤；ThemeToggle 一键切暗/浅色并用 `localStorage` 记住选择，刷新页面不丢。
7. **轮询与降频**：App.vue 用 `setInterval` 每 30s 拉一次 `/api/overview`；监听 `visibilitychange`——标签页被切走就降频到 5 分钟一次（省请求），切回来立刻补刷一次并恢复 30s。
8. **通用组件**：`StatCard`（指标卡片）/ `EmptyState`（空状态占位）抽出为 props 驱动的复用组件，后面 9 个页面直接用。

## 每个文件干什么

| 文件 | 作用 | 关键概念 |
|---|---|---|
| `src/layout/nav.js` | 导航唯一配置源：4 组 10 页（标题/路径/图标），导出 `NAV_GROUPS` 与展平的 `NAV_ITEMS` | 单一数据源 |
| `src/layout/SideNav.vue` | 侧边栏：`el-menu` router 模式 + 分组渲染，`collapsed` prop 控制折叠 | props / router 模式 / 全局图标 |
| `src/layout/StatusBar.vue` | 顶栏状态条：滞后着色、模拟盘徽标、告警红点、MCP、版本、时钟 | computed 派生 / setInterval 清理 |
| `src/components/ThemeToggle.vue` | 暗/浅主题切换按钮，`localStorage` 持久化选择 | html.dark / 持久化 |
| `src/components/StatCard.vue` | 指标卡片（标题/数值/副文案/着色），props 传参 | props |
| `src/components/EmptyState.vue` | 空状态占位（图标+文案），props + slot | props / slot |
| `src/stores/global.js` | Pinia 全局仓库：缓存 `/api/overview`，getters 派生出顶栏要的字段 | state / getters / actions |
| `src/api/overview.js` | `/api/overview` 聚合接口封装，一个请求拿全健康/告警/模拟盘/MCP/版本 | 后端聚合替代 5 次轮询 |
| `src/router/index.js` | 从 `NAV_ITEMS` 生成路由表，`afterEach` 改浏览器标题 | 单一数据源 / 路由 meta |
| `src/main.js` | 挂载前按 localStorage 初始化主题；注册 Element Plus + 中文语言包；循环全局注册全部图标 | 全局注册 / 挂载前初始化 |
| `src/styles/base.css` | 主题 CSS 变量（`--bg/--panel/--line/--ok/--err/--brand` 等）+ `html.dark` 兜底 | CSS 变量 |
| `src/App.vue` | 组装外壳：放 SideNav/StatusBar/路由出口；30s 轮询 + 失焦降频 | 轮询 / visibilitychange |

> `/api/overview` 字段速记（后端聚合好一次给全）：
> `health{latest,lag_days,status}` · `alerts{count,recent}` · `paper{trading_enabled,paused,engine_available,modules_ok}` · `mcp{total,ok_rate,avg_ms,p95_ms}` · `version{webui{version},stale,msg,ui_mode}`

## 关键概念对照表

| 名词 | 白话 | 在本项目哪里 |
|---|---|---|
| Pinia | 跨组件共享的响应式"仓库"：任何组件都能读同一份数据，改一处全界面跟着变 | `stores/global.js` 的 `defineStore('global', …)` |
| state | 仓库里"存数据"的地方，只有 actions 能改它 | `global.js` 的 `overview/error/lastRefresh` |
| getter | 从 state **派生**出来的只读计算值，带缓存，依赖没变就不重算 | `global.js` 的 `lagDays`、StatusBar 的 `lagClass` |
| action | 仓库里的"方法"，负责取数据/改 state，组件只管调用 | `global.js` 的 `refresh()` |
| 轮询与降频 | 定时器每隔 N 秒拉一次新数据；页面没人看时把频率降下来省钱省电 | App.vue 30s 轮询，失焦降频 |
| visibilitychange | 浏览器事件：标签页被切走/切回来时触发，告诉页面"你可见性变了" | App.vue 用它暂停/恢复轮询 |
| 单一数据源 | 同一份配置只写一处，所有地方从它生成，杜绝两处配置对不上 | `nav.js` → 路由表 + 侧边栏 |
| 组件 props | 父组件给子组件传参数的"接口"，子组件声明要什么，父组件给什么 | SideNav 的 `collapsed`、StatCard 的 `label/value/tone` |
| 全局注册图标 | 在入口一次性装好图标库，模板里直接写名字用，不用每个组件都 import | `main.js` + `<component :is="图标名" />` |
| html.dark | 给 `<html>` 加 `dark` 类，配合 Element Plus 暗色变量整站换肤 | ThemeToggle 切换 |
| computed | 依赖数据变了才重算的"自动计算属性"，是 Vue 响应式的核心用法 | StatusBar 的 `lagClass`/`paperTag` |

## 你可以怎么玩

```bash
cd docker/webui/spa
npm install        # 装依赖（第一次或换机器后）
npm run dev        # 打开 http://localhost:5173，热更新
npm run test       # 前端单测
npm run build      # 编译出 dist/
```

改一改试试（保存后浏览器立即生效）：

1. **切主题看全局变化**：点顶栏的 ThemeToggle 按钮切浅色——注意侧边栏、顶栏、卡片、菜单高亮全部跟着变。想看原理，搜一下 `components/ThemeToggle.vue` 里切换 `html.dark` 和写 `localStorage` 的两行。
2. **把 30s 轮询改成 5s**：在 `App.vue` 找到 `POLL_FAST = 30_000` 这一行，改成 `5000` → 保存 → 把顶栏的"刷新 时:分"和时钟对比，数据刷新明显变勤快。改完记得改回来；再试试切走标签页，观察 `POLL_SLOW`（5 分钟）怎么接管。
3. **在 nav.js 加一个新导航项**：往 `NAV_GROUPS` 任意分组里加 `{ path: '/hello', title: '你好', icon: 'Star' }` → 保存 → 刷新页面，侧边栏和路由**同时**多出 `/hello`（访问 http://localhost:5173/hello 也能打开）——这就是单一数据源：改一处，两处生效。
4. **看 props 流动**：点顶栏的折叠按钮，侧边栏收起成图标条——`collapsed` 这个值从 StatusBar（emit）→ App.vue → SideNav（props），跟着这条链路读一遍三个文件，组件通信就入门了。
5. **拔掉后端看降级**：关掉 webui 进程，顶栏出现"接口异常"红字、数据列变 `—`，但页面不崩——观察 store 的 `error` 字段怎么兜底。

## 验收记录（已全部通过）

- [x] 前端测试全绿（18 例 / 3 文件）
- [x] npm run build 通过
- [x] Python 229 全绿
- [x] 端到端冒烟通过（app.py 静态服务 + vite dev 双通道）

## 下个里程碑（M2）

10 个页面逐个替换占位页：总览页仪表盘（StatCard 拼指标、lag 着色、告警列表）、数据同步页、模拟盘页、运维各页——继续复用 M1 的 store 数据流、StatCard/EmptyState 组件与主题体系。
