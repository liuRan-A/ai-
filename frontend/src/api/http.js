import axios from 'axios'
import { message } from 'ant-design-vue'
import router from '@/router'

// 登出处理器由 main.js 装配注入（避免 http 静态依赖 store 形成循环依赖）
let logoutHandler = null
export function bindLogoutHandler(fn) {
  logoutHandler = fn
}

// 生产部署（Vercel）：VITE_API_BASE 指向后端公网地址，如 https://api.example.com
// 本地开发：不设置该变量，走 vite.config.js 的 /api 代理到 127.0.0.1:8000
const apiBase = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

const http = axios.create({
  baseURL: apiBase + '/api/v1',
  timeout: 15000,
})

// 请求拦截：附带 token
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('mockwise_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：统一拆 {code,msg,data}
let redirectingToLogin = false  // 401 跳转去重，避免并发请求被中止时重复弹错
http.interceptors.response.use(
  (resp) => {
    // 登录/注册成功：复位 401 跳转标志（同一会话内重新登录后恢复正常拦截）
    if (resp.config?.url?.includes('/auth/')) redirectingToLogin = false
    const body = resp.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 0) return body.data
      message.error(body.msg || '请求失败')
      return Promise.reject(new Error(body.msg || '请求失败'))
    }
    return body
  },
  (err) => {
    // 页面跳转中被浏览器中止的请求：静默 reject，不再弹错
    const aborted = !err.response && (
      err.code === 'ERR_NETWORK' || err.code === 'ECONNABORTED' ||
      /aborted|network/i.test(err.message || '')
    )
    if (redirectingToLogin || aborted) {
      return Promise.reject(err)
    }
    const status = err.response?.status
    const msg = err.response?.data?.detail || err.response?.data?.msg || err.message
    if (status === 401) {
      // 同步完成 store 登出 + 软跳转（不能插入 await：microtask 间隙会让并发
      // 401 重入、组件在半登出状态继续渲染，产生卸载竞态）
      logoutHandler?.()
      // vue-router 软跳转：不触发整页文档加载，不会中止进行中的 XHR
      // （原来的 location.href 硬跳转是 ERR_ABORTED /login 与 /admin 的总根源）
      const cur = router.currentRoute.value
      if (cur.path !== '/login') {
        redirectingToLogin = true
        message.error('登录已过期，请重新登录')
        router.replace({
          name: 'login',
          query: cur.fullPath && cur.fullPath !== '/' ? { redirect: cur.fullPath } : {},
        })
      }
    } else {
      message.error(msg || '网络异常')
    }
    return Promise.reject(err)
  }
)

export default http
