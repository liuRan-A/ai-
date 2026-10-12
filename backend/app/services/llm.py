# -*- coding: utf-8 -*-
"""
DeepSeek 大模型服务：面试作答评分 + 实时反馈/追问

设计原则：
1. 密钥只在后端 .env 配置，前端永不接触；
2. 任何异常（未配置 Key / 网络超时 / 限流 / 返回格式错误）都返回 None，
   由调用方降级到规则评分，保证「提交作答 → 出分 → 生成报告」业务闭环不被外部服务打断；
3. 严格 JSON 输出（response_format=json_object），并做字段校验与 clamp。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx

from app.core.config import settings
from app.services.llm_stats import (
    LLMErrorKind, RETRYABLE, classify_exception, classify_http_error, llm_stats,
)

logger = logging.getLogger(__name__)

DIMENSIONS = ["专业深度", "逻辑结构", "语言表达", "应急应变", "岗位匹配"]

# —— 评分用系统提示 ——
_SCORE_SYS = """你是一名有 10 年经验的资深面试评委，正在评估候选人的模拟面试作答。
请严格按以下要求输出（必须是 JSON，不要输出任何多余文字）：
{
  "total_score": 0-100 的数字,  // 本题综合得分
  "dim_scores": {"专业深度": 0-100数字, "逻辑结构": 数字, "语言表达": 数字, "应急应变": 数字, "岗位匹配": 数字},
  "dim_comments": {"专业深度": "12字以内简短评语", "逻辑结构": "...", "语言表达": "...", "应急应变": "...", "岗位匹配": "..."},
  "understanding": "一句话概括你对候选人回答的理解（30-60字）",
  "strengths": [
    {"point": "答得好的地方（20字内，说清楚好在哪）", "quote": "对应的回答原句或精炼概括（40字内）"}
  ],
  "gaps": [
    {"point": "欠缺/答得不好的地方（20字内）", "why": "为什么这是问题（30字内）", "how": "具体怎么改（35字内）"}
  ],
  "highlights": ["回答中真实存在的亮点原句或精炼概括，2-3条，每条不超过40字"],
  "suggestions": ["针对性改进建议，2-3条，每条不超过40字"],
  "followup": "一道基于回答细节的追问问题（半结构化面试用，25字以内）"
}
评分标准：跑题/内容空洞 45-59；基本切题但缺细节 60-69；结构完整有案例 70-79；
有数据/有深度/逻辑清晰 80-89；非常出色 90-95。分数必须与回答质量匹配，禁止全部给高分。

strengths 与 gaps 的要求（这两项是复盘核心，务必认真区分）：
- strengths 写「确实做得对的点」，每条都要能对应到回答中的具体内容，禁止空泛夸奖；
- gaps 写「明确没做到位的点」，必须指出为什么是问题、以及具体怎么补；
- 若回答整体优秀，gaps 也要给出 1 条可继续拔高的点，不要留空；
- 若回答整体较差，strengths 至少保留 1 条真实存在的可取之处（哪怕只是态度或方向对）。
所有内容必须来自候选人的真实回答，不要编造。"""

# —— 实时反馈用系统提示（不打分，更快）——
_FEEDBACK_SYS = """你是一名面试教练，正在听候选人作答。请严格输出 JSON：
{
  "understanding": "一句话复述候选人表达的核心观点（30-60字）",
  "highlights": ["回答中的亮点，1-3条，每条不超过40字，必须来自真实回答"],
  "suggestions": ["可立即改进的建议，1-3条，每条不超过40字"],
  "followup": "一道顺着回答细节的追问（25字以内）",
  "dim_scores": {"专业深度": 0-100数字, "逻辑结构": 数字, "语言表达": 数字, "应急应变": 数字, "岗位匹配": 数字}
}
不要输出 JSON 以外的任何文字。"""


def _chat_json(system_prompt: str, user_prompt: str, timeout: Optional[int] = None,
               temperature: float = 0.3, max_tokens: int = 1200,
               caller: str = "unknown") -> Optional[dict]:
    """调用 DeepSeek chat 接口并解析 JSON。任何失败返回 None（调用方降级规则）。

    错误处理策略（分类见 llm_stats.LLMErrorKind）：
    - 未配置 Key            → 直接返回 None，不计入故障
    - 本地限流超限           → 直接拒绝，记 RATE_LIMIT，保护上游配额
    - 超时/断连/429/5xx     → 按 LLM_RETRY_TIMES 有界重试，指数退避；429 尊重 Retry-After（封顶 10s）
    - 401/402/400           → 不重试（密钥/余额/参数问题重试无意义），记日志后降级
    - JSON 解析失败          → 不重试，记 PARSE 后降级
    """
    api_key = (settings.DEEPSEEK_API_KEY or "").strip()
    if not api_key:
        logger.info("[llm] DEEPSEEK_API_KEY 未配置，跳过模型调用")
        return None

    if not llm_stats.allow(settings.LLM_RATE_LIMIT_RPM):
        logger.warning("[llm][%s] 本地限流拦截（%s RPM），本次调用直接降级", caller, settings.LLM_RATE_LIMIT_RPM)
        llm_stats.record(caller=caller, ok=False, latency_s=0,
                         kind=LLMErrorKind.RATE_LIMIT, detail="local rpm cap")
        return None

    url = settings.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": settings.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }

    max_attempts = 1 + max(0, settings.LLM_RETRY_TIMES)
    last_kind, last_detail = LLMErrorKind.UNKNOWN, ""
    t0 = time.time()
    retried = False

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            retried = True
            time.sleep(settings.LLM_RETRY_BACKOFF_S * (attempt - 1))
        try:
            with httpx.Client(timeout=timeout or settings.LLM_TIMEOUT_S) as client:
                resp = client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                data = _safe_json(content)
                if data is not None:
                    llm_stats.record(caller=caller, ok=True,
                                     latency_s=time.time() - t0, retried=retried)
                    return data
                last_kind, last_detail = LLMErrorKind.PARSE, (content or "")[:150]
                break                                    # 解析失败重试无意义

            last_kind = classify_http_error(resp.status_code, resp.text)
            last_detail = f"HTTP {resp.status_code}: {resp.text[:150]}"
            # 429 且带 Retry-After：按服务端要求等待（封顶 10s），替代固定退避
            if resp.status_code == 429 and attempt < max_attempts:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(float(retry_after), 10.0))
            if last_kind not in RETRYABLE:
                break                                    # 密钥/余额/参数错误，立即放弃

        except Exception as e:
            last_kind = classify_exception(e)
            last_detail = f"{type(e).__name__}: {e}"[:200]
            if last_kind not in RETRYABLE:
                break

    latency = time.time() - t0
    log = logger.warning if last_kind in (LLMErrorKind.AUTH, LLMErrorKind.BALANCE,
                                          LLMErrorKind.BAD_REQUEST) else logger.info
    log("[llm][%s] DeepSeek 失败（%s），降级规则: %s", caller, last_kind.value, last_detail)
    llm_stats.record(caller=caller, ok=False, latency_s=latency,
                     kind=last_kind, detail=last_detail, retried=retried)
    return None


def _safe_json(content: str) -> Optional[dict]:
    """模型偶尔会在 JSON 外包裹 markdown，做容错提取"""
    if not content:
        return None
    try:
        return json.loads(content)
    except Exception:
        pass
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        return json.loads(content[start:end])
    except Exception:
        return None


def _clamp(v, lo=40.0, hi=96.0, default=65.0) -> float:
    try:
        f = float(v)
        return round(max(lo, min(hi, f)), 1)
    except Exception:
        return default


def _norm_dim_scores(raw: dict | None) -> dict:
    out = {}
    raw = raw or {}
    for d in DIMENSIONS:
        out[d] = _clamp(raw.get(d), lo=40, hi=96, default=65)
    return out


def _norm_str_list(raw, limit=3, max_len=40) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        s = str(item).strip().replace("\n", " ")
        if s:
            out.append(s[:max_len])
        if len(out) >= limit:
            break
    return out


def _norm_point_list(raw, limit=3) -> list[dict]:
    """规范化「答得好 / 欠缺」这类带说明的对象数组。
    兼容两种模型输出：对象数组 [{"point":..}] 或纯字符串数组 ["…"]。"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            d = {k: str(v).strip().replace("\n", " ") for k, v in item.items() if v}
            if d.get("point"):
                out.append(d)
        else:
            s = str(item).strip().replace("\n", " ")
            if s:
                out.append({"point": s[:60]})
        if len(out) >= limit:
            break
    return out


