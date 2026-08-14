import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
// 引入顺序固定：token → EP 基础样式 → EP 覆盖 → 全局样式（保证覆盖生效）
import './styles/tokens.css'
import 'element-plus/dist/index.css'
import './styles/element-overrides.css'
import * as Icons from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

// 全量注册图标，菜单/按钮按名字使用
for (const [name, comp] of Object.entries(Icons)) {
  app.component(name, comp)
}

app.use(ElementPlus, { locale: zhCn })
app.use(router)
app.mount('#app')
