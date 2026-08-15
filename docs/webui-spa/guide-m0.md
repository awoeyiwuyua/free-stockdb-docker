# 导读笔记 M0 — 脚手架与双轨打通（"Hello SPA"）

> 配套里程碑：Phase 5 SPA 重构 M0
> 阅读对象：想跟着学的你
> 配套资料：Vite 官方文档前 3 节（https://cn.vitejs.dev/guide/）

## 这一轮做了什么

1. **前端源码工程** `docker/webui/spa/`：Vue 3 + Vite 项目，含 9 个路由占位页（M2 逐个实现）。
2. **后端静态服务**（`docker/webui/app.py`）：删掉了 1300 行的 PAGE 字符串，改为
   - `/` → SPA 的 `index.html`（未构建时自动兜底旧面板）
   - `/legacy` → 旧面板（原样保留，逃生通道）
   - `/assets/*` → 构建产物（带 immutable 缓存）
   - 非 API 路径 → SPA 回退（前端路由接管深链刷新）
   - `WEBUI_UI=legacy` 环境变量 → 根路径切回旧面板
   - 新增 `/api/overview`：总览页聚合接口（健康/告警/模拟盘/MCP/版本一次拿全）
3. **Dockerfile 多阶段**：`node:22-alpine` 只在构建期编译前端，最终镜像仍然只有 Python。
4. **CI**：新增 `test-spa` job（npm ci + Vitest + Vite build）；`test-mcp` job 补跑 test_ops/test_paper。

## 每个文件干什么

| 文件 | 作用 | 关键概念 |
|---|---|---|
| `spa/package.json` | 依赖清单 + 命令脚本（dev/build/test） | npm 的"购物清单" |
| `spa/vite.config.js` | 构建/开发配置，`/api` 代理到本地 webui | 开发时前后端怎么连 |
| `spa/index.html` | 浏览器入口，只有一个 `<div id="app">` | SPA 的一切都在这个 div 里长出来 |
| `spa/src/main.js` | 应用入口：创建 Vue 应用、装路由/状态/组件库 | createApp + use |
| `spa/src/App.vue` | 根组件（M0 只有顶栏 + 路由出口） | 单文件组件三件套 |
| `spa/src/router/index.js` | 网址 ↔ 页面 对照表（9 页） | 路由 = 导航的地图 |
| `spa/src/api/http.js` | 统一 fetch 封装（超时/错误） | 后端接口的"总机" |
| `spa/src/utils/format.js` | 展示格式化纯函数 + 单测 | 纯函数最好测 |
| `spa/src/views/PlaceholderView.vue` | 占位页（M2 会被真实页面替换） | useRoute 读当前路由 |

## 关键概念对照表

| 名词 | 白话 | 在本项目哪里 |
|---|---|---|
| SPA 单页应用 | 浏览器只加载一次 HTML，之后所有页面切换都由 JS 完成，永不整页刷新 | `/paper`、`/overview` 都是"假页面"，真实文件只有 index.html |
| 组件 | 可复用的界面积木（模板+逻辑+样式三合一） | `App.vue`、`PlaceholderView.vue` 都是组件 |
| 路由 | 网址与组件的对应关系 | `router/index.js` 里 9 条 path |
| 回退路由 | 后端收到不认识的非 API 路径时，统一返回 index.html，让前端路由接管 | app.py 的 `_serve_static` |
| 多阶段构建 | Docker 里先用一个镜像（node）干活，再把产物交给另一个镜像（python） | Dockerfile 的 `spa-build` 阶段 |
| 响应式 | 数据变了界面自动变（不用 getElementById） | M1 会大量用到 |

## 你可以怎么玩

```bash
cd docker/webui/spa
npm install                # 装依赖（你自己的电脑上直接跑就行）
npm run dev                # 打开 http://localhost:5173，改一行代码浏览器立刻变
npm run test               # 前端单测
npm run build              # 编译出 dist/（就是进镜像的那些文件）
```

改一改试试（改完浏览器立即生效，这就是"热更新"）：

1. 打开 `src/App.vue`，把 `旧面板（逃生通道）` 改成别的文字 → 保存 → 看浏览器。
2. 打开 `src/styles/base.css`，把 `--brand` 颜色改掉 → 看顶栏徽标颜色。
3. 访问 `http://localhost:5173/paper` 再刷新 → 页面不 404（SPA 回退 + 前端路由在干活）。
4. `npm run dev` 打开的是 5173 端口，但它能读后端数据（vite.config.js 里的 proxy 把 `/api` 转给了本地 webui 的 8080）。

## 验收记录（已通过）

- [x] Python 测试 229 全绿（223 旧 + 6 新：根路径/legacy/路径穿越/overview/ui_mode）
- [x] 前端 Vitest 9/9、`npm run build` 出 dist
- [x] 本地端到端冒烟：SPA 根路径 / 深链回退 / assets immutable 缓存 / index.html no-cache / 默认目录 legacy 兜底 / 路径穿越不泄露

## 下个里程碑（M1）

侧边栏布局 + 9 页骨架 + 顶栏状态条接真实数据 + Element Plus 主题（暗色/浅色）。