def score_answer(
    question_content: str,
    dimension: str,
    category: str,
    transcript: str,
    form_type: str = "structured",
) -> Optional[dict]:
    """
    大模型评分。返回：
    {
      "total_score": float,
      "dim_scores": {dim: score},
      "dim_comments": {dim: comment},
      "understanding": str,
      "highlights": [str],
      "suggestions": [str],
      "followup": str,
      "engine": "deepseek"
    }
    失败返回 None。
    """
    if not transcript or len(transcript.strip()) < 8:
        return None

    form_label = {"structured": "结构化面试", "group": "无领导小组讨论", "semi": "半结构化面试"}.get(form_type, "结构化面试")
    user_prompt = (
        f"【面试形式】{form_label}\n"
        f"【题目类别】{category or '通用'}\n"
        f"【考察维度】{dimension or '综合'}\n"
        f"【面试题目】{question_content or '（见候选人作答）'}\n"
        f"【候选人作答原文】\n{transcript[:2500]}\n"
        f"请以评委身份评分并输出 JSON。"
    )
    data = _chat_json(_SCORE_SYS, user_prompt, caller="score")
    if not data:
        return None

    dim_scores = _norm_dim_scores(data.get("dim_scores"))
    total = _clamp(data.get("total_score"), lo=40, hi=96,
                   default=round(sum(dim_scores.values()) / len(dim_scores), 1))

    comments_raw = data.get("dim_comments") or {}
    dim_comments = {}
    for d in DIMENSIONS:
        c = str(comments_raw.get(d) or "").strip()
        dim_comments[d] = c[:24] if c else f"{d}表现{('优秀' if dim_scores[d] >= 80 else '良好' if dim_scores[d] >= 70 else '待提升')}"

    return {
        "total_score": total,
        "dim_scores": dim_scores,
        "dim_comments": dim_comments,
        "understanding": str(data.get("understanding") or "").strip()[:120] or "候选人围绕题目给出了作答，具体表现见各维度评分。",
        "strengths": _norm_point_list(data.get("strengths"), limit=3),
        "gaps": _norm_point_list(data.get("gaps"), limit=3),
        "highlights": _norm_str_list(data.get("highlights"), limit=3, max_len=40),
        "suggestions": _norm_str_list(data.get("suggestions"), limit=3, max_len=40),
        "followup": str(data.get("followup") or "").strip()[:60],
        "engine": "deepseek",
    }


def analyze_answer(
    transcript: str,
    dimension: str = "",
    question_content: str = "",
) -> Optional[dict]:
    """大模型实时反馈（理解/亮点/建议/追问）。失败返回 None。"""
    if not transcript or len(transcript.strip()) < 8:
        return None
    user_prompt = (
        f"【考察维度】{dimension or '综合'}\n"
        f"【面试题目】{question_content or '（通用面试题）'}\n"
        f"【候选人作答】\n{transcript[:2000]}\n"
        f"请输出反馈 JSON。"
    )
    data = _chat_json(_FEEDBACK_SYS, user_prompt, timeout=30, caller="feedback")
    if not data:
        return None
    return {
        "understanding": str(data.get("understanding") or "").strip()[:120] or "候选人已表达核心观点。",
        "highlights": _norm_str_list(data.get("highlights"), limit=3, max_len=40),
        "suggestions": _norm_str_list(data.get("suggestions"), limit=3, max_len=40),
        "followup": str(data.get("followup") or "").strip()[:60],
        "dim_scores": _norm_dim_scores(data.get("dim_scores")),
        "engine": "deepseek",
    }


# —— 简历分析 ——
_RESUME_SYS = """你是一名资深 HR 和技术面试官。请分析候选人简历，严格输出 JSON（不要多余文字）：
{
  "candidate_name": "姓名（简历中没有则空字符串）",
  "target_position": "求职意向/目标岗位",
  "years_exp": "工作年限数字（字符串，如 3）",
  "summary": "候选人画像概述（60-100字，含背景、核心优势）",
  "skills": ["核心技能3-6项"],
  "projects": ["代表项目/经历，2-4项，每项含角色与成果，不超过50字"],
  "highlights": ["简历亮点2-3项"],
  "weaknesses": ["可能被面试官追问或质疑的薄弱点2-3项"],
  "suggested_questions": ["基于简历内容的个性化面试题5-6道，围绕项目细节、技术决策、成果真实性、岗位匹配，每题不超过45字"]
}
suggested_questions 必须具体引用简历中的项目/技能/数据，不要泛泛而谈。"""

_RESUME_RULE_SKILLS = ["项目管理", "数据分析", "沟通协作", "问题解决", "团队协作"]


def analyze_resume(resume_text: str) -> Optional[dict]:
    """大模型分析简历生成画像。失败返回 None（调用方做规则兜底）。"""
    if not resume_text or len(resume_text.strip()) < 20:
        return None
    data = _chat_json(_RESUME_SYS, f"【候选人简历】\n{resume_text[:4000]}\n请输出画像 JSON。", timeout=45, caller="resume")
    if not data:
        return None
    return {
        "candidate_name": str(data.get("candidate_name") or "").strip()[:32],
        "target_position": str(data.get("target_position") or "").strip()[:64],
        "years_exp": str(data.get("years_exp") or "").strip()[:8],
        "summary": str(data.get("summary") or "").strip()[:200] or "候选人简历已解析。",
        "skills": _norm_str_list(data.get("skills"), limit=6, max_len=24),
        "projects": _norm_str_list(data.get("projects"), limit=4, max_len=60),
        "highlights": _norm_str_list(data.get("highlights"), limit=3, max_len=50),
        "weaknesses": _norm_str_list(data.get("weaknesses"), limit=3, max_len=50),
        "suggested_questions": _norm_str_list(data.get("suggested_questions"), limit=6, max_len=60),
        "engine": "deepseek",
    }


