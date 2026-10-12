# -*- coding: utf-8 -*-
"""LLM 运行监控：JSON 指标接口 + 一个零依赖的内嵌 HTML 监控页。

- GET /api/v1/monitor/llm   → 指标 JSON（需管理员 Bearer Token）
- GET /api/v1/monitor       → 监控页（需管理员；浏览器无法带 Bearer 头，
  通过 ?token=<JWT> 校验，校验通过后页面内 fetch 自动携带 Authorization 头）
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_admin_user
from app.core.config import settings
from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User
from app.services.llm_stats import llm_stats

router = APIRouter(prefix="/monitor", tags=["monitor"])

_KIND_LABEL = {
    "timeout": "超时", "network": "网络错误", "rate_limit": "限流",
    "auth": "密钥无效", "balance": "余额不足", "bad_request": "参数错误",
    "server": "上游故障", "parse": "解析失败", "unknown": "未知",
    "not_configured": "未配置Key",
}


@router.get("/llm", summary="LLM 调用统计（JSON，需管理员）")
def llm_metrics(_admin: User = Depends(get_admin_user)):
    """返回进程启动以来的 LLM 调用统计与当前可靠性配置。"""
    snap = llm_stats.snapshot()
    snap["fail_by_kind_label"] = {_KIND_LABEL.get(k, k): v
                                  for k, v in snap["fail_by_kind"].items()}
    snap["config"] = {
        "model": settings.DEEPSEEK_MODEL,
        "base_url": settings.DEEPSEEK_BASE_URL,
        "api_key_configured": bool((settings.DEEPSEEK_API_KEY or "").strip()),
        "timeout_s": settings.LLM_TIMEOUT_S,
        "retry_times": settings.LLM_RETRY_TIMES,
        "retry_backoff_s": settings.LLM_RETRY_BACKOFF_S,
        "rate_limit_rpm": settings.LLM_RATE_LIMIT_RPM or "不限",
    }
    return {"code": 0, "msg": "ok", "data": snap}


_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>MockWise · LLM 监控</title>
<meta http-equiv="refresh" content="5">
<style>
  body{font:14px/1.6 -apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e8eaf0;
       max-width:960px;margin:0 auto;padding:24px}
  h1{font-size:18px} h2{font-size:14px;color:#9aa0ae;margin:22px 0 8px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .card{background:#1a1d24;border-radius:10px;padding:14px}
  .card .v{font-size:24px;font-weight:700} .card .n{font-size:12px;color:#9aa0ae}
  .ok{color:#3ecf8e}.bad{color:#f66}.warn{color:#e8b13f}
  table{width:100%;border-collapse:collapse;font-size:12px}
  td,th{padding:6px 8px;border-bottom:1px solid #262a33;text-align:left;vertical-align:top}
  th{color:#9aa0ae;font-weight:600}
  code{background:#262a33;padding:1px 6px;border-radius:4px}
</style></head><body>
<h1>MockWise · LLM 调用监控 <span id="t" style="color:#9aa0ae;font-size:12px"></span></h1>
<div class="grid" id="cards"></div>
<h2>失败分类</h2><div class="grid" id="kinds"></div>
<h2>当前配置</h2><div class="card" id="cfg" style="font-size:12px"></div>
<h2>最近错误（新→旧）</h2>
<table><thead><tr><th>时间</th><th>模块</th><th>类型</th><th>耗时</th><th>详情</th></tr></thead>
<tbody id="errs"></tbody></table>
<script>
const KIND_LABEL = {timeout:"超时",network:"网络错误",rate_limit:"限流",auth:"密钥无效",
  balance:"余额不足",bad_request:"参数错误",server:"上游故障",parse:"解析失败",unknown:"未知"};
async function load(){
  const r = await fetch("monitor/llm", {headers: {Authorization: "Bearer __TOKEN__"}});
  if (r.status === 401 || r.status === 403) {
    document.body.innerHTML = "<h2 style='color:#f66'>鉴权失败（" + r.status + "）：链接中的 token 已失效或不是管理员，请重新带 ?token= 访问。</h2>";
    return;
  }
  const j = await r.json(); const d = j.data;
  document.getElementById("t").textContent = "· 每 5s 自动刷新 · 启动 " + d.uptime_s + "s";
  const rate = d.success_rate == null ? "—" : (d.success_rate*100).toFixed(1)+"%";
  const cls = d.success_rate == null ? "" : (d.success_rate >= 0.9 ? "ok" : d.success_rate >= 0.7 ? "warn" : "bad");
  document.getElementById("cards").innerHTML = [
    ["总调用", d.total], ["成功率", rate, cls], ["降级次数", d.degraded, d.degraded?"warn":"ok"],
    ["重试过", d.retried_calls], ["平均耗时", (d.latency.avg_s??"—")+"s"], ["最大耗时", d.latency.max_s+"s"],
  ].map(([n,v,c]) => `<div class="card"><div class="v ${c||""}">${v}</div><div class="n">${n}</div></div>`).join("");
  const kinds = Object.entries(d.fail_by_kind);
  document.getElementById("kinds").innerHTML = kinds.length
    ? kinds.map(([k,v]) => `<div class="card"><div class="v bad">${v}</div><div class="n">${KIND_LABEL[k]||k}</div></div>`).join("")
    : '<div class="card">暂无失败 🎉</div>';
  document.getElementById("cfg").innerHTML = Object.entries(d.config)
    .map(([k,v]) => `<div><code>${k}</code> = ${v}</div>`).join("");
  document.getElementById("errs").innerHTML = d.recent_errors.length
    ? d.recent_errors.map(e => `<tr><td>${e.ts}</td><td>${e.caller}</td>
        <td class="bad">${KIND_LABEL[e.kind]||e.kind}</td><td>${e.latency_s}s</td>
        <td style="color:#9aa0ae">${e.detail}</td></tr>`).join("")
    : '<tr><td colspan="5" style="color:#9aa0ae">暂无错误记录</td></tr>';
}
load();
</script></body></html>"""


_FORBIDDEN_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>需要管理员权限</title></head>
<body style="font:14px sans-serif;background:#0f1115;color:#e8eaf0;display:flex;
justify-content:center;align-items:center;height:100vh;margin:0">
<div style="text-align:center">
<h2 style="color:#f66">403 · 需要管理员权限</h2>
<p style="color:#9aa0ae">请使用管理员账号登录后，通过 <code>?token=&lt;JWT&gt;</code> 访问本页。<br>
登录接口：POST /api/v1/auth/login（管理员 role=admin）</p>
</div></body></html>"""


@router.get("", response_class=HTMLResponse, summary="LLM 监控页（需管理员 ?token=）")
def llm_dashboard(token: str = Query(default=""), db: Session = Depends(get_db)):
    """浏览器无法携带 Bearer 头，这里改用 ?token=<JWT> 校验管理员；
    校验通过后将 token 注入页面，供页面内 fetch 以 Authorization 头访问 JSON 接口。"""
    user_id = decode_token(token) if token else None
    user = db.get(User, int(user_id)) if user_id else None
    if not user or user.status != "active" or getattr(user, "role", "user") != "admin":
        return HTMLResponse(_FORBIDDEN_PAGE, status_code=403)
    # JWT 仅含 base64url 字符，可安全内嵌进 JS 字符串
    return _PAGE.replace("__TOKEN__", token)
