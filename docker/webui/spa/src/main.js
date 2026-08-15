// 应用入口：创建 Vue 应用，挂载路由 / 状态库 / 组件库（中文语言包），
// 全局注册全部图标，并在挂载前完成主题初始化（避免首屏闪错配色）。
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue' // 全量图标包（命名导出）
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css' // Element Plus 深色变量（配合 html.dark）
import './styles/base.css'
import App from './App.vue'
import router from './router'

// —— 主题初始化（必须在挂载前做，避免首屏闪一下错误配色）——
// 读用户上次的选择（localStorage），没有记录则默认深色（与旧面板一致）。
const savedTheme = localStorage.getItem('webui-theme') ?? 'dark'
// html 根元素带不带 dark class，决定 base.css 里 :root / html.dark 哪套变量生效
document.documentElement.classList.toggle('dark', savedTheme === 'dark')

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

// 全局注册全部 Element Plus 图标：
// 循环调用 app.component(key, component)，之后任何模板都能
// 用 <el-icon><component :is="'图标名'" /></el-icon> 直接写图标名。
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.mount('#app')