def rule_analyze_resume(resume_text: str) -> dict:
    """规则兜底画像（DeepSeek 不可用时）。题目必须引用简历真实内容（项目/技能/量化数据），避免泛泛模板。"""
    text = resume_text or ""
    # 简单技能关键词命中
    skill_kw = {
        "Python": ["python"], "Java": ["java"], "前端": ["vue", "react", "前端", "javascript"],
        "数据分析": ["数据分析", "sql", "报表"], "项目管理": ["项目管理", "负责", "主导"],
        "AI/算法": ["ai", "算法", "模型", "机器学习", "大模型"],
        "运营": ["运营", "增长", "用户"], "产品": ["产品", "需求", "原型"],
    }
    skills = [name for name, kws in skill_kw.items() if any(k in text.lower() for k in kws)]

    # 目标岗位 / 年限提取
    target = ""
    m = re.search(r"求职意向[:：]\s*([^\s，,。；;]{2,20})", text)
    if m:
        target = m.group(1)
    years = ""
    m = re.search(r"(\d+)\s*年[^。\n]{0,6}(经验|工作)", text)
    if m:
        years = m.group(1)

    # 项目/经历行：优先含动作词的行
    lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 12]
    proj_kws = ["项目", "负责", "主导", "设计", "搭建", "优化", "上线", "系统", "开发", "实现", "建设"]
    proj_lines = [l for l in lines if any(k in l for k in proj_kws)] or lines
    projects = proj_lines[:3] if proj_lines else ["（简历中未提取到结构化项目经历）"]

    # 量化数据（如 "召回率提升15%" "62%提升到78%"）
    nums = [n[:16] for n in re.findall(r"[\u4e00-\u9fa5A-Za-z]{1,10}?\d+(?:\.\d+)?(?:%|万|倍)", text)][:2]

    name = ""
    for line in text.split("\n")[:5]:
        m = re.search(r"([\u4e00-\u9fa5]{2,4})(?=\s|$|简历|的|，|,)", line.strip())
        if m and len(m.group(1)) >= 2:
            name = m.group(1)
            break

    # —— 基于简历内容的个性化问题（引用真实项目/技能/数据） ——
    p1 = _proj_short(projects[0])
    qs = [f"你在简历里写到「{p1}」，请讲讲你承担的具体角色和最大的挑战？"]
    if nums:
        qs.append(f"简历中提到「{nums[0]}」，这个数字是怎么测出来的？如何证明是你的贡献？")
    if skills:
        qs.append(f"你提到熟悉{skills[0]}，说说你用它解决过的最棘手的一个问题？")
    if len(projects) > 1:
        qs.append(f"关于「{_proj_short(projects[1])}」，如果让你重新做一次，你会改进哪一点？")
    if len(nums) > 1:
        qs.append(f"「{nums[1]}」这个结果花了多久达成？中间遇到的最大阻力是什么？")
    qs.append("这些经历里最有成就感的一件事是什么？它如何支撑你的求职目标？")
    # 兜底补足 5 题
    fallback_qs = [
        "请用 STAR 法则展开你简历中最核心的一段经历。",
        "简历中团队协作的角色分工是怎样的？出现分歧时你如何推进？",
    ]
    for f in fallback_qs:
        if len(qs) >= 5:
            break
        qs.append(f)

    return {
        "candidate_name": name,
        "target_position": target,
        "years_exp": years,
        "summary": (f"{'约 ' + years + ' 年经验、' if years else ''}{'目标岗位 ' + target + '。' if target else ''}"
                    f"已基于简历关键词生成画像（规则引擎），核心技能：{'、'.join(skills[:4]) if skills else '见简历'}。"),
        "skills": skills or _RESUME_RULE_SKILLS[:4],
        "projects": projects,
        "highlights": (["有可量化的项目成果" if nums else "具备相关岗位项目经历"]
                       + [f"掌握 {skills[0]}" if skills else "简历结构完整"])[:2],
        "weaknesses": (["项目成果的数据口径可能被追问"] if nums else []) +
                      ["经历集中于单一公司/方向，转型动机会被问", "部分技能缺少项目佐证"],
        "suggested_questions": qs[:6],
        "engine": "rule",
    }


def _proj_short(s: str, n: int = 18) -> str:
    """从简历行中截取可读的项目引用片段（去序号/标签前缀，优先从动作词截取）"""
    s = re.sub(r"^\d+[.、）)]\s*", "", (s or "").strip())
    s = re.sub(r"^[\u4e00-\u9fa5A-Za-z]{1,8}[:：]", "", s)  # 去掉「工作经历：」「项目：」等前缀
    for kw in ["主导", "独立设计", "负责", "设计", "搭建", "建设", "开发", "优化", "上线", "项目"]:
        i = s.find(kw)
        if i >= 0:
            s = s[i:]
            break
    else:
        s = re.sub(r"\d{4}\s*[-—至/年.]\s*[\d]{0,4}\s*[-—至/月]?\s*[\d月]{0,4}", "", s).strip() or s
    return s[:n].strip()


def _resume_ref_answer(content: str, profile: dict) -> str:
    """为简历动态题生成参考答案要点"""
    projects = profile.get("projects") or [""]
    proj = _proj_short(str(projects[0]))
    if "数据" in content or "证明" in content or "数字" in content:
        body = ("说明统计口径与基线 → 拆解你的具体贡献边界 → 给出测量方法（如 A/B、埋点、监控大盘）"
                f"→ 举一个数据背后的具体动作（结合「{proj}」）。")
    elif "角色" in content or "挑战" in content or "承担" in content:
        body = (f"STAR 展开——情境：交代「{proj}」的背景与目标；任务：明确你的角色与职责边界；"
                "行动：分 2-3 步讲关键决策与执行细节；结果：量化成果 + 复盘改进。")
    elif "改进" in content or "重新" in content:
        body = ("先肯定原方案的合理性 → 指出 1-2 个真实的局限（数据/成本/扩展性）→ 给出改进方案与预期收益，"
                "体现反思深度而不是全盘否定。")
    elif "技能" in content or "棘手" in content or "熟悉" in content:
        body = ("选一个真实难题：问题现象 → 排查过程 → 你的解决方案 → 最终效果（含量化数据），"
                "突出该技能在其中的关键作用。")
    else:
        body = "结论先行 → 分点展开（每点配案例或数据）→ 总结回扣问题，控制在 2 分钟内。"
    return f"参考答案要点：{body}"


# —— 参考答案生成 ——
_REF_ANSWER_SYS = """你是一名资深面试官，正在为模拟面试题撰写「标准参考答案」，供考生对照复盘。
请严格输出 JSON（不要多余文字）：
{
  "key_points": ["面试官最想听到的采分点，3-5条，每条25字内，必须紧扣本题"],
  "outline": ["推荐的答题框架，3-4步，每条30字内，体现先后顺序"],
  "sample": "一段完整的示范作答，250-400字，第一人称、口语化，像真人在面试现场说出来，要贴合本题场景",
  "common_traps": ["本题常见失分点，2-3条，每条30字内"]
}
要求：
1. 所有内容必须围绕【面试题目】展开，禁止输出通用模板；
2. key_points 是评分时的给分依据，要具体、可检验；
3. sample 要能直接朗读，有细节、有数据占位（如「提升了约 20%」），不要写成提纲；
4. 若题目来自候选人简历，示范作答要贴合其背景。"""


def _norm_ref_answer(data: dict | None) -> dict | None:
    """校验参考答案结构：四项至少要有实质内容才算有效"""
    if not isinstance(data, dict):
        return None
    key_points = _norm_str_list(data.get("key_points"), limit=5, max_len=60)
    outline = _norm_str_list(data.get("outline"), limit=4, max_len=70)
    sample = str(data.get("sample") or "").strip()
    traps = _norm_str_list(data.get("common_traps"), limit=3, max_len=70)
    if not key_points or len(sample) < 40:
        return None
    return {
        "key_points": key_points,
        "outline": outline,
        "sample": sample[:900],
        "common_traps": traps,
        "engine": "deepseek",
    }


def generate_reference_answer(
    question_content: str,
    category: str = "",
    dimension: str = "",
    form_type: str = "structured",
    resume_profile: dict | None = None,
) -> dict:
    """
    生成结构化参考答案：采分点 / 答题框架 / 示范作答 / 常见失分点。
    大模型优先，失败降级规则生成（保证永不返回 None，业务不中断）。
    """
    if question_content and len(question_content.strip()) >= 6:
        profile_hint = ""
        if resume_profile:
            skills = resume_profile.get("skills") or []
            projects = resume_profile.get("projects") or []
            if skills or projects:
                profile_hint = (
                    f"\n【候选人背景】技能：{'、'.join(str(s) for s in skills[:4])}；"
                    f"经历：{str(projects[0])[:60] if projects else '（无）'}"
                )
        form_label = {"structured": "结构化面试", "group": "无领导小组讨论",
                      "semi": "半结构化面试"}.get(form_type, "结构化面试")
        user_prompt = (
            f"【面试形式】{form_label}\n"
            f"【题目类别】{category or '通用'}\n"
            f"【考察维度】{dimension or '综合'}\n"
            f"【面试题目】{question_content}\n"
            f"{profile_hint}\n"
            f"请输出参考答案 JSON。"
        )
        try:
            parsed = _norm_ref_answer(_chat_json(_REF_ANSWER_SYS, user_prompt, timeout=45, caller="ref_answer"))
            if parsed:
                return parsed
        except Exception as e:
            logger.warning("[llm] 参考答案生成失败，降级规则: %s", e)
    return _rule_reference_answer(question_content, category, dimension)


