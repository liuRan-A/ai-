"""模拟场次路由"""
import os
import secrets
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.common import R
from app.schemas.session import SessionCreate, SessionOut, SessionDetail, AnswerSubmit, SessionQuestionDetail
from app.crud import session as sc
from app.crud import question as qc
from app.models.session import SessionQuestion

router = APIRouter(prefix="/sessions", tags=["session"])


@router.post("", response_model=R, summary="开始一场模拟")
def create_session(
    payload: SessionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    qset = qc.get_set(db, payload.set_id)
    if not qset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "套题不存在")
    cfg = sc.create_config(db, user.id,
                           position_id=payload.position_id,
                           form_type=payload.form_type,
                           voice_mode=payload.voice_mode,
                           difficulty=payload.difficulty,
                           enable_followup=int(payload.enable_followup),
                           peer_count=payload.peer_count)
    sess = sc.start_session(db, user.id, cfg, qset,
                           use_resume=payload.use_resume,
                           resume_count=payload.resume_count)
    db.commit()
    db.refresh(sess)
    return R.ok({"session_id": sess.id, "status": sess.status})


@router.get("/{session_id}", response_model=R, summary="场次详情（含所有题）")
def get_session(session_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    sess = sc.get_session(db, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场次不存在")
    sqs = sc.list_session_questions(db, session_id)
    # 联表带出题干信息（/answer/:sessionId 直达时前端没有套题上下文）
    from app.models.question import Question
    qmap = {}
    qids = [s.question_id for s in sqs if s.question_id]
    if qids:
        for q in db.query(Question).filter(Question.id.in_(qids)).all():
            qmap[q.id] = q
    out = SessionDetail.from_orm(sess).dict()
    out["questions"] = [
        {
            "id": s.id, "session_id": s.session_id, "question_id": s.question_id,
            "seq": s.seq, "phase": s.phase, "status": s.status,
            "transcript": s.transcript, "audio_url": s.audio_url,
            "video_url": s.video_url or "",
            "ref_answer": s.ref_answer, "total_score": s.total_score,
            "duration_ms": s.duration_ms,
            # 动态题（简历出题）优先取 SessionQuestion 自带内容，否则取题库联表
            "question_content": (s.content or (qmap.get(s.question_id).content if qmap.get(s.question_id) else "") or ""),
            "question_category": (s.category or (qmap.get(s.question_id).category if qmap.get(s.question_id) else "") or ""),
            "question_dimension": (s.dimension or (qmap.get(s.question_id).dimension if qmap.get(s.question_id) else "") or ""),
            "time_limit_s": s.time_limit_s or (qmap.get(s.question_id).time_limit_s if qmap.get(s.question_id) else 120) or 120,
            "source": s.source or "bank",
        }
        for s in sqs
    ]
    return R.ok(out)


@router.post("/{session_id}/questions/{sq_id}/answer", response_model=R, summary="提交单题作答")
def submit_answer(
    session_id: int,
    sq_id: int,
    payload: AnswerSubmit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sq = sc.get_session_question(db, sq_id)
    if not sq or sq.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    sc.submit_answer(db, sq, payload.transcript, payload.audio_url, payload.duration_ms)
    db.commit()
    return R.ok({"total_score": float(sq.total_score or 0)})


@router.post("/{session_id}/questions/{sq_id}/video", response_model=R, summary="上传单题录像")
async def upload_question_video(
    session_id: int,
    sq_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """结构化视频面试：MediaRecorder 录制的逐题 webm 录像上传持久化"""
    sess = sc.get_session(db, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场次不存在")
    sq = sc.get_session_question(db, sq_id)
    if not sq or sq.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    ctype = file.content_type or ""
    if not ctype.startswith("video/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "仅支持视频文件")
    # 复用 main.UPLOAD_DIR（backend/uploads），落 videos 子目录
    from app.main import UPLOAD_DIR
    video_dir = os.path.join(UPLOAD_DIR, "videos")
    os.makedirs(video_dir, exist_ok=True)
    ext = ".mp4" if "mp4" in ctype else ".webm"
    fname = f"sess{session_id}_sq{sq_id}_{secrets.token_hex(4)}{ext}"
    fpath = os.path.join(video_dir, fname)
    size = 0
    try:
        with open(fpath, "wb") as f:
            while True:
                chunk = await file.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "视频过大（上限 50MB）")
                f.write(chunk)
    except HTTPException:
        if os.path.exists(fpath):
            os.remove(fpath)
        raise
    url = f"/uploads/videos/{fname}"
    sq.video_url = url
    db.commit()
    return R.ok({"video_url": url, "size": size})


@router.post("/{session_id}/finish", response_model=R, summary="结束场次 + 生成报告")
def finish_session(session_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    sess = sc.get_session(db, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场次不存在")
    rep = sc.finish_session(db, sess)
    if not rep:
        return R.fail("无作答记录，无法生成报告")
    return R.ok({"report_id": rep.id, "session_id": sess.id, "total_score": float(rep.total_score or 0)})


@router.get("/{session_id}/questions/{sq_id}", response_model=R, summary="逐题详情回放")
def get_question_detail(
    session_id: int,
    sq_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    detail = sc.get_session_question(db, sq_id)
    if not detail or detail.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    # 复用 report crud 的逐题详情
    from app.crud import report as rc
    res = rc.get_session_question_detail(db, sq_id)
    if not res:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "详情不存在")
    sq, question, scores, metrics, highlights, recommendations = (
        res["sq"], res["question"], res["scores"], res["metrics"], res["highlights"], res["recommendations"]
    )
    out = {
        "id": sq.id, "session_id": sq.session_id, "question_id": sq.question_id,
        "seq": sq.seq, "phase": sq.phase, "status": sq.status,
        "transcript": sq.transcript, "audio_url": sq.audio_url,
        "ref_answer": sq.ref_answer, "total_score": float(sq.total_score) if sq.total_score is not None else None,
        "duration_ms": sq.duration_ms, "started_at": sq.started_at, "answered_at": sq.answered_at,
        "scores": scores,
        "metrics": {
            "wpm": metrics.wpm, "pause_count": metrics.pause_count,
            "pause_ms_total": metrics.pause_ms_total, "filler_count": metrics.filler_count,
            "interrupt_count": metrics.interrupt_count,
            "extra_json": getattr(metrics, "extra_json", None),
        } if metrics is not None else {
            "wpm": 0, "pause_count": 0, "pause_ms_total": 0,
            "filler_count": 0, "interrupt_count": 0, "extra_json": None,
        },
        "highlights": highlights,
        "recommendations": recommendations,
        "question_content": res.get("content") or (question.content if question else ""),
        "question_category": res.get("category") or (question.category if question else ""),
        "question_dimension": res.get("dimension") or (question.dimension if question else ""),
        "time_limit_s": sq.time_limit_s or (question.time_limit_s if question else 120),
        "source": sq.source or "bank",
        # v1.1：答得好的地方 / 欠缺的地方 / 结构化参考答案
        "strengths": sq.strengths or [],
        "gaps": sq.gaps or [],
        "ref_detail": sq.ref_detail,
    }
    return R.ok(out)


# —— AI 理解 + 建议接口（实时反馈） ——
class FeedbackReq(BaseModel):
    transcript: str
    question_dimension: str = ""
    question_content: str = ""


@router.post("/questions/{sq_id}/feedback")
def get_feedback(
    sq_id: int, body: FeedbackReq,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """AI 理解候选人的回答并给出实时建议"""
    from app.crud.session import analyze_transcript
    sq = db.get(SessionQuestion, sq_id)
    if not sq:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    feedback = analyze_transcript(body.transcript, body.question_dimension, body.question_content)
    return R.ok(feedback)


# —— 无领导小组讨论：AI 虚拟候选人 ——
@router.get("/{session_id}/peers", response_model=R, summary="本场 AI 候选人列表（群面）")
def get_session_peers(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = sc.get_session(db, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场次不存在")
    if sess.form_type != "group":
        return R.ok([])
    from app.models.question import PeerPersona
    from app.services.group_discussion import select_session_personas, build_cards
    limit = 5
    if sess.config_id:
        from app.models.session import InterviewConfig
        cfg = db.get(InterviewConfig, sess.config_id)
        if cfg and cfg.peer_count:
            limit = cfg.peer_count
    dedicated = db.query(PeerPersona).filter(PeerPersona.set_id == sess.set_id).all() if sess.set_id else []
    generic = db.query(PeerPersona).filter(PeerPersona.set_id.is_(None)).all()
    personas = select_session_personas(dedicated, generic, limit=max(1, limit))
    cards = build_cards(personas)
    return R.ok([
        {
            "id": c["persona_id"], "name": c["name"], "style": c["style"], "color": c["color"],
            "bio": c["bio"], "aggressiveness": max(1, round(c["talkativeness"] * 5)),
            # —— 多智能体调度特质（新增字段，旧前端忽略不影响）——
            "archetype": c["archetype"], "archetype_label": c["archetype_label"],
            "talkativeness": c["talkativeness"], "agree_bias": c["agree_bias"],
            "interrupt_rate": c["interrupt_rate"], "stance": c["stance"],
        }
        for c in cards
    ])


class PeerTalkReq(BaseModel):
    stage: str = "debate"        # opening / debate / summary
    persona_id: int
    stance: str = ""             # 该候选人所持立场的论点陈述
    topic: str = ""              # 辩题
    target_name: str = ""        # 反驳对象（候选人名或候选人=你）
    other_name: str = ""         # 场上另一位候选人名（整合派话术用）
    user_text: str = ""          # 被反驳的原话（从中摘取观点）
    # —— 上下文：让候选人能引用「最近的对话」而不是只看 user_text ——
    # [{"who":"me"|"peer","name":"...","text":"..."}]
    recent_context: list[dict] = []
    # 这一次发言主要是回应谁：me=候选人 / 某 peer 名 = 回应另一位候选人
    respond_to: str = "me"


@router.post("/{session_id}/peer-talk", response_model=R, summary="生成 AI 候选人发言（群面）")
def peer_talk(
    session_id: int,
    body: PeerTalkReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = sc.get_session(db, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场次不存在")
    from app.models.question import PeerPersona
    from app.crud.peer import generate_peer_talk
    persona = db.get(PeerPersona, body.persona_id)
    if not persona:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    text = generate_peer_talk(
        stage=body.stage if body.stage in ("opening", "debate", "summary") else "debate",
        persona=persona,
        stance=body.stance,
        topic=body.topic,
        target_name=body.target_name,
        user_text=body.user_text,
        other_name=body.other_name,
        recent_context=body.recent_context or [],
        respond_to=body.respond_to or "me",
    )
    return R.ok({
        "persona_id": persona.id, "name": persona.name,
        "style": persona.style, "color": persona.color, "text": text,
    })


# —— 群面多智能体：事件驱动导演接口 ——
class GroupDirectReq(BaseModel):
    phase: str = "opening"          # opening/statement/discussion/convergence/summary
    event: dict = {"type": "enter"}  # {"type":"enter"|"user_spoke"|"advance","text":"..."}
    topic: str = ""
    # 本场候选人（前端从 GET /peers 拿到的 id 列表，顺序即上场顺序）
    persona_ids: list[int] = []
    # 到目前为止的完整讨论历史：
    # [{"who":"me"|"peer"|"moderator","name":"...","text":"...","phase":"discussion",
    #   "reply_mode":"agree|refute|probe","interrupt":false}]
    history: list[dict] = []


@router.post("/{session_id}/group/direct", response_model=R, summary="群面导演：动态选人+生成下一批动作")
def group_direct(
    session_id: int,
    body: GroupDirectReq,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """无领导小组讨论的调度中枢。

    前端只负责上报「发生了什么事件 + 完整历史」，后端决定：谁发言（加权随机）、
    回应谁、用什么方式（认同/反驳/追问）、主持人是否点名/拉回/切阶段，
    并生成发言文本（LLM 优先，失败自动降级规则话术）。
    """
    sess = sc.get_session(db, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "场次不存在")
    from app.models.question import PeerPersona
    from app.services import group_discussion as gd

    ids = body.persona_ids or []
    personas = db.query(PeerPersona).filter(PeerPersona.id.in_(ids)).all() if ids else []
    id_map = {p.id: p for p in personas}
    ordered = [id_map[i] for i in ids if i in id_map]
    cards = gd.build_cards(ordered)

    result = gd.direct(
        topic=body.topic or "",
        phase=body.phase,
        event=body.event or {"type": "enter"},
        history=body.history or [],
        cards=cards,
    )
    return R.ok(result)
