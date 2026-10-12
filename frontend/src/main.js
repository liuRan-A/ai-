import { createApp } from 'vue'
import { createPinia } from 'pinia'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import router from './router'
import { bindLogoutHandler } from './api/http'
import { useUserStore } from './store/user'
import './styles/global.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(Antd)
// 装配：401 时由拦截器触发 store 登出（回调注入，保持模块依赖无环）
bindLogoutHandler(() => useUserStore().logout())
app.mount('#app')