def _topic_of(content: str, n: int = 14) -> str:
    """从题目里提炼一个能自然嵌进示范作答的短主题。
    例：「如果线上服务突然出现大量超时，你会怎么处理？」→「线上服务突然出现大量超时」"""
    s = re.sub(r"^[0-9]+[.、）)]\s*", "", (content or "").strip())
    s = re.sub(r"^(?:如果|假如|假设|请问|请|谈谈|谈一谈|说说|说一说|讲讲|讲一讲"
               r"|描述一下|描述|你)+", "", s)
    s = re.sub(r"(你会怎么|你怎么|该怎么|如何处理|怎么处理|怎么办|怎么应对|怎么解决"
               r"|有什么思路|有哪些思路|是什么|有哪些|请展开|请说明).*$", "", s)
    s = s.strip(" ，。？?！!、,;；:：")
    return (s[:n].strip() or "这类问题")


def _rule_reference_answer(question_content: str, category: str, dimension: str) -> dict:
    """规则兜底参考答案：按题目类别/维度给出差异化的采分点、框架、示范与失分点"""
    content = question_content or ""
    cat = category or ""
    dim = dimension or ""

    # 从题目里提炼一个短主题，让示范作答读起来是「针对这道题」的，而不是套模板
    topic = _topic_of(content)

    if "自我介绍" in cat or "自我介绍" in content or "介绍你自己" in content:
        key_points = [
            "30 秒内交代身份与核心方向，不背简历",
            "挑 2 个与目标岗位强相关的代表作",
            "每个成果带一个可验证的量化结果",
            "结尾一句话点明与岗位的匹配点",
        ]
        outline = ["背景定位（身份 + 方向 + 年限）", "代表作一（角色 + 动作 + 数据）",
                   "代表作二（难点 + 解法 + 结果）", "回扣岗位：为什么我适合"]
        sample = (
            "面试官您好，我是 X，做 Y 方向 N 年，主要在 A 和 B 两块。\n"
            "最近一段我主导了 ____ 这个项目：当时面临的核心是 ____，"
            "我把它拆成三步推进——先 ____，再 ____，最后 ____，"
            "最终把核心指标从 ____ 提升到 ____，大概提升了 20%。\n"
            "另外我还做过 ____，沉淀了一套可复用的 ____ 方法。\n"
            "我关注到贵岗位需要 ____，这正好和我前面两段经历吻合，所以很想深入聊聊。"
        )
        traps = ["把简历从头背一遍，没有重点", "成果没有数据，全是形容词", "自我介绍超过 2 分钟，抢了后续提问时间"]

    elif "简历深挖" in cat or "项目" in cat or "项目" in content or "经历" in cat:
        key_points = [
            "说清项目背景与目标，界定问题边界",
            "明确「我」的角色与职责边界，区分团队贡献",
            "讲关键决策的取舍理由，而不只是做了什么",
            "给出可验证的量化结果与复盘反思",
        ]
        outline = ["情境 S：项目背景与目标", "任务 T：我的角色与职责边界",
                   "行动 A：2-3 个关键决策与执行细节", "结果 R：量化成果 + 复盘改进"]
        sample = (
            "这个项目（%s）当时的背景是 ____，目标是 ____，我负责的是 ____ 这一块。\n"
            "最大的难点在 ____。我评估后有两个方案：方案一 ____ 成本低但扩展性差，"
            "方案二 ____ 前期投入大但能支撑后续 ____，我选了方案二，理由是 ____。\n"
            "执行上分三步：先 ____，再 ____，最后 ____。中间踩过的坑是 ____，我通过 ____ 解决。\n"
            "最终结果是 ____，核心指标提升了约 ____%%。如果重来一次，我会在 ____ 上更早 ____。"
        ) % topic
        traps = ["全程说「我们」，说不清个人贡献", "只讲流程不讲决策取舍", "结果没有数据或数据口径经不起追问"]

    elif "情景" in cat or dim == "应急应变" or "突发" in content or "怎么办" in content:
        key_points = [
            "第一时间止血：先控制影响面，不急着找根因",
            "快速评估影响范围与优先级，给出判断依据",
            "给出具体可执行的处置步骤与时间预期",
            "事后复盘沉淀机制，避免同类问题再发",
        ]
        outline = ["止血（降级/熔断/回滚，先恢复服务）", "定位根因并评估影响面",
                   "给出修复方案与灰度/回滚预案", "复盘沉淀：监控、预案、流程"]
        sample = (
            "遇到「%s」这种情况，我第一步一定是先止血，控制影响面——"
            "比如先 ____（降级/回滚/切流量），让业务先恢复可用，预计 ____ 分钟内生效。\n"
            "第二步同步评估影响：看 ____ 指标和 ____ 日志，确认受影响的用户量级和数据是否需要补偿。\n"
            "第三步定位根因并修复：我的排查顺序是 ____，同时保留 ____ 便于回溯。\n"
            "修复后灰度上线观察 ____ 分钟，确认指标正常再全量，并保留回滚开关。\n"
            "最后复盘：补上 ____ 的监控告警，把这次的处置步骤沉淀成 runbook，并做一次故障演练。"
        ) % topic
        traps = ["一上来就找根因，忽略了先止血", "只说要排查，没有时间预期和判断依据", "答完就结束，没有复盘和机制沉淀"]

    elif dim == "专业深度" or "专业" in cat or "技术" in cat:
        key_points = [
            "先给结论或方案全景，让面试官知道你要讲什么",
            "分层拆解（如数据/模型/工程/成本）并给选型理由",
            "讲清关键参数的权衡与踩过的坑",
            "用核心指标收尾，量化效果或代价",
        ]
        outline = ["结论先行：整体方案是什么", "分层拆解并说明每层的技术选型",
                   "关键权衡：为什么这么选、放弃了什么", "效果与代价：指标表现 + 已知局限"]
        sample = (
            "关于「%s」，我的整体思路是 ____。\n"
            "拆开看分三层：数据层 ____，核心层 ____，工程层 ____。"
            "选型上我选 ____ 而不是 ____，主要考虑 ____（性能/成本/可维护性）。\n"
            "这里有个关键权衡：____ 会增加 ____ 的开销，但能换来 ____，"
            "结合我们的场景（____），这笔交换是划算的。\n"
            "实际上线后指标是 ____，相比之前提升了 ____%%；目前的局限在 ____，"
            "后续我计划通过 ____ 优化。"
        ) % topic
        traps = ["堆砌名词但说不清为什么这么选", "只讲优点不提局限和代价", "缺少量化指标，无法判断真实水平"]

    elif dim == "逻辑结构":
        key_points = ["结论先行，第一句就给出核心判断",
                      "分点之间有清晰的逻辑关系（并列/递进/时间）",
                      "每一点都有论据或案例支撑",
                      "结尾回扣题目，不跑题"]
        outline = ["结论：我的判断是什么", "理由一 + 论据", "理由二 + 论据", "总结回扣 + 补充边界条件"]
        sample = (
            "我的结论是 ____。\n"
            "主要有三方面理由。第一，____，比如 ____ 这个案例就能说明。"
            "第二，____，从数据上看 ____。第三，____，风险在于 ____，可以用 ____ 对冲。\n"
            "所以综合来看 ____。当然这个结论的前提是 ____，"
            "如果 ____ 发生变化，我会调整为 ____。"
        )
        traps = ["先讲半天铺垫，结论放在最后", "分点之间逻辑重叠或跳跃", "只列观点不给论据"]

    elif dim == "语言表达":
        key_points = ["观点明确，一句话说清立场",
                      "用数据或事实支撑观点",
                      "主动交代风险与预案，体现周全",
                      "控制时长，重点前置"]
        outline = ["观点（一句话）", "数据支撑", "风险与预案", "一句话总结"]
        sample = (
            "我的看法是 ____。\n"
            "支撑这个判断的数据是 ____（说明口径：____）。\n"
            "需要注意的风险是 ____，我的预案是 ____，触发条件是 ____。\n"
            "所以简单说，____。"
        )
        traps = ["口头禅多、表达绕", "有观点没数据", "没提风险和边界，显得不周全"]

    elif dim == "岗位匹配" or "匹配" in cat or "为什么" in content:
        key_points = ["先说清对岗位要求的理解",
                      "逐条对照自己的能力项，给出对应经历",
                      "每个能力项配一个量化结果",
                      "说明职业规划与岗位的长期契合"]
        outline = ["我对岗位要求的理解", "能力项一 → 对应经历 + 数据",
                   "能力项二 → 对应经历 + 数据", "规划契合：短期能做什么、长期想成长为什么"]
        sample = (
            "我理解这个岗位核心要看三块：____、____ 和 ____。\n"
            "第一块我有直接经验：在 ____ 项目里我 ____，结果是 ____。"
            "第二块我做过 ____，虽然没有那么深，但 ____ 这个方法是相通的，我上手会比较快。\n"
            "第三块目前是我的短板，我的补齐计划是 ____。\n"
            "短期我能马上贡献的是 ____；长期我希望在 ____ 方向深耕，这和岗位的发展路径是一致的。"
        )
        traps = ["空喊「我很匹配」但拿不出证据", "只讲过去不讲岗位需要什么", "对岗位理解停留在 JD 字面"]

    else:
        key_points = ["先界定问题边界，确认理解一致",
                      "给出分步骤的可执行方案",
                      "说明预期效果与衡量方式",
                      "预设风险与应对"]
        outline = ["明确问题边界与目标", "分步方案（每步的动作与产出）",
                   "预期效果 + 衡量指标", "风险与预案"]
        sample = (
            "关于「%s」，我先确认一下目标：我们要解决的是 ____，衡量标准是 ____。\n"
            "我的方案分三步：第一步 ____，产出是 ____；第二步 ____，产出是 ____；"
            "第三步 ____，产出是 ____。\n"
            "预期效果是 ____，我会用 ____ 这个指标来跟踪。\n"
            "主要风险是 ____，我的预案是 ____。如果资源只够做一件事，我会优先 ____，因为 ____。"
        ) % topic
        traps = ["没确认问题就开始答，容易跑题", "只有方向没有具体步骤", "没说怎么衡量效果"]

    return {
        "key_points": key_points,
        "outline": outline,
        "sample": sample,
        "common_traps": traps,
        "engine": "rule",
    }


