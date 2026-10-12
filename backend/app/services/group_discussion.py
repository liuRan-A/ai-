# -*- coding: utf-8 -*-
"""
群面（无领导小组讨论）事件驱动多智能体调度核心。

设计要点：
1. 五阶段状态机：opening 破冰 → statement 个人陈述 → discussion 自由交锋
   → convergence 收敛共识 → summary 总结陈词；阶段可由导演自动推进，也可由前端手动推进。
2. 动态选人：意愿打分 + 加权随机采样（绝不取最高分，保证顺序不可预测）。
   打分因子：发言意愿基础值 / 冷却 ×0.1 / 被点名 ×4.0 / 立场对立 ×2.5 /
   久未发言 ×1.8 / 抢话 ×2.0。
3. Prompt 锚定：每次发言把最近 1-2 条发言的【摘要】注入 LLM，回应方式三选一
   （agree 认同补充 / refute 反驳质疑 / probe 追问），结构上保证回应锚定率。
4. 主持人（moderator）：冷场点名最沉默者、控场拉回议题、宣布阶段切换、收尾。
5. LLM 失败（无 Key / 超时 / 余额不足）时降级到内置规则话术——规则话术同样
   锚定上文观点、同样区分人设，业务闭环不中断。

本模块为纯逻辑 + LLM 调用，不落库；状态由前端上报的 history 重建（无状态服务）。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from app.services import llm

# ————————————————————————————————————————————————
# 1. 五阶段状态机
# ————————————————————————————————————————————————
PHASES: list[dict] = [
    {
        "key": "opening",
        "label": "破冰开场",
        "hint": "主持人开场，候选人轮流用一句话介绍自己的切入角度。轮到你时点击「举手发言」。",
        "max_rounds": 3,
        "llm_hint": "破冰开场阶段，气氛可以轻松一点",
    },
    {
        "key": "statement",
        "label": "个人陈述",
        "hint": "每位候选人依次亮明立场与理由。轮到你时给出你的立场和 1-2 个论据。",
        "max_rounds": 8,
        "llm_hint": "个人陈述阶段，每人都要亮明立场",
    },
    {
        "key": "discussion",
        "label": "自由交锋",
        "hint": "交锋环节：认同补充、反驳质疑或追问。每次发言后候选人会动态回应，注意谁在抢话。",
        "max_rounds": 16,
        "llm_hint": "自由交锋阶段，观点碰撞、互相质疑",
    },
    {
        "key": "convergence",
        "label": "收敛共识",
        "hint": "停止发散，寻找最大公约数：哪些结论可以定？分歧怎么处理？",
        "max_rounds": 7,
        "llm_hint": "收敛共识阶段，目标是推动小组形成结论",
    },
    {
        "key": "summary",
        "label": "总结陈词",
        "hint": "候选人代表小组总结，随后给出你的最终陈词并提交。",
        "max_rounds": 4,
        "llm_hint": "总结陈词阶段，归纳全场共识与遗留分歧",
    },
]
PHASE_KEYS = [p["key"] for p in PHASES]
PHASE_MAP = {p["key"]: p for p in PHASES}

# ————————————————————————————————————————————————
# 2. 人设卡（五种差异原型 + 一个额外的质疑型）
# ————————————————————————————————————————————————
# talkativeness 发言意愿基础值（拉开频率差距：激进 0.95 vs 划水 0.32）
# agree_bias 倾向认同的概率基准（越低越爱唱反调）
# interrupt_rate 抢话概率（仅自由交锋/收敛阶段生效）
# stance: support 正方 / oppose 反方 / neutral 中立
ARCHETYPES: dict[str, dict] = {
    "aggressive": {
        "label": "激进型",
        "talkativeness": 0.95, "agree_bias": 0.18, "interrupt_rate": 0.32,
        "stance": "support",
        "stance_phrase": "这个命题我坚定支持",
        "temperature": 0.9,
        "style_prompt": "你是团队里最激进的人，性格急切、进攻性强，爱抢占话语权，习惯说“我直接说”“别绕了”，观点鲜明甚至咄咄逼人，倾向支持大胆行动",
        "habits": "句子短、语气冲、爱下判断、喜欢打断、常用“窗口期”“必须现在”",
    },
    "slacker": {
        "label": "划水型",
        "talkativeness": 0.32, "agree_bias": 0.78, "interrupt_rate": 0.02,
        "stance": "neutral",
        "stance_phrase": "我没啥强烈立场，大家定就行",
        "temperature": 0.7,
        "style_prompt": "你是典型的划水者，怕担责、随大流，发言短、信息密度低，习惯附和多数人，被点名时才被迫说两句，倾向和稀泥",
        "habits": "爱说“我同意”“都行”“差不多”“大家定就好”，语气犹豫、经常打圆场",
    },
    "analyst": {
        "label": "分析型",
        "talkativeness": 0.72, "agree_bias": 0.30, "interrupt_rate": 0.10,
        "stance": "neutral",
        "stance_phrase": "结论要让数据说话",
        "temperature": 0.4,
        "style_prompt": "你是数据驱动的分析者，咨询顾问出身，开口先讲口径、样本、数据，不轻易站队，谁的论证不严谨就质疑谁",
        "habits": "爱说“数据是”“口径要统一”“这个结论可证伪吗”，语速平稳、逻辑词多",
    },
    "integrator": {
        "label": "调和型",
        "talkativeness": 0.78, "agree_bias": 0.60, "interrupt_rate": 0.05,
        "stance": "neutral",
        "stance_phrase": "我更关心能不能形成可执行的共识",
        "temperature": 0.6,
        "style_prompt": "你是团队的粘合剂，HR/管培生背景，擅长求同存异，总能在对立观点里找到共同点并推动共识，照顾每个人的情绪",
        "habits": "爱说“我理解两位的意思”“我们能不能”“共识是”，语气温和、爱归纳",
    },
    "steady": {
        "label": "稳健型",
        "talkativeness": 0.58, "agree_bias": 0.42, "interrupt_rate": 0.04,
        "stance": "oppose",
        "stance_phrase": "风险没算清之前，我倾向踩刹车",
        "temperature": 0.5,
        "style_prompt": "你是风控出身的稳健派，先问最坏情况再谈收益，反对激进冒进，发言不多但每次都踩在风险点上",
        "habits": "爱说“先别急”“最坏情况是”“代价谁承担”，语气沉稳、偏保守",
    },
    "questioner": {
        "label": "质疑型",
        "talkativeness": 0.82, "agree_bias": 0.12, "interrupt_rate": 0.16,
        "stance": "oppose",
        "stance_phrase": "论证过不了我这关之前，我保留意见",
        "temperature": 0.75,
        "style_prompt": "你是哲学系转产品的质疑者，专挑论证漏洞，喜欢追问前提，让人又爱又恨，天然站在对立面",
        "habits": "爱说“等一下”“这个前提成立吗”“你怎么证明”，反问句多",
    },
}

# 群面固定上场的五种原型（保证每场人格结构完整）
CANONICAL_ORDER = ["aggressive", "slacker", "analyst", "integrator", "steady"]

_STANCE_LABEL = {"support": "正方：倾向支持命题、主张积极行动",
                 "oppose": "反方：倾向质疑命题、强调风险代价",
                 "neutral": "中立：只看证据与逻辑，不预设立场"}

# DB 人设 style/bio 关键词 → 原型
_KEYWORD_MAP = [
    ("aggressive", ("激进", "猛", "冲")),
    ("slacker", ("划水", "随大流", "附和")),
    ("analyst", ("数据", "分析", "咨询")),
    ("integrator", ("整合", "调和", "共识", "求同存异")),
    ("steady", ("稳健", "风控", "保守")),
    ("questioner", ("质疑", "漏洞", "挑")),
]


def archetype_of(style: str, bio: str = "") -> str:
    """根据 DB 人设的 style/bio 文本推断原型 key；推断不出时默认分析型。"""
    text = f"{style}{bio}"
    for key, kws in _KEYWORD_MAP:
        if any(kw in text for kw in kws):
            return key
    return "analyst"


def select_session_personas(dedicated: list, generic: list, limit: int = 5) -> list:
    """为一场群面挑选上场人设：套题专属人设优先，通用池保证五种固定原型齐全。

    :param dedicated: 该套题专属人设（如有，优先全部上场）
    :param generic: 通用人设池
    :param limit: 上场人数（默认 5）
    """
    chosen = list(dedicated)
    by_arch: dict[str, list] = {}
    for p in generic:
        if p in chosen:
            continue
        by_arch.setdefault(archetype_of(p.style or "", p.bio or ""), []).append(p)
    # 第一轮：五种固定原型各取一人（顺序即激进→划水→分析→调和→稳健）
    for key in CANONICAL_ORDER:
        if len(chosen) >= limit:
            break
        if by_arch.get(key):
            chosen.append(by_arch[key][0])
    # 第二轮：剩余名额（如专属人设占了坑位）从其他原型（质疑型等）随机补
    picked = set(id(p) for p in chosen)
    rest = [p for p in generic if id(p) not in picked]
    random.shuffle(rest)
    for p in rest:
        if len(chosen) >= limit:
            break
        chosen.append(p)
    return chosen[:limit]


def build_cards(personas: list) -> list[dict]:
    """把 DB PeerPersona 列表转成带调度特质的人设卡（保序）。

    优先按文本关键词映射原型；若五个固定原型未覆盖齐（例如缺少划水型），
    则按 CANONICAL_ORDER 把未占用的原型按位置补齐，保证一场群面五型俱全。
    """
    cards = []
    used = set()
    for p in personas:
        key = archetype_of(p.style or "", p.bio or "")
        if key in used and key in CANONICAL_ORDER:
            # 同原型重复：让位于尚未出现的固定原型
            for cand in CANONICAL_ORDER:
                if cand not in used:
                    key = cand
                    break
        used.add(key)
        a = ARCHETYPES[key]
        cards.append({
            "persona_id": p.id,
            "name": p.name,
            "style": p.style or a["label"],
            "color": p.color,
            "bio": p.bio or "",
            "archetype": key,
            "archetype_label": a["label"],
            "talkativeness": a["talkativeness"],
            "agree_bias": a["agree_bias"],
            "interrupt_rate": a["interrupt_rate"],
            "stance": a["stance"],
            "stance_label": _STANCE_LABEL[a["stance"]],
            "stance_phrase": a["stance_phrase"],
            "temperature": a["temperature"],
            "style_prompt": a["style_prompt"],
            "habits": a["habits"],
        })
    return cards


# ————————————————————————————————————————————————
# 3. 讨论状态重建（无状态服务：状态全部从 history 推导）
# ————————————————————————————————————————————————
@dataclass
class SpeakerStat:
    """单个候选人的累计发言状态。"""
    persona_id: int
    name: str
    total: int = 0                       # 全场总发言数
    last_idx: int = -1                   # 最近一次发言在实质消息序列中的下标
    phase_count: dict = field(default_factory=dict)  # phase → 本阶段发言数


@dataclass
class DiscussionState:
    phase: str
    cards: list[dict]
    stats: dict                           # persona_id → SpeakerStat
    substantive: list[dict]               # 仅 me/peer 的实质发言（含 idx/phase）
    phase_rounds: int = 0                 # 本阶段实质发言数
    last_speaker_id: int | None = None    # 最近发言者（me 用 -1）
    user_spoke_phases: set = field(default_factory=set)
    user_stance: str = "neutral"

    def card_by_name(self, name: str) -> dict | None:
        return next((c for c in self.cards if c["name"] == name), None)


_SUPPORT_KW = ("支持", "赞成", "认同", "利大于弊", "应该推", "我站正")
_OPPOSE_KW = ("反对", "不赞成", "弊大于利", "不应该", "风险太大", "我质疑", "站反")


def _detect_user_stance(history: list[dict]) -> str:
    """从用户在陈述阶段的发言里粗略识别立场（供立场对立加权使用）。"""
    s = o = 0
    for m in history:
        if m.get("who") == "me" and m.get("phase") == "statement":
            t = m.get("text", "")
            s += sum(t.count(k) for k in _SUPPORT_KW)
            o += sum(t.count(k) for k in _OPPOSE_KW)
    if s == o == 0:
        return "neutral"
    return "support" if s > o else "oppose"


def rebuild_state(phase: str, cards: list[dict], history: list[dict]) -> DiscussionState:
    """从前端上报的历史消息重建调度状态。"""
    stats = {c["persona_id"]: SpeakerStat(c["persona_id"], c["name"]) for c in cards}
    substantive = []
    last_speaker_id = None
    user_phases: set = set()
    idx = 0
    for m in history:
        who = m.get("who")
        if who not in ("me", "peer") or not m.get("text"):
            continue
        substantive.append({"idx": idx, **m})
        m_phase = m.get("phase") or phase
        if who == "me":
            last_speaker_id = -1
            user_phases.add(m_phase)
        else:
            pid = m.get("persona_id")
            card = next((c for c in cards if c["name"] == m.get("name")), None)
            pid = card["persona_id"] if card else pid
            if pid in stats:
                st = stats[pid]
                st.total += 1
                st.last_idx = idx
                st.phase_count[m_phase] = st.phase_count.get(m_phase, 0) + 1
                last_speaker_id = pid
        idx += 1
    phase_rounds = sum(1 for m in substantive if (m.get("phase") or phase) == phase)
    return DiscussionState(
        phase=phase, cards=cards, stats=stats, substantive=substantive,
        phase_rounds=phase_rounds, last_speaker_id=last_speaker_id,
        user_spoke_phases=user_phases, user_stance=_detect_user_stance(history),
    )


# ————————————————————————————————————————————————
# 4. 发言摘要 + 被点名检测
# ————————————————————————————————————————————————
def summarize(text: str, max_len: int = 38) -> str:
    """提取式摘要：优先观点句，截断到完整词边界。注入摘要而非全量历史，防止上下文膨胀。"""
    if not text:
        return ""
    sents = [s.strip() for s in text.replace("\n", "。").split("。") if len(s.strip()) >= 6]
    if not sents:
        return text.strip()[:max_len]
    marked = [s for s in sents if any(
        k in s for k in ("我认为", "我觉得", "应该", "关键", "核心", "支持", "反对", "共识", "建议", "数据"))]
    pick = (marked or sents)[0]
    if len(pick) > max_len:
        cut = pick[:max_len]
        for sep in ("，", ",", "；", " "):
            pos = cut.rfind(sep)
            if pos >= max_len - 10:
                cut = cut[:pos]
                break
        pick = cut
    return pick


def _mentioned_in(text: str, card: dict) -> bool:
    """检测最近发言是否点名了该候选人（全名 / 姓+称谓），命中即 ×4.0。"""
    if not text:
        return False
    name = card["name"]
    if name and name in text:
        return True
    surname = name[0] if name else ""
    return bool(surname and re.search(rf"{surname}(同学|先生|女士|老师|总)", text))


def _anchors_from(working_history: list[dict], n: int = 2) -> list[dict]:
    """从工作历史（含本轮已生成的新发言）末尾取 n 条实质发言做锚点摘要。"""
    out = []
    for m in reversed(working_history):
        if m.get("who") in ("me", "peer") and m.get("text"):
            speaker = "你（候选人本人）" if m.get("who") == "me" else m.get("name", "候选人")
            out.append({"speaker": speaker, "summary": summarize(m["text"])})
            if len(out) >= n:
                break
    out.reverse()
    return out


# ————————————————————————————————————————————————
# 5. 意愿打分 + 加权随机选人（核心，禁止取最高分）
# ————————————————————————————
def _weight_floor(w: float) -> float:
    """权重地板：划水型也不能永远沉默（还要靠主持人点名制造真实感）。"""
    return max(0.02, w)


def score_speakers(state: DiscussionState, *, exclude_last: bool = True) -> list[tuple[dict, float, list[str]]]:
    """为每位候选人计算发言意愿分。

    :return: [(card, weight, reasons)]，reasons 记录命中的加权因子，便于调试/观测
    """
    n = max(1, len(state.cards))
    last1 = state.substantive[-1] if state.substantive else None
    last2 = state.substantive[-2] if len(state.substantive) >= 2 else None
    last_texts = " ".join(m.get("text", "") for m in (last1, last2) if m)
    # 上一位发言者的立场（用户或候选人）
    if last1 and last1.get("who") == "me":
        last_stance = state.user_stance
    elif last1:
        c = state.card_by_name(last1.get("name", ""))
        last_stance = c["stance"] if c else "neutral"
    else:
        last_stance = "neutral"

    result = []
    for card in state.cards:
        pid = card["persona_id"]
        st = state.stats[pid]
        w = card["talkativeness"]
        reasons = [f"base={w:.2f}"]

        # 冷却：刚发言过 ×0.1（同阶段连续发言几乎不再选他）
        if exclude_last and state.last_speaker_id == pid:
            w *= 0.1
            reasons.append("cooldown×0.1")

        # 被点名或质疑 ×4.0
        if _mentioned_in(last_texts, card):
            w *= 4.0
            reasons.append("called×4.0")

        # 立场与上文相反 ×2.5（中立者轻微加权 1.25）
        if last_stance in ("support", "oppose"):
            if card["stance"] != "neutral" and card["stance"] != last_stance:
                w *= 2.5
                reasons.append("oppose-stance×2.5")
            elif card["stance"] == "neutral":
                w *= 1.2
                reasons.append("neutral×1.2")

        # 久未发言 ×1.8（从未发言也算；阈值≈一轮人数）
        never_or_idle = st.last_idx < 0 or (len(state.substantive) - st.last_idx) >= n
        if never_or_idle:
            w *= 1.8
            reasons.append("idle×1.8")

        # 阶段偏置
        ph = state.phase
        if ph == "statement":
            # 陈述阶段：还没陈述的人强加权，已陈述的人几乎不再选
            if st.phase_count.get("statement", 0) == 0:
                w *= 8.0
                reasons.append("need-statement×8")
            else:
                w *= 0.02
                reasons.append("stated×0.02")
        elif ph == "convergence":
            if card["archetype"] == "integrator":
                w *= 1.7; reasons.append("integrator×1.7")
            elif card["archetype"] == "slacker":
                w *= 0.8
        elif ph == "summary":
            if card["archetype"] == "integrator":
                w *= 3.0; reasons.append("summarizer×3")
            else:
                w *= 0.15

        result.append((card, _weight_floor(w), reasons))
    return result


def weighted_pick(weighted: list[tuple[dict, float, list[str]]]) -> tuple[dict, list[str]]:
    """加权随机采样——绝不取最高分，否则顺序会重新变得可预测。"""
    cards = [x[0] for x in weighted]
    weights = [x[1] for x in weighted]
    reason_map = {x[0]["persona_id"]: x[2] for x in weighted}
    chosen = random.choices(cards, weights=weights, k=1)[0]
    return chosen, reason_map[chosen["persona_id"]]


def maybe_interrupt(card: dict, phase: str) -> bool:
    """按人设 interrupt_rate 掷骰子决定是否抢话（仅交锋/收敛阶段）。"""
    if phase not in ("discussion", "convergence"):
        return False
    return random.random() < card["interrupt_rate"]


# ————————————————————————————————————————————————
# 6. 回应方式选择（agree / refute / probe）
# ————————————————————————————————————————————————
def pick_reply_mode(card: dict, target_card: dict | None, target_is_user: bool,
                    user_stance: str) -> str:
    """按人设 agree_bias + 双方立场关系决定回应方式。

    - 与上文同立场 → 倾向 agree；对立立场 → 倾向 refute；
    - 分析型/质疑型 probe 概率更高；
    - 划水型几乎只 agree（被点名 answer 模式在外层单独处理）。
    """
    r = random.random()
    bias = card["agree_bias"]
    # 立场关系修正
    if target_is_user:
        opposite = card["stance"] in ("support", "oppose") and user_stance in ("support", "oppose") \
            and card["stance"] != user_stance
    elif target_card:
        opposite = card["stance"] in ("support", "oppose") and target_card["stance"] in ("support", "oppose") \
            and card["stance"] != target_card["stance"]
    else:
        opposite = False
    if opposite:
        bias -= 0.3
    if card["archetype"] in ("analyst", "questioner"):
        probe_p = 0.28
    elif card["archetype"] == "slacker":
        probe_p = 0.05
    else:
        probe_p = 0.15
    bias = max(0.05, min(0.9, bias))
    if r < bias:
        return "agree"
    if r < bias + probe_p:
        return "probe"
    return "refute"


# ————————————————————————————————————————————————
# 7. 规则降级话术（LLM 不可用时；同样锚定观点、同样区分人设）
# ————————————————————————————————————————————————
_RULE_FRAMES: dict[str, dict[str, list[str]]] = {
    # 每种人设 × 每种回应方式的话术骨架，{point}=被回应观点，{stance}=立场
    "aggressive": {
        "agree": ["「{point}」这话在点上，我补一刀：既然对，就别磨叽，马上排期干。",
                  "同意「{point}」，但光认同没用，我加一句：窗口期就这两年，必须现在动。"],
        "refute": ["「{point}」？我不同意。等你们把风险讨论完，机会早没了，{stance}。",
                   "这话太保守了——「{point}」听起来稳，实际是把市场拱手让人。"],
        "probe": ["我追一句：「{point}」谁来做、几个月见效？拿不出路径就是空谈。",
                  "等一下，「{point}」的依据是什么？拍脑袋可不算。"],
        "opening": ["我先抛个砖啊，我看问题就一个标准：能不能快，{stance}。",
                    "我就直给了：这种事最怕拖，我是来推动结论的，{stance}。"],
        "statement": ["我的立场很明确：{stance}。理由就一条，窗口期不等人，算了再干等于不干。",
                      "我先说结论，{stance}——速度本身就是护城河。"],
        "converge": ["别绕了，能定的就两条：方向认「{point}」，责任到人，散会就能开工。",
                     "我表个态：「{point}」我认，剩下的细节会后补，先把结论锁了。"],
        "answer": ["行，我说。「{point}」我不是没听见，我就是觉得与其等共识，不如先跑起来。"],
    },
    "slacker": {
        "agree": ["嗯……「{point}」我没什么意见，我也差不多这个想法。",
                  "对对，「{point}」挺好的，大家定就行，我跟着。"],
        "refute": ["呃，非要我说的话……「{point}」会不会有点冒进？不过你们都同意我也行。",
                   "我倒不是反对啊，就是「{point}」听着压力挺大的……算了，听大家的。"],
        "probe": ["我想问一下……「{point}」这个，出了问题谁负责啊？",
                  "呃，那「{point}」要不要再加个备选？我随便一说。"],
        "opening": ["大家好……那我就先听着，有想法我再补充。",
                    "我没什么准备，你们先说，我跟大家节奏走。"],
        "statement": ["我嘛……{stance}，其实两边都有道理，我比较认同刚才同学说的。",
                      "我的想法不成熟啊，{stance}，主要是别太折腾就行。"],
        "converge": ["我觉得大家说得都挺好……要不就按「{point}」定？我都可以。",
                     "我没补充了，「{point}」能收就收吧，别拖太晚。"],
        "answer": ["啊？点我了啊……那我就两句，「{point}」我没意见，真要做我配合就是了。"],
    },
    "analyst": {
        "agree": ["「{point}」这个判断我可以支持，但先把口径定清楚：用什么指标验证、多长周期。",
                  "方向同意「{point}」，补一个数据视角：先看样本，再谈结论，别用个例推全局。"],
        "refute": ["我质疑的不是态度，是「{point}」的论证链——相关性不等于因果，数据支撑不足。",
                   "「{point}」这个结论的样本有偏，按这个口径推，结论是不可证伪的。"],
        "probe": ["追问一个关键问题：「{point}」的对照组在哪？没有对照，这个数说明不了问题。",
                  "我想确认口径：「{point}」说的增长，是同比还是环比？基数是什么？"],
        "opening": ["我习惯先对齐口径再讨论，先不下结论，先看数据怎么说。",
                    "我的角度比较简单：任何观点先问可不可证伪。"],
        "statement": ["我的立场是：{stance}。因为现有数据并不支持更强的结论，先证伪再站队。",
                      "我倾向{stance}，但这是个假设，需要用指标验证，不是拍板。"],
        "converge": ["那我把共识数据化一下：「{point}」可以定，但附一个验证指标和一个止损线。",
                     "收敛可以，建议把「{point}」写成可量化的结论，分歧点单列、标责任人。"],
        "answer": ["好，我回应。「{point}」要成立需要三个前提，缺一个我就保留意见，先列出来。"],
    },
    "integrator": {
        "agree": ["我特别理解「{point}」，这和刚才另一位同学的担心其实不冲突，可以合并。",
                  "顺着「{point}」补一句，我们把双方诉求都装进来，结论会更稳。"],
        "refute": ["我不是反对「{point}」，而是担心只强调它会丢掉另一边合理的诉求。",
                   "「{point}」有道理，但说成非此即彼我就不同意了，中间地带是存在的。"],
        "probe": ["我帮两位问一句：你们争的「{point}」，是不是其实只是优先级不同？",
                  "想确认下，反对「{point}」的同学，具体担心的是哪一步？我们对症解决。"],
        "opening": ["很高兴和大家一组，我的习惯是多听多记，帮大家把共同点攒起来。",
                    "我先不站队，今天的目标是让每个人的好想法都不被漏掉。"],
        "statement": ["我的立场偏中立：{stance}，因为我更关心最后能不能形成大家都能执行的结论。",
                      "我表个态，{stance}，但保留根据讨论调整的空间。"],
        "converge": ["我来归纳一下：我们其实在「{point}」上已经一致了，分歧只剩节奏，可以定。",
                     "求个最大公约数吧——「{point}」作为共同结论，分歧写进风险项，可行吗？"],
        "answer": ["好的，我来接。关于「{point}」，我建议把它翻译成各方都能接受的表述，这样……"],
    },
    "steady": {
        "agree": ["「{point}」方向我不反对，但请同步回答一个问题：最坏情况下，谁兜底？",
                  "同意「{point}」，前提是把退路想清楚，先设止损线再推进。"],
        "refute": ["我必须泼盆冷水：「{point}」的下行风险没有定价，真出问题代价是全组承担。",
                   "先别急——「{point}」成立的前提太多，任何一个不成立就是连锁反应。"],
        "probe": ["我问个保守的问题：「{point}」如果失败，退出机制是什么？沉没成本谁认？",
                  "再确认一下，「{point}」对应的合规和责任边界，有人核过吗？"],
        "opening": ["我先听大家说，我的习惯是先找坑，坑找完了再谈速度。",
                    "我来自风控视角，今天会多问几个“万一”。"],
        "statement": ["我的立场很清楚：{stance}。在没看清最坏情况之前，我不支持冒进。",
                      "我倾向{stance}，理由是这个议题里不可控变量太多，安全边际不够。"],
        "converge": ["要收敛我同意，但「{point}」必须附带风险预案，没有兜底的结论我不签字。",
                     "可以定「{point}」，条件是分阶段推进，每阶段设一个可回退的检查点。"],
        "answer": ["好，我说。「{point}」我最担心的是连锁风险，所以我的态度是可以试，但必须带刹车。"],
    },
    "questioner": {
        "agree": ["即便我倾向认同「{point}」，也要先问一句：这个结论的前提真的成立吗？",
                  "「{point}」暂时可以接受，但别把暂时解当终极答案。"],
        "refute": ["等一下，「{point}」偷换了概念——你说的 A 和你证明的 B 不是一回事。",
                   "我拆一下「{point}」：大前提就有问题，后面推得再顺也没用。"],
        "probe": ["我追问：如果「{point}」的前提不成立，你这套结论还剩下什么？",
                  "那请定义一下你说的「{point}」——边界在哪？反例算不算数？"],
        "opening": ["我今天负责挑刺，别介意啊——结论越经得起我问，才越靠谱。",
                    "我先不亮立场，我想先看看大家的论证能不能站住。"],
        "statement": ["我倾向{stance}，但比起立场，我更想先拆对方论证里的漏洞。",
                      "暂时{stance}，除非有人能回答我后面这几个质疑。"],
        "converge": ["在定「{point}」之前，我最后问一次——这个结论能经受反例检验吗？能，我就认。",
                     "收敛可以，但请把我们没解决的那个反驳也记录在案，别假装达成了共识。"],
        "answer": ["行，我接住。「{point}」要过我这关，先回答我的前提质疑，不然我保留意见。"],
    },
}


def rule_utterance(card: dict, mode: str, anchors: list[dict], interrupt: bool) -> str:
    """规则降级：按人设×回应方式取话术骨架，并填入被回应观点（保证锚定）。"""
    point = anchors[-1]["summary"] if anchors else "刚才这个观点"
    frames = _RULE_FRAMES.get(card["archetype"], _RULE_FRAMES["analyst"])
    bucket = frames.get(mode) or frames.get("statement") or list(frames.values())[0]
    text = random.choice(bucket).format(point=point, stance=card.get("stance_phrase", "我有保留意见"))
    if interrupt:
        text = "等一下，我插一句——" + text
    return text


# ————————————————————————————————————————————————
# 8. 单条发言动作生成（LLM 优先，失败降级规则）
# ————————————————————————————————————————————————
def _phase_default_mode(phase: str) -> str:
    return {"opening": "opening", "statement": "statement",
            "discussion": "refute", "convergence": "converge",
            "summary": "summary"}[phase]


def make_peer_action(card: dict, topic: str, phase: str, working_history: list[dict],
                     *, mode: str | None = None, interrupt: bool = False,
                     force_reply_to: str | None = None,
                     anchors: list[dict] | None = None,
                     engine_log: list | None = None) -> dict:
    """生成一条候选人发言动作。

    :param mode: 指定回应方式；None 时按阶段给默认模式（opening/statement 等无锚定模式）
    :param force_reply_to: 强制回应对象显示名（主持人点名场景）
    :param anchors: 可外部注入锚点；默认从 working_history 末尾提取
    """
    if anchors is None:
        anchors = _anchors_from(working_history)
    if mode is None:
        mode = _phase_default_mode(phase)
    text = llm.generate_peer_utterance(
        card=card, topic=topic,
        phase_hint=PHASE_MAP[phase]["llm_hint"],
        reply_mode=mode, anchors=anchors, interrupt=interrupt,
    )
    engine = "deepseek"
    if not text:
        text = rule_utterance(card, mode, anchors, interrupt)
        engine = "rule"
    if engine_log is not None:
        engine_log.append(engine)

    # 回应对象显示名：锚点最后一条的发言者；锚到自己（如自己做全场总结）时不显示指向
    if force_reply_to:
        reply_to = force_reply_to
    elif anchors:
        last_speaker = anchors[-1]["speaker"]
        reply_to = "你" if "你" in last_speaker else last_speaker
        if reply_to == card["name"]:
            reply_to = ""
    else:
        reply_to = ""
    return {
        "type": "peer",
        "persona_id": card["persona_id"],
        "name": card["name"],
        "style": card["style"],
        "color": card["color"],
        "archetype": card["archetype_label"],
        "text": text,
        "reply_to": reply_to,
        "reply_mode": mode if mode in ("agree", "refute", "probe") else "",
        "interrupt": interrupt,
        "engine": engine,
    }


def moderator_action(text: str, subtype: str) -> dict:
    """构造主持人动作（不参与答题，只控场）。"""
    return {"type": "moderator", "name": "主持人", "subtype": subtype, "text": text}


# ————————————————————————————————————————————————
# 9. 导演主入口：根据事件产出一批动作（发言/主持）+ 阶段推进
# ————————————————————————————————————————————————
_MODERATOR_OPEN = "各位好，欢迎来到无领导小组讨论环节。今天的议题是：{topic}。请先用一句话做个破冰，让大家认识你的思考角度。"
_MODERATOR_TO_STATEMENT = "破冰就到这里，下面进入个人陈述：请依次亮明你的立场和理由。"
_MODERATOR_TO_DISCUSSION = "立场都清楚了，进入自由交锋——可以认同补充，也可以直接反驳、追问。"
_MODERATOR_TO_CONVERGENCE = "讨论得比较充分了，下面进入收敛阶段：停止发散，找最大公约数，推动小组形成结论。"
_MODERATOR_TO_SUMMARY = "时间差不多了，进入总结陈词，请一位同学代表小组归纳今天的结论。"
_MODERATOR_CLOSE = "感谢各位。今天的讨论有交锋也有共识，小组结论已经形成，本场无领导小组讨论到此结束。"


def _short_topic(topic: str, n: int = 22) -> str:
    t = (topic or "今天的议题").strip()
    # 去掉末尾句读，避免与主持人文案里的句号叠成"。。"
    t = t.rstrip("。.!！?？；;，,")
    return t[:n] + ("…" if len(t) > n else "")


def direct(topic: str, phase: str, event: dict, history: list[dict],
           cards: list[dict]) -> dict:
    """导演：根据事件 + 历史，决定接下来发生什么。

    :param event: {"type": "enter"|"user_spoke"|"advance", "text": "..."}
                  - enter：进入某阶段（开场/手动切阶段后播放阶段动作）
                  - user_spoke：用户刚发言（text 必填，且已包含在 history 中）
                  - advance：用户手动推进到下一阶段
    :return: {"actions": [...], "phase": 当前阶段key, "phase_advanced": bool,
              "finished": bool, "round": 当前阶段轮次, "phase_info": {...}}
    """
    phase = phase if phase in PHASE_MAP else "opening"
    event_type = event.get("type", "enter")

    # 无人设卡时（候选人库异常）的安全兜底：不产生发言动作，避免加权采样空列表崩溃
    if not cards:
        return {
            "actions": [moderator_action("候选人阵容加载失败，请刷新页面重试。", "prompt_user")],
            "phase": phase, "phase_advanced": False, "finished": False,
            "round": {}, "engines": [], "phase_info": PHASE_MAP[phase],
        }

    # advance：先推进阶段，再按 enter 处理新阶段
    phase_advanced = False
    if event_type == "advance":
        idx = PHASE_KEYS.index(phase)
        if idx < len(PHASE_KEYS) - 1:
            phase = PHASE_KEYS[idx + 1]
            phase_advanced = True
        event_type = "enter"

    state = rebuild_state(phase, cards, history)
    working = [dict(m) for m in history]  # 本轮新生成的发言也追加进去，形成链式锚定
    actions: list[dict] = []
    engines: list[str] = []
    finished = False

    def append_peer(card, *, mode=None, interrupt=False, force_reply_to=None, anchors=None):
        act = make_peer_action(
            card, topic, phase, working, mode=mode, interrupt=interrupt,
            force_reply_to=force_reply_to, anchors=anchors, engine_log=engines)
        actions.append(act)
        working.append({
            "who": "peer", "name": card["name"], "persona_id": card["persona_id"],
            "text": act["text"], "phase": phase,
            "reply_mode": act["reply_mode"], "interrupt": act["interrupt"],
        })
        return act

    def pick_dynamic(exclude_last=True):
        weighted = score_speakers(state, exclude_last=exclude_last)
        card, reasons = weighted_pick(weighted)
        # 把选中者在 working 视角的状态同步给 state（供连续选第 2 人时冷却生效）
        return card, reasons

    def sync_state():
        """连续生成多条发言后，用 working 重建 state，保证冷却/锚点链正确。"""
        nonlocal state
        state = rebuild_state(phase, cards, working)

    # —— 阶段进入动作 ——
    if event_type == "enter":
        if phase == "opening":
            actions.append(moderator_action(_MODERATOR_OPEN.format(topic=_short_topic(topic, 40)), "open"))
            # 破冰：选一个人（不一定是激进派，加权随机）说第一句话
            card, _ = pick_dynamic(exclude_last=False)
            append_peer(card, mode="opening")
        elif phase == "statement":
            actions.append(moderator_action(_MODERATOR_TO_STATEMENT, "transition"))
            card, _ = pick_dynamic(exclude_last=False)
            append_peer(card, mode="statement")
        elif phase == "discussion":
            actions.append(moderator_action(_MODERATOR_TO_DISCUSSION, "transition"))
        elif phase == "convergence":
            actions.append(moderator_action(_MODERATOR_TO_CONVERGENCE, "transition"))
            # 调和型开启收敛（打分天然偏向 integrator，但仍走加权随机）
            card, _ = pick_dynamic(exclude_last=False)
            append_peer(card, mode="converge")
        elif phase == "summary":
            actions.append(moderator_action(_MODERATOR_TO_SUMMARY, "transition"))
            card, _ = pick_dynamic(exclude_last=False)
            anchors_all = _summarize_all(working, topic)
            append_peer(card, mode="summary", anchors=anchors_all)

    # —— 用户发言后的反应（每个阶段都触发）——
    # event_type == "continue"：前端播完上一批动作后自动拉取（仅陈述阶段用于逐位推进）
    elif event_type in ("user_spoke", "continue"):
        if phase == "opening" and event_type == "user_spoke":
            # 1 位候选人回应破冰，随后主持人切到个人陈述并由 1 位候选人先陈述
            card, _ = pick_dynamic()
            append_peer(card, mode="opening")
            actions.append(moderator_action(_MODERATOR_TO_STATEMENT, "transition"))
            phase = "statement"
            phase_advanced = True
            sync_state()
            card, _ = pick_dynamic(exclude_last=False)
            append_peer(card, mode="statement")

        elif phase == "statement":
            # 陈述阶段：优先让还没陈述的人发言；全部陈述完 → 自动进入自由交锋
            # （user_spoke 与 continue 都可驱动，前端在播完一位后自动 continue）
            not_stated = [c for c in cards if state.stats[c["persona_id"]].phase_count.get("statement", 0) == 0]
            user_stated = "statement" in state.user_spoke_phases
            if not_stated:
                # 只在未陈述者中加权随机
                weighted = [x for x in score_speakers(state) if x[0] in not_stated]
                card = random.choices([x[0] for x in weighted],
                                      weights=[x[1] for x in weighted], k=1)[0]
                append_peer(card, mode="statement")
            elif user_stated:
                actions.append(moderator_action(_MODERATOR_TO_DISCUSSION, "transition"))
                phase = "discussion"
                phase_advanced = True
                sync_state()
            elif event_type == "user_spoke":
                # 用户尚未陈述，提示用户
                actions.append(moderator_action("也请你用一句话亮明立场。", "prompt_user"))

        elif phase in ("discussion", "convergence") and event_type == "user_spoke":
            # 自由交锋/收敛：1-2 人动态回应，允许抢话、允许主持人冷场点名
            n_respond = 2 if random.random() < 0.35 else 1
            for i in range(n_respond):
                sync_state()
                weighted = score_speakers(state)
                card, _ = weighted_pick(weighted)
                interrupt = maybe_interrupt(card, phase) and i == 0
                # 回应方式：第 1 人锚定用户；第 2 人有 55% 概率锚定第 1 人（peer 间互咬）
                if i == 0:
                    # 第 1 人锚定用户最新观点，回应方式按人设加权
                    mode = pick_reply_mode(
                        card, None, True, rebuild_state(phase, cards, working).user_stance)
                    append_peer(card, mode=mode, interrupt=interrupt)
                else:
                    prev = actions[-1]
                    bite_peer = random.random() < 0.55 and prev["type"] == "peer"
                    if bite_peer:
                        prev_card = next((c for c in cards if c["persona_id"] == prev["persona_id"]), None)
                        mode = pick_reply_mode(card, prev_card, False, state.user_stance)
                        append_peer(card, mode=mode,
                                    interrupt=maybe_interrupt(card, phase))
                    else:
                        # 不咬 peer 而是仍回应用户：显式锚定用户最近一条发言
                        user_anchor = None
                        for m in reversed(working):
                            if m.get("who") == "me" and m.get("text"):
                                user_anchor = [{
                                    "speaker": "你（候选人本人）",
                                    "summary": summarize(m["text"]),
                                }]
                                break
                        mode = pick_reply_mode(card, None, True, state.user_stance)
                        append_peer(card, mode=mode, anchors=user_anchor)
            # 主持人控场：本阶段已有 ≥5 条实质发言、且有人全程沉默 → 冷场点名
            sync_state()
            silent = [c for c in cards if state.stats[c["persona_id"]].phase_count.get(phase, 0) == 0]
            if state.phase_rounds >= 5 and silent:
                target = random.choice(silent)
                actions.append(moderator_action(
                    f"我们还没太听到{target['name']}的看法，{target['name']}，你怎么看？", "cold_call"))
                sync_state()
                # 点名前先把主持人话术也放进 working（不计实质发言），被点名者锚点仍是用户最新观点
                append_peer(target, mode="answer", force_reply_to="主持人")
            # 随机控场噪音：讨论发散时拉回议题
            elif phase == "discussion" and state.phase_rounds >= 6 and random.random() < 0.12:
                actions.append(moderator_action(
                    f"大家稍微有点发散了，拉回到议题本身：{_short_topic(topic)}。", "pull_back"))

            # 阶段轮次超限 → 自动推进
            sync_state()
            if state.phase_rounds >= PHASE_MAP[phase]["max_rounds"]:
                nxt = "convergence" if phase == "discussion" else "summary"
                line = _MODERATOR_TO_CONVERGENCE if nxt == "convergence" else _MODERATOR_TO_SUMMARY
                actions.append(moderator_action(line, "transition"))
                phase = nxt
                phase_advanced = True
                sync_state()
                if phase == "convergence":
                    card, _ = weighted_pick(score_speakers(rebuild_state(phase, cards, working)))
                    append_peer(card, mode="converge")
                else:
                    # 进入总结：调和型代表全组归纳全场观点
                    card, _ = weighted_pick(score_speakers(rebuild_state(phase, cards, working)))
                    append_peer(card, mode="summary", anchors=_summarize_all(working, topic))

        elif phase == "summary" and event_type == "user_spoke":
            # 用户陈词后：稳健派/质疑派补一句收尾，主持人宣布结束
            sync_state()
            weighted = score_speakers(state)
            # 总结阶段 integrator 已做过主总结，这里偏向其他人简短收尾
            weighted = [(c, w * (0.3 if c["archetype"] == "integrator" else 1.0), r)
                        for c, w, r in weighted]
            card, _ = weighted_pick(weighted)
            append_peer(card, mode="converge")
            actions.append(moderator_action(_MODERATOR_CLOSE, "close"))
            finished = True

    # 返回时重新按最终 phase 统计轮次
    final_state = rebuild_state(phase, cards, working)
    return {
        "actions": actions,
        "phase": phase,
        "phase_advanced": phase_advanced,
        "finished": finished,
        "round": final_state.phase_rounds,
        "engines": engines,
        "phase_info": PHASE_MAP[phase],
    }


def _summarize_all(working: list[dict], topic: str) -> list[dict]:
    """总结陈词用：从全场发言中挑 3 条信息密度最高的观点做锚点（简单关键词启发式）。"""
    scored = []
    kws = ("认为", "建议", "数据", "风险", "共识", "应该", "支持", "反对", "结论", "关键")
    for m in working:
        if m.get("who") not in ("me", "peer") or not m.get("text"):
            continue
        t = m["text"]
        score = sum(k in t for k in kws) + min(len(t) / 30, 3)
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    picked = scored[:3]
    anchors = [{
        "speaker": "你（候选人本人）" if m["who"] == "me" else m.get("name", "候选人"),
        "summary": summarize(m["text"], 30),
    } for _, m in picked]
    if not anchors:
        anchors = [{"speaker": "全场", "summary": _short_topic(topic, 30)}]
    return anchors
