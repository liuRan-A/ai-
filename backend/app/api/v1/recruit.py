# -*- coding: utf-8 -*-
"""招聘管理路由（管理员专用）：题库 CRUD / AI 面试提纲 / 简历筛选 / 终面对比 / 语音多维度分析"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.api.deps import get_admin_user
from app.models.user import User
from app.models.position import Position, Resume
from app.models.question import QuestionSet, Question
from app.models.recruit import InterviewOutline, ResumeScreening
from app.models.session import InterviewSession, SessionQuestion, SessionScore, SessionMetrics
from app.schemas.common import R
from app.services.llm import (
    generate_interview_outline,
    generate_resume_questions,
    screen_resume,
    analyze_voice_profile,
    DIMENSIONS,
)

router = APIRouter(prefix="/admin", tags=["recruit"])

_FORM_TYPES = {"structured", "group", "semi"}
_DIFFICULTIES = {"easy", "medium", "hard"}


# ============ 题库管理 ============
def _q_out(q: Question) -> dict:
    return {
        "id": q.id, "seq": q.seq,
        "content": q.content,
        "dimension": q.dimension or "",
        "category": q.category or "",
        "difficulty": q.difficulty,
        "time_limit_s": q.time_limit_s,
        "ref_answer": q.ref_answer or "",
    }


def _set_out(s: QuestionSet, with_questions: bool = False) -> dict:
    out = {
        "id": s.id, "name": s.name,
        "industry": s.industry or "",
        "position_type": s.position_type or "",
        "form_type": s.form_type,
        "difficulty": s.difficulty,
        "description": s.description or "",
        "question_count": s.question_count,
        "est_minutes": s.est_minutes,
        "is_published": s.is_published,
        "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "",
    }
    if with_questions:
        out["questions"] = [_q_out(q) for q in
                            sorted(s.questions, key=lambda x: x.seq)]
    return out


def _check_set_fields(data: dict) -> None:
    if not str(data.get("name") or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "套题名称不能为空")
    if data.get("form_type") not in _FORM_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "面试形式非法")
    if data.get("difficulty") not in _DIFFICULTIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "难度非法")


def _apply_questions(db: Session, qs: QuestionSet, items: list) -> None:
    """整包替换套题题目（级联删除旧题），服务端写 seq 并回填统计"""
    clean = []
    for i, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        clean.append(Question(
            set_id=qs.id, seq=i,
            form_type=qs.form_type, difficulty=qs.difficulty,
            category=str(item.get("category") or "").strip()[:32],
            dimension=str(item.get("dimension") or "").strip()[:32],
            content=content,
            ref_answer=str(item.get("ref_answer") or "").strip() or None,
            time_limit_s=int(item.get("time_limit_s") or 120),
        ))
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少需要 1 道有效题目")
    qs.questions = clean
    db.flush()
    qs.question_count = len(clean)
    qs.est_minutes = round(sum(q.time_limit_s for q in clean) / 60)


@router.get("/question-sets", response_model=R, summary="题库套题列表（含题目）")
def list_question_sets(
    form_type: str = "",
    keyword: str = "",
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    q = db.query(QuestionSet)
    if form_type in _FORM_TYPES:
        q = q.filter(QuestionSet.form_type == form_type)
    if keyword:
        q = q.filter(QuestionSet.name.like(f"%{keyword}%"))
    rows = q.order_by(QuestionSet.created_at.desc()).all()
    return R.ok([_set_out(s, with_questions=True) for s in rows])


@router.post("/question-sets", response_model=R, summary="新建套题（含题目）")
def create_question_set(
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    _check_set_fields(payload)
    qs = QuestionSet(
        name=str(payload["name"]).strip()[:128],
        industry=str(payload.get("industry") or "").strip()[:64],
        position_type=str(payload.get("position_type") or "").strip()[:64],
        form_type=payload["form_type"],
        difficulty=payload["difficulty"],
        description=str(payload.get("description") or "").strip()[:512],
    )
    db.add(qs)
    db.flush()
    _apply_questions(db, qs, payload.get("questions") or [])
    db.commit()
    db.refresh(qs)
    return R.ok(_set_out(qs, with_questions=True))


@router.put("/question-sets/{set_id}", response_model=R, summary="编辑套题（整包替换题目）")
def update_question_set(
    set_id: int,
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    qs = db.get(QuestionSet, set_id)
    if not qs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "套题不存在")
    _check_set_fields(payload)
    qs.name = str(payload["name"]).strip()[:128]
    qs.industry = str(payload.get("industry") or "").strip()[:64]
    qs.position_type = str(payload.get("position_type") or "").strip()[:64]
    qs.form_type = payload["form_type"]
    qs.difficulty = payload["difficulty"]
    qs.description = str(payload.get("description") or "").strip()[:512]
    _apply_questions(db, qs, payload.get("questions") or [])
    db.commit()
    db.refresh(qs)
    return R.ok(_set_out(qs, with_questions=True))


@router.delete("/question-sets/{set_id}", response_model=R, summary="删除套题")
def delete_question_set(
    set_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    qs = db.get(QuestionSet, set_id)
    if not qs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "套题不存在")
    db.delete(qs)
    db.commit()
    return R.ok({"deleted": set_id})


@router.put("/questions/{question_id}", response_model=R, summary="编辑单题")
def update_question(
    question_id: int,
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "题目内容不能为空")
    q.content = content
    q.dimension = str(payload.get("dimension") or "").strip()[:32]
    q.category = str(payload.get("category") or "").strip()[:32]
    q.ref_answer = str(payload.get("ref_answer") or "").strip() or None
    q.time_limit_s = int(payload.get("time_limit_s") or q.time_limit_s or 120)
    db.commit()
    return R.ok(_q_out(q))


@router.delete("/questions/{question_id}", response_model=R, summary="删除单题")
def delete_question(
    question_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "题目不存在")
    qs = db.get(QuestionSet, q.set_id)
    db.delete(q)
    db.flush()
    if qs:
        remaining = sorted([x for x in qs.questions if x.id != question_id], key=lambda x: x.seq)
        for i, x in enumerate(remaining, start=1):
            x.seq = i
        qs.question_count = len(remaining)
        qs.est_minutes = round(sum(x.time_limit_s for x in remaining) / 60)
    db.commit()
    return R.ok({"deleted": question_id})


@router.post("/question-sets/from-resume", response_model=R, summary="从简历画像 AI 生成套题入库")
def create_set_from_resume(
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    resume_id = payload.get("resume_id")
    count = min(int(payload.get("count") or 5), 8)
    resume = db.get(Resume, resume_id) if resume_id else None
    if not resume:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "简历不存在")
    profile = resume.parsed_json or {}
    questions = generate_resume_questions(profile, count=count)
    candidate = str(profile.get("candidate_name") or "").strip()
    name = f"{candidate or resume.filename or '候选人'}-简历深挖"

    existing = db.query(QuestionSet).filter(QuestionSet.name == name).first()
    if existing:
        name = f"{name}-{existing.id + 1}"
    qs = QuestionSet(
        name=name[:128],
        industry="",
        position_type=str(profile.get("target_position") or "").strip()[:64],
        form_type="semi", difficulty="medium",
        description=f"基于简历画像（engine={profile.get('engine', 'rule')}）自动生成的个性化深挖题。",
    )
    db.add(qs)
    db.flush()
    items = [{
        "content": q["content"],
        "category": q.get("category") or "简历深挖",
        "dimension": q.get("dimension") or "专业深度",
        "time_limit_s": q.get("time_limit_s") or 180,
        "ref_answer": q.get("ref_answer") or "",
    } for q in questions]
    _apply_questions(db, qs, items)
    db.commit()
    db.refresh(qs)
    return R.ok(_set_out(qs, with_questions=True))


# ============ AI 面试提纲 ============
def _outline_out(o: InterviewOutline) -> dict:
    return {
        "id": o.id,
        "title": o.title,
        "position_id": o.position_id,
        "position_name": "",  # 由调用处填充
        "resume_id": o.resume_id,
        "jd_text": (o.jd_text or "")[:200],
        "outline": o.outline_json or {},
        "engine": o.engine,
        "created_at": o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "",
    }


@router.get("/outlines", response_model=R, summary="面试提纲列表")
def list_outlines(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    rows = db.query(InterviewOutline).order_by(
        InterviewOutline.created_at.desc()).limit(50).all()
    pos_map = {p.id: p.name for p in db.query(Position).all()}
    out = []
    for o in rows:
        item = _outline_out(o)
        item["position_name"] = pos_map.get(o.position_id, "")
        out.append(item)
    return R.ok(out)


@router.post("/outlines/generate", response_model=R, summary="按 JD+简历生成结构化面试提纲")
def generate_outline(
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    jd_text = str(payload.get("jd_text") or "").strip()
    position_id = payload.get("position_id")
    resume_id = payload.get("resume_id")

    profile = None
    if resume_id:
        resume = db.get(Resume, resume_id)
        if resume:
            profile = resume.parsed_json or {}
    if not jd_text and profile:
        jd_text = f"目标岗位：{profile.get('target_position') or '（未填写）'}"
    if not jd_text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请粘贴岗位 JD 或选择带画像的简历")

    result = generate_interview_outline(jd_text, profile)

    title = ""
    if position_id:
        pos = db.get(Position, position_id)
        if pos:
            title = pos.name
    if not title:
        title = str(profile.get("target_position") or "") if profile else ""
    title = (title or jd_text[:24]).strip()[:80]

    row = InterviewOutline(
        created_by=admin.id,
        position_id=position_id or None,
        resume_id=resume_id or None,
        title=title,
        jd_text=jd_text[:4000],
        outline_json=result,
        engine=result.get("engine", "rule"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    item = _outline_out(row)
    item["position_name"] = ""
    item["outline"] = result
    return R.ok(item)


# ============ 简历筛选 ============
def _screening_out(s: ResumeScreening, user_name: str = "") -> dict:
    return {
        "id": s.id,
        "resume_id": s.resume_id,
        "user_id": s.user_id,
        "user_name": user_name,
        "candidate_name": s.candidate_name,
        "target_position": s.target_position,
        "resume_summary": s.resume_summary,
        "score": float(s.score or 0),
        "verdict": s.verdict,
        "summary": s.summary,
        "concerns": s.concerns or [],
        "followups": s.followups or [],
        "engine": s.engine,
        "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "",
    }


@router.post("/screening/run", response_model=R, summary="按 JD 批量筛选简历（逐条降级不中断）")
def run_screening(
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    jd_text = str(payload.get("jd_text") or "").strip()
    position_id = payload.get("position_id")
    resume_ids = payload.get("resume_ids") or []
    if not jd_text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请粘贴岗位 JD")
    if not resume_ids or not isinstance(resume_ids, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请选择至少一份简历")
    resume_ids = [int(x) for x in resume_ids][:20]

    results = []
    for rid in resume_ids:
        resume = db.get(Resume, rid)
        if not resume:
            continue
        profile = resume.parsed_json or {}
        try:
            result = screen_resume(jd_text, resume.raw_text or "", profile)
        except Exception:
            result = screen_resume("", "", profile)
        # 覆盖 upsert：同简历删除旧筛选结果
        db.query(ResumeScreening).filter(
            ResumeScreening.resume_id == rid).delete()
        row = ResumeScreening(
            position_id=position_id or None,
            resume_id=rid,
            user_id=resume.user_id,
            candidate_name=str(profile.get("candidate_name") or "")[:64],
            target_position=str(profile.get("target_position") or "")[:128],
            resume_summary=str(profile.get("summary") or "")[:250],
            score=result["score"],
            verdict=result["verdict"],
            summary=result["summary"][:500],
            concerns=result.get("concerns") or [],
            followups=result.get("followups") or [],
            engine=result.get("engine", "rule"),
        )
        db.add(row)
        db.flush()
        results.append(row)
    db.commit()
    user_map = {u.id: u.nickname for u in db.query(User).all()}
    return R.ok({
        "count": len(results),
        "list": [_screening_out(r, user_map.get(r.user_id, "")) for r in results],
    })


@router.get("/screening", response_model=R, summary="筛选结果列表（按分数排序）")
def list_screening(
    position_id: int = 0,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    q = db.query(ResumeScreening)
    if position_id:
        q = q.filter(ResumeScreening.position_id == position_id)
    total = q.count()
    rows = q.order_by(ResumeScreening.score.desc()).offset(
        (page - 1) * page_size).limit(page_size).all()
    user_map = {u.id: u.nickname for u in db.query(User).all()}
    return R.ok({
        "list": [_screening_out(r, user_map.get(r.user_id, "")) for r in rows],
        "total": int(total), "page": page, "page_size": page_size,
    })


@router.delete("/screening/{screening_id}", response_model=R, summary="删除筛选结果")
def delete_screening(
    screening_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    row = db.get(ResumeScreening, screening_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "筛选结果不存在")
    db.delete(row)
    db.commit()
    return R.ok({"deleted": screening_id})


@router.post("/screening/compare", response_model=R, summary="终面候选人多维度对比（雷达图数据）")
def compare_candidates(
    payload: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    ids = [int(x) for x in (payload.get("screening_ids") or [])][:5]
    if len(ids) < 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请选择至少 2 名候选人进行对比")
    rows = db.query(ResumeScreening).filter(ResumeScreening.id.in_(ids)).all()
    if len(rows) < 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "有效候选人不足 2 名")
    rows.sort(key=lambda r: -float(r.score or 0))

    user_map = {u.id: u.nickname for u in db.query(User).all()}
    candidates = []
    for r in rows:
        uid = r.user_id
        # 面试 5 维均分（该用户全部 done 场次）
        dim_scores: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
        interview_avg = 0.0
        if uid:
            sq_ids = [
                sq.id for sq in db.query(SessionQuestion.id, InterviewSession.status)
                .join(InterviewSession, SessionQuestion.session_id == InterviewSession.id)
                .filter(InterviewSession.user_id == uid,
                        InterviewSession.status == "done").all()
            ]
            if sq_ids:
                for dim, score in db.query(SessionScore.dimension, SessionScore.score).filter(
                        SessionScore.session_question_id.in_(sq_ids)).all():
                    if dim in dim_scores:
                        dim_scores[dim].append(float(score))
        values = [round(float(r.score or 0), 1)]  # 筛选匹配
        valid_dims = [v for v in dim_scores.values() if v]
        interview_avg = round(sum(sum(v) / len(v) for v in valid_dims) / len(valid_dims), 1) \
            if valid_dims else 0.0
        for d in DIMENSIONS:
            vals = dim_scores[d]
            values.append(round(sum(vals) / len(vals), 1) if vals else 50.0)  # 无面试数据置 50

        # 语音表现（规则映射，避免串行 LLM 拖慢响应）
        voice = analyze_voice_profile(*_user_voice_inputs(db, uid), allow_llm=False) if uid \
            else {"voice_score": 50.0}
        voice_score = round(float(voice.get("voice_score") or 50.0), 1)

        candidates.append({
            "id": r.id,
            "name": r.candidate_name or user_map.get(uid, f"候选人{r.id}"),
            "user_id": uid,
            "screening_score": round(float(r.score or 0), 1),
            "interview_avg": interview_avg,
            "voice_score": voice_score,
            "verdict": r.verdict,
            "has_interview_data": bool(valid_dims),
            "values": values + [voice_score],
        })

    indicators = [{"name": "筛选匹配", "max": 100}] + \
        [{"name": d, "max": 100} for d in DIMENSIONS] + \
        [{"name": "语音表现", "max": 100}]
    return R.ok({"indicators": indicators, "candidates": candidates})


# ============ 语音多维度分析 ============
def _user_voice_inputs(db: Session, user_id: int | None) -> tuple[list[dict], list[str]]:
    """聚合某用户全部作答的语音指标与转写。返回 (metrics_list, transcripts)"""
    if not user_id:
        return [], []
    rows = (
        db.query(SessionMetrics, SessionQuestion)
        .join(SessionQuestion, SessionMetrics.session_question_id == SessionQuestion.id)
        .join(InterviewSession, SessionQuestion.session_id == InterviewSession.id)
        .filter(InterviewSession.user_id == user_id)
        .all()
    )
    metrics, transcripts = [], []
    for m, sq in rows:
        metrics.append({
            "wpm": m.wpm or 0,
            "pause_count": m.pause_count or 0,
            "pause_ms_total": m.pause_ms_total or 0,
            "filler_count": m.filler_count or 0,
            "interrupt_count": m.interrupt_count or 0,
            "duration_ms": sq.duration_ms or 0,
        })
        if sq.transcript:
            transcripts.append(sq.transcript[:800])
    return metrics, transcripts


@router.get("/voice-analysis", response_model=R, summary="候选人语音多维度分析（情绪/抗压/表达）")
def voice_analysis(
    user_id: int = Query(...),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    rows = (
        db.query(SessionMetrics, SessionQuestion, InterviewSession)
        .join(SessionQuestion, SessionMetrics.session_question_id == SessionQuestion.id)
        .join(InterviewSession, SessionQuestion.session_id == InterviewSession.id)
        .filter(InterviewSession.user_id == user_id)
        .order_by(InterviewSession.id, SessionQuestion.seq)
        .all()
    )
    labels = {"structured": "结构化", "group": "群面", "semi": "半结构化"}
    per_q = []
    metrics_list, transcripts = [], []
    for m, sq, sess in rows:
        metrics_list.append({
            "wpm": m.wpm or 0,
            "pause_count": m.pause_count or 0,
            "pause_ms_total": m.pause_ms_total or 0,
            "filler_count": m.filler_count or 0,
            "interrupt_count": m.interrupt_count or 0,
            "duration_ms": sq.duration_ms or 0,
        })
        if sq.transcript:
            transcripts.append(sq.transcript[:800])
        per_q.append({
            "session_id": sess.id,
            "form_label": labels.get(sess.form_type, sess.form_type),
            "seq": sq.seq,
            "question_content": (sq.content or "")[:60],
            "wpm": m.wpm or 0,
            "pause_count": m.pause_count or 0,
            "pause_ms_total": m.pause_ms_total or 0,
            "filler_count": m.filler_count or 0,
            "interrupt_count": m.interrupt_count or 0,
            "duration_ms": sq.duration_ms or 0,
            "transcript": (sq.transcript or "")[:200],
        })

    profile = analyze_voice_profile(metrics_list, transcripts, allow_llm=True)
    return R.ok({
        "user": {"id": user.id, "nickname": user.nickname},
        "question_count": len(per_q),
        "profile": profile,
        "questions": per_q,
    })


@router.get("/recruit/overview", response_model=R, summary="候选人（有语音/面试数据的用户）列表")
def candidate_users(
    keyword: str = "",
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """语音分析 Tab 的候选人下拉/列表数据：全部普通用户 + 作答统计"""
    q = db.query(User)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter((User.nickname.like(like)) | (User.phone.like(like)))
    users = q.order_by(User.created_at.desc()).limit(100).all()
    out = []
    for u in users:
        answered = db.query(func.count(SessionQuestion.id)).join(
            InterviewSession, SessionQuestion.session_id == InterviewSession.id
        ).filter(InterviewSession.user_id == u.id).scalar() or 0
        out.append({
            "id": u.id, "nickname": u.nickname, "phone": u.phone,
            "answered_count": int(answered),
        })
    return R.ok(out)