def generate_resume_questions(profile: dict, count: int = 5) -> list[dict]:
    """
    基于简历画像生成半结构化面试题（用于动态出题）。
    返回 [{content, dimension, category, time_limit_s, ref_answer}]
    """
    questions = []
    projects = profile.get("projects") or []
    skills = profile.get("skills") or []
    suggested = profile.get("suggested_questions") or []

    dims = ["专业深度", "逻辑结构", "应急应变", "岗位匹配", "语言表达"]

    # 优先采用大模型/规则引擎给的个性化题（含简历真实内容）
    for i, q in enumerate(suggested[:count]):
        q = q.strip()
        if q:
            questions.append({
                "content": q,
                "dimension": dims[i % len(dims)],
                "category": "简历深挖",
                "time_limit_s": 180,
            })
    # 不足则用项目/技能模板补（引用简历真实内容）
    tpls = []
    if projects:
        p0 = _proj_short(str(projects[0]), 20)
        tpls.append((f"结合你的项目经历「{p0}」，请讲讲你当时面临的核心问题和你的解决思路。", "专业深度"))
    if skills:
        tpls.append((f"你在简历中提到熟悉{skills[0]}，能否举一个用它解决实际问题的具体例子？", "岗位匹配"))
    tpls.append(("请用 STAR 法则（情境-任务-行动-结果）介绍一段你最能体现能力的经历。", "逻辑结构"))

    for content, dim in tpls:
        if len(questions) >= count:
            break
        questions.append({"content": content, "dimension": dim, "category": "简历深挖", "time_limit_s": 180})

    out = questions[:count]
    for q in out:
        q["ref_answer"] = _resume_ref_answer(q["content"], profile)
    return out


# —— 招聘域：面试提纲 / 简历筛选 / 语音多维度分析（管理后台） ——
_OUTLINE_SYS = """你是资深面试官，正在为某岗位设计结构化面试提纲（所有候选人共用同一评估框架）。
请严格输出 JSON（不要多余文字）：
{
  "summary": "提纲设计思路概述（60-100字，说明考察重点与框架逻辑）",
  "dimensions": [
    {"name": "维度名（如 专业深度）", "weight": 权重数字0-100,
     "questions": [
       {"content": "面试题（40字内，紧扣JD与简历）", "ref_answer": "采分点要点（50字内）",
        "followups": ["基于该题的追问预案1-2道，每道30字内"]}
     ]}
  ]
}
要求：
1. dimensions 为 4-6 个维度，weight 总和为 100，权重体现 JD 强调的重点；
2. 每个维度 2-3 道题，题目必须引用候选人简历中的真实项目/技能/JD 要求，禁止通用模板；
3. followups 是面试官追问预案，针对回答可能空洞/存疑之处。"""


def generate_interview_outline(jd_text: str, profile: dict | None) -> dict:
    """JD + 简历画像 → 结构化面试提纲。失败降级规则版，永不返回 None。"""
    profile = profile or {}
    skills = profile.get("skills") or []
    projects = profile.get("projects") or []
    prof_hint = ""
    if skills or projects:
        prof_hint = (f"【候选人简历要点】技能：{'、'.join(str(s) for s in skills[:5])}；"
                     f"项目：{str(projects[0])[:60] if projects else '（无）'}")
    user_prompt = (
        f"【岗位JD】\n{(jd_text or '（未提供，请按通用岗位设计）')[:3000]}\n{prof_hint}\n"
        f"请输出面试提纲 JSON。"
    )
    data = _chat_json(_OUTLINE_SYS, user_prompt, timeout=45, caller="outline")
    if data and isinstance(data.get("dimensions"), list) and data["dimensions"]:
        dims = []
        for d in data["dimensions"][:6]:
            name = str(d.get("name") or "").strip()[:16]
            if not name:
                continue
            qs = []
            for q in (d.get("questions") or [])[:3]:
                content = str(q.get("content") or "").strip()
                if not content:
                    continue
                qs.append({
                    "content": content[:80],
                    "ref_answer": str(q.get("ref_answer") or "").strip()[:120],
                    "followups": _norm_str_list(q.get("followups"), limit=2, max_len=50),
                })
            if qs:
                dims.append({"name": name, "weight": _clamp(d.get("weight"), 0, 100, 20),
                             "questions": qs})
        if dims:
            return {"summary": str(data.get("summary") or "").strip()[:200] or "已生成结构化面试提纲。",
                    "dimensions": dims, "engine": "deepseek"}
    return _rule_interview_outline(jd_text, profile)


def _rule_interview_outline(jd_text: str, profile: dict | None) -> dict:
    """规则版提纲：5 维固定框架，题目引用简历/JD 真实内容。"""
    profile = profile or {}
    jd = (jd_text or "").strip()
    skills = profile.get("skills") or ["目标岗位核心技能"]
    projects = profile.get("projects") or ["简历核心项目经历"]
    years = str(profile.get("years_exp") or "").strip()
    jd_words = _jd_keywords(jd, top=3)

    p0 = _proj_short(str(projects[0]))
    dims = [
        {"name": "专业深度", "weight": 30, "questions": [
            {"content": f"结合你做「{p0}」的经历，讲讲该方向的核心难点与你的解决方案。",
             "ref_answer": "难点界定→方案拆解→关键决策→量化结果",
             "followups": [f"你在{jd_words[0] if jd_words else '该方向'}上的深度如何体现？",
                           "方案里最容易失效的前提是什么？"]},
            {"content": f"JD 要求「{jd_words[0] if jd_words else '核心能力'}」，你对应的能力上限在哪里？",
             "ref_answer": "对照 JD 逐条给证据，讲清能力边界",
             "followups": ["举一个超出你能力边界的场景，你怎么处理？"]},
        ]},
        {"name": "逻辑结构", "weight": 20, "questions": [
            {"content": f"请用 STAR 法则完整展开「{p0}」：情境、任务、行动、结果。",
             "ref_answer": "四要素完整，行动部分占 60%",
             "followups": ["如果时间倒回，你会砍掉哪一步？"]},
        ]},
        {"name": "语言表达", "weight": 15, "questions": [
            {"content": "用 1 分钟向非技术背景的高管讲清你最有价值的一段工作成果。",
             "ref_answer": "结论先行、少术语、有一个比喻或数据锚点",
             "followups": ["对方没听懂你会怎么补救？"]},
        ]},
        {"name": "应急应变", "weight": 15, "questions": [
            {"content": f"「{p0}」推进中如果关键资源突然被抽走，你的应对步骤是什么？",
             "ref_answer": "先止血→评估影响→重排优先级→沟通对齐",
             "followups": ["如果止血方案也失败呢？"]},
        ]},
        {"name": "岗位匹配", "weight": 20, "questions": [
            {"content": f"对照这份 JD，你的匹配点和短板各是什么？短板如何补？"
                        + (f"（你已有 {years} 年经验）" if years else ""),
             "ref_answer": "逐条对照 JD，短板给可执行补齐计划",
             "followups": ["为什么离开上一段经历选择这个岗位？"]},
        ]},
    ]
    summary = (f"按 JD 重点生成 5 维评估框架（权重合计 100），题目引用候选人简历「{p0}」"
               f"与技能（{'、'.join(str(s) for s in skills[:3])}），每题附追问预案。")
    return {"summary": summary, "dimensions": dims, "engine": "rule"}


_SCREEN_SYS = """你是资深 HR，正在按 JD 筛选一份简历。请严格输出 JSON（不要多余文字）：
{
  "score": 0-100 匹配分,
  "verdict": "recommend|consider|reject 三选一",
  "summary": "匹配度评价（60-100字，含核心优势与主要顾虑）",
  "concerns": [
    {"type": "gap|rank_mismatch|frequency|other", "text": "疑点描述（30字内，如空窗期、职级不符、频繁跳槽）",
     "followup": "对应追问问题（30字内）"}
  ],
  "followups": ["面试前预准备的追问问题 3-5 道，针对存疑处与 JD 关键要求，每道 35 字内"]
}
concerns 必须基于简历事实（时间线、职级、成果），不要编造；没有疑点可为空数组。"""

_LEVEL_ORDER = {"实习": 0, "助理": 0, "初级": 1, "专员": 1, "中级": 2, "主管": 2,
                "高级": 3, "资深": 4, "专家": 5, "经理": 4, "总监": 6, "vp": 7, "副总裁": 7}


def _detect_gaps(text: str) -> list[str]:
    """简历时间线空窗期检测：返回描述列表（>6 个月间隔）"""
    now = datetime.now()
    spans: list[tuple[float, float, str]] = []
    for m in re.finditer(
            r"(\d{4})\s*[年./-]\s*(\d{1,2})?\s*[-—~至到]\s*([^，,。\n；;]{0,6}?至今|(\d{4})\s*[年./-]\s*(\d{1,2})?)",
            text):
        y1 = int(m.group(1)); m1 = int(m.group(2) or 1) if m.group(2) else 1
        seg = m.group(3)
        if seg and "至今" in seg:
            y2, m2 = now.year, now.month
        elif m.group(4):
            y2 = int(m.group(4)); m2 = int(m.group(5) or 12) if m.group(5) else 12
        else:
            continue
        start = y1 * 12 + m1
        end = y2 * 12 + m2
        if end >= start:
            spans.append((start, end, m.group(0)[:24]))
    spans.sort()
    gaps = []
    for a, b in zip(spans, spans[1:]):
        hole = b[0] - a[1]  # 下一段开始 - 上一段结束
        if hole > 6:
            gaps.append(f"时间线疑点：{a[2]} 之后存在约 {hole} 个月空窗期（至 {b[2]}）")
    return gaps[:3]


def _rank_of(text: str) -> tuple[int, str]:
    """提取简历/JD 中的最高职级：返回 (等级序号, 命中词)"""
    best, hit = -1, ""
    low = text.lower()
    for kw, lv in _LEVEL_ORDER.items():
        if kw in low and lv > best:
            best, hit = lv, kw
    return best, hit


def _jd_keywords(jd_text: str, top: int = 5) -> list[str]:
    """从 JD 提取高频实义词（简单规则）"""
    words = re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,6}", jd_text or "")
    stop = {"要求", "负责", "相关", "工作", "优先", "熟悉", "经验", "能力", "具有", "良好",
            "岗位", "职责", "任职", "以上", "进行", "公司", "团队", "以及", "熟练", "了解",
            "能够", "具备", " Strong", "strong", "plus"}
    freq: dict[str, int] = {}
    for w in words:
        if w in stop or len(w) < 2:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top]]


def _rule_screen_resume(jd_text: str, resume_text: str, profile: dict | None) -> dict:
    """规则版简历筛选：关键词命中 + 年限/职级比对 + 空窗期检测。"""
    profile = profile or {}
    text = resume_text or ""
    profile_text = " ".join(
        " ".join(str(x) for x in (profile.get("skills") or [] + profile.get("projects") or [])))
    full = text + " " + profile_text

    # 1) JD 关键词命中（满分 40）
    kws = _jd_keywords(jd_text, top=6)
    hit = [k for k in kws if k.lower() in full.lower()]
    kw_score = round(len(hit) / max(len(kws), 1) * 40) if kws else 24

    # 2) 年限匹配（满分 20）
    jd_years_m = re.search(r"(\d+)\s*年", jd_text or "")
    jd_years = int(jd_years_m.group(1)) if jd_years_m else 0
    cand_years = int(profile.get("years_exp") or 0) if str(profile.get("years_exp") or "").isdigit() else 0
    if not cand_years:
        m = re.search(r"(\d+)\s*年[^。\n]{0,6}(经验|工作)", text)
        cand_years = int(m.group(1)) if m else 0
    if jd_years == 0:
        yr_score = 14
    elif cand_years >= jd_years:
        yr_score = 20
    elif cand_years >= jd_years * 0.7:
        yr_score = 14
    else:
        yr_score = 6

    # 3) 职级匹配（满分 20）
    jd_rank, jd_hit = _rank_of(jd_text)
    ca_rank, ca_hit = _rank_of(text)
    if jd_rank <= 0:
        rank_score = 14
    elif ca_rank >= jd_rank:
        rank_score = 20
    elif ca_rank >= 0 and ca_rank == jd_rank - 1:
        rank_score = 12
    else:
        rank_score = 4

    # 4) 量化成果（满分 10）
    nums = re.findall(r"[\u4e00-\u9fa5A-Za-z]{1,10}?\d+(?:\.\d+)?(?:%|万|倍)", text)
    num_score = min(10, len(nums) * 3)

    # 5) 学历关键词（满分 10）
    edu_score = 8 if re.search(r"本科|硕士|研究生|博士|学士", text) else 4

    score = round(kw_score + yr_score + rank_score + num_score + edu_score, 1)
    score = max(30.0, min(96.0, score))
    verdict = "recommend" if score >= 75 else "consider" if score >= 55 else "reject"

    # 疑点
    concerns = []
    for g in _detect_gaps(text):
        concerns.append({"type": "gap", "text": g,
                         "followup": "这段空窗期你在做什么？有什么产出？"})
    if jd_rank > 0 and ca_rank >= 0 and ca_rank < jd_rank - 1:
        concerns.append({"type": "rank_mismatch",
                         "text": f"职级疑点：JD 要求「{jd_hit}」级别，简历最高仅体现「{ca_hit}」",
                         "followup": f"你目前实际承担的职责是否达到「{jd_hit}」的深度？"})
    if cand_years and jd_years and cand_years + 3 < jd_years:
        concerns.append({"type": "other",
                         "text": f"经验年限偏少：JD 要求约 {jd_years} 年，简历体现约 {cand_years} 年",
                         "followup": "你的哪些经历可以弥补年限上的差距？"})
    if len(re.findall(r"\d{4}", text)) >= 6:
        jobs_hint = re.findall(r"(\d{4})\s*[年./-]", text)
        if len(jobs_hint) >= 4 and len(set(jobs_hint)) >= 3:
            concerns.append({"type": "frequency",
                             "text": "跳槽频率疑点：短期内多段经历，稳定性待确认",
                             "followup": "每段经历离开的原因是什么？"})
    concerns = concerns[:4]

    followups = [c["followup"] for c in concerns if c.get("followup")]
    for k in kws[:3]:
        q = f"简历中「{hit[0] if hit else k}」相关经历，请展开一个最有挑战的案例。"
        if q not in followups:
            followups.append(q)
    followups = followups[:5]

    kw_txt = "、".join(hit[:4]) if hit else "核心要求"
    summary = (f"JD 关键词命中：{kw_txt}；年限 {'达标' if yr_score >= 20 else '基本匹配' if yr_score >= 14 else '偏少'}，"
               f"职级 {'匹配' if rank_score >= 20 else '接近' if rank_score >= 12 else '存疑'}，"
               f"量化成果 {len(nums)} 处。"
               + (f"主要顾虑：{concerns[0]['text']}。" if concerns else ""))
    return {"score": score, "verdict": verdict, "summary": summary,
            "concerns": concerns, "followups": followups, "engine": "rule"}


def screen_resume(jd_text: str, resume_text: str, profile: dict | None) -> dict:
    """按 JD 筛选简历：打分 + 疑点 + 预生成追问。失败降级规则版。"""
    if not (jd_text or "").strip() or len((resume_text or "").strip()) < 30:
        return _rule_screen_resume(jd_text, resume_text, profile)
    prof_hint = ""
    if profile:
        prof_hint = (f"【简历画像摘要】{str(profile.get('summary') or '')[:150]}"
                     f" 技能：{'、'.join(str(s) for s in (profile.get('skills') or [])[:5])}")
    user_prompt = (f"【岗位JD】\n{jd_text[:2500]}\n\n【候选人简历】\n{resume_text[:3500]}\n{prof_hint}\n"
                   f"请输出筛选评估 JSON。")
    data = _chat_json(_SCREEN_SYS, user_prompt, timeout=40, caller="screening")
    if not data or data.get("score") is None:
        return _rule_screen_resume(jd_text, resume_text, profile)
    score = _clamp(data.get("score"), 30, 96, 60)
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("recommend", "consider", "reject"):
        verdict = "recommend" if score >= 75 else "consider" if score >= 55 else "reject"
    concerns = []
    for c in (data.get("concerns") or [])[:4]:
        if isinstance(c, dict) and str(c.get("text") or "").strip():
            concerns.append({"type": str(c.get("type") or "other")[:16],
                             "text": str(c.get("text")).strip()[:60],
                             "followup": str(c.get("followup") or "").strip()[:60]})
    return {
        "score": score, "verdict": verdict,
        "summary": str(data.get("summary") or "").strip()[:300] or "已完成筛选评估。",
        "concerns": concerns,
        "followups": _norm_str_list(data.get("followups"), limit=5, max_len=60),
        "engine": "deepseek",
    }


_VOICE_SYS = """你是面试心理与表达分析专家。根据候选人的语音指标与作答转写，输出多维度分析 JSON（不要多余文字）：
{
  "emotion": "情绪状态判断（8字内，如：平稳自信/略紧张/急躁/低沉）",
  "emotion_detail": "判断依据（50字内，引用指标或原话特征）",
  "resilience": "抗压能力：strong|medium|weak 三选一",
  "resilience_detail": "依据（50字内，如被打断后的恢复、面对追问的表现）",
  "expression": "语言表达素养：strong|medium|weak 三选一",
  "expression_detail": "依据（50字内，如逻辑连接词、口头禅、语流控制）",
  "voice_score": 0-100 语音综合表现分,
  "suggestions": ["针对性建议 2 条，每条 35 字内"]
}"""


def _speed_score(wpm: int) -> float:
    """语速分：130-190 字/分 满分区，向外线性衰减"""
    if wpm <= 0:
        return 50.0
    if 130 <= wpm <= 190:
        return 100.0
    if wpm < 130:
        return max(40.0, 100 - (130 - wpm) * 1.2)
    return max(40.0, 100 - (wpm - 190) * 1.5)


def analyze_voice_profile(metrics_list: list[dict], transcripts: list[str],
                          allow_llm: bool = True) -> dict:
    """语音多维度分析：metrics_list=[{wpm,pause_count,pause_ms_total,filler_count,interrupt_count,duration_ms}]
    transcripts 为对应作答转写。DeepSeek 优先，失败降级规则映射。
    allow_llm=False 时只用规则映射（批量对比场景，避免串行 LLM 拖慢响应）。"""
    metrics_list = [m for m in (metrics_list or []) if m]
    n = max(len(metrics_list), 1)
    avg = {
        "wpm": round(sum(m.get("wpm") or 0 for m in metrics_list) / n),
        "pause_count": round(sum(m.get("pause_count") or 0 for m in metrics_list) / n),
        "pause_ms_total": round(sum(m.get("pause_ms_total") or 0 for m in metrics_list) / n),
        "filler_count": round(sum(m.get("filler_count") or 0 for m in metrics_list) / n),
        "interrupt_count": round(sum(m.get("interrupt_count") or 0 for m in metrics_list) / n),
        "duration_ms": round(sum(m.get("duration_ms") or 0 for m in metrics_list) / n),
    }
    transcript = " ".join(t for t in (transcripts or []) if t)

    if allow_llm and transcript and len(transcript.strip()) >= 40:
        user_prompt = (
            f"【语音指标（多题均值）】语速 {avg['wpm']} 字/分，每题平均停顿 {avg['pause_count']} 次"
            f"（共 {avg['pause_ms_total']}ms），填充词 {avg['filler_count']} 个，被打断 {avg['interrupt_count']} 次。\n"
            f"【作答转写（节选）】\n{transcript[:2000]}\n请输出语音分析 JSON。"
        )
        data = _chat_json(_VOICE_SYS, user_prompt, timeout=35, caller="voice")
        if data:
            resilience = str(data.get("resilience") or "").strip()
            expression = str(data.get("expression") or "").strip()
            return {
                "emotion": str(data.get("emotion") or "").strip()[:8] or "平稳",
                "emotion_detail": str(data.get("emotion_detail") or "").strip()[:100],
                "resilience": resilience if resilience in ("strong", "medium", "weak") else "medium",
                "resilience_detail": str(data.get("resilience_detail") or "").strip()[:100],
                "expression": expression if expression in ("strong", "medium", "weak") else "medium",
                "expression_detail": str(data.get("expression_detail") or "").strip()[:100],
                "voice_score": _clamp(data.get("voice_score"), 40, 96, 70),
                "suggestions": _norm_str_list(data.get("suggestions"), limit=2, max_len=50),
                "metrics_avg": avg,
                "engine": "deepseek",
            }

    # —— 规则降级：纯指标映射 ——
    sp = _speed_score(avg["wpm"])
    dur_min = max(avg["duration_ms"] / 60000, 0.5)
    pause_ratio = avg["pause_ms_total"] / max(avg["duration_ms"], 1)
    pause_score = max(30.0, 100 - pause_ratio * 500)          # 停顿占比 <6% 高分
    filler_rate = avg["filler_count"] / dur_min
    filler_score = max(30.0, 100 - max(filler_rate - 2, 0) * 20)  # 每分钟 <2 个高分
    voice_score = round(sp * 0.5 + pause_score * 0.3 + filler_score * 0.2, 1)

    if avg["pause_count"] >= 4 and avg["wpm"] < 140:
        emotion = "偏紧张"
    elif avg["wpm"] > 200 or avg["interrupt_count"] >= 2:
        emotion = "偏急躁"
    elif voice_score >= 80:
        emotion = "平稳自信"
    else:
        emotion = "平稳"
    resilience = "strong" if (avg["interrupt_count"] == 0 or voice_score >= 75) else \
                 "weak" if avg["pause_count"] >= 6 else "medium"
    expression = "strong" if (filler_rate <= 2 and 130 <= avg["wpm"] <= 200 and voice_score >= 75) else \
                 "weak" if filler_rate >= 6 or avg["wpm"] < 100 else "medium"
    detail = (f"语速 {avg['wpm']} 字/分，停顿 {avg['pause_count']} 次/题（{pause_ratio:.0%}），"
              f"填充词 {avg['filler_count']} 个/题，被打断 {avg['interrupt_count']} 次。")
    return {
        "emotion": emotion, "emotion_detail": detail,
        "resilience": resilience,
        "resilience_detail": f"基于 {avg['interrupt_count']} 次打断与停顿恢复情况的规则推断。",
        "expression": expression, "expression_detail": detail,
        "voice_score": voice_score,
        "suggestions": ["控制语速在 130-190 字/分，重点处刻意放慢",
                        "减少口头禅，用「第一/第二」替代「然后/那个」"],
        "metrics_avg": avg,
        "engine": "rule",
    }


# —— 群面多智能体：候选人发言生成 ——

# 回应方式 → 对模型的具体动作指令
_REPLY_MODE_INSTR = {
    "agree": "认同上一位发言人的观点，并且必须顺着补一个【新的论据或角度】，不能只说“我同意”",
    "refute": "直接反驳或质疑上一位发言人，必须针对其论证中的具体漏洞（前提/数据/可行性），不能空喊反对",
    "probe": "向上一位发言人追问一个具体、尖锐的问题，逼对方把观点落到可执行层面",
    "answer": "你被主持人点名了，先简短回应点名，再明确给出自己的观点，不许含糊、不许划水",
    "opening": "破冰开场：用一句有个人色彩的话亮明你将从什么角度参与讨论",
    "statement": "个人陈述：亮明你的立场，并给出一到两个理由",
    "converge": "收敛讨论：在对立观点中找最大公约数，提出可以形成共识的表述",
    "summary": "代表全组做总结陈词：归纳本场讨论达成的结论与遗留分歧，给出明确收尾",
}

_PEER_SYS = """你正在扮演一场无领导小组讨论中的候选人，必须完全进入角色。
输出严格 JSON：{"text": "你的发言"}。
铁律：
1. 必须按“回应要求”咬住上文指定的那个观点，禁止自说自话、禁止复述他人原话；
2. 口语化，像真实面试讨论现场，2-3 句话，不分点、不用书面语、不用 Markdown；
3. 严格保持你被分配的性格、语气和立场；
4. 不要输出名字前缀、引号、括号旁白或 JSON 以外的任何内容。"""


def _clean_peer_text(text: str) -> str:
    """清洗模型输出：去名字前缀/列表符号，截断到 2-3 句，限长。"""
    if not text:
        return ""
    t = text.strip().strip("「」“”\"'")
    # 去 "张三：" / "- " / "1. " 等前缀
    t = re.sub(r"^(?:\s*[-*•·]\s*)?(?:\d+[.、]\s*)?(?:\S{1,8}\s*[：:]\s*)?", "", t)
    # 仅保留前 3 句（中文句末标点），避免长篇大论
    parts = re.split(r"(?<=[。！？!?…])", t)
    out, n = "", 0
    for seg in parts:
        if not seg.strip():
            continue
        out += seg
        n += 1
        if n >= 3:
            break
    return out.strip()[:150]


def generate_peer_utterance(
    card: dict,
    topic: str,
    phase_hint: str,
    reply_mode: str,
    anchors: list[dict],
    interrupt: bool = False,
    extra_instruction: str = "",
) -> Optional[str]:
    """生成一条群面候选人发言（LLM）。失败返回 None，由调用方降级规则话术。

    :param card: 人设卡（含 name/style_prompt/habits/stance_label/temperature）
    :param topic: 辩题
    :param phase_hint: 当前阶段的中文说明
    :param reply_mode: agree/refute/probe/answer/opening/statement/converge/summary
    :param anchors: 最近 1-2 条发言摘要 [{"speaker": "张三", "summary": "..."}]
    :param interrupt: 是否抢话（抢话时提示模型用打断式开头、语气更急）
    :param extra_instruction: 阶段附加指令
    :return: 清洗后的发言文本，或 None（未配置 Key / 超时 / 余额不足 / 格式错）
    """
    mode_instr = _REPLY_MODE_INSTR.get(reply_mode, _REPLY_MODE_INSTR["statement"])
    if anchors:
        anchor_lines = "\n".join(f"- {a['speaker']}：{a['summary']}" for a in anchors[:2])
        anchor_block = f"场上最近的发言（你必须咬住其中最新一条的具体观点）：\n{anchor_lines}"
    else:
        anchor_block = "目前场上还没有实质发言，你可以直接开启话题。"
    interrupt_block = (
        "\n注意：你此刻在【抢话】，可以用“等一下，我插一句”“先别急——”这类打断式开头，语气更急、更直接。"
        if interrupt else ""
    )
    sys_prompt = (
        f"{_PEER_SYS}\n"
        f"你的名字：{card['name']}\n"
        f"你的性格与说话风格：{card.get('style_prompt', '')}\n"
        f"你的表达习惯：{card.get('habits', '')}"
    )
    user_prompt = (
        f"讨论议题：{topic}\n"
        f"当前阶段：{phase_hint}\n"
        f"你的立场：{card.get('stance_label', '中立')}\n"
        f"{anchor_block}\n"
        f"回应要求：{mode_instr}。{interrupt_block}{extra_instruction}"
    )
    data = _chat_json(
        sys_prompt, user_prompt,
        temperature=float(card.get("temperature", 0.7)),
        max_tokens=260,
        caller="group_peer",
    )
    if not data:
        return None
    text = _clean_peer_text(str(data.get("text", "")))
    return text or None
