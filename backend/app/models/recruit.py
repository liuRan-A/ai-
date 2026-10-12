"""招聘域：AI 面试提纲 / 简历筛选结果（管理后台专用）"""
from datetime import datetime
from sqlalchemy import BigInteger, String, Text, JSON, DECIMAL, ForeignKey
from sqlalchemy.dialects.mysql import TINYINT, DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
import sqlalchemy as sa


class InterviewOutline(Base):
    """AI 生成的结构化面试提纲：同一岗位所有候选人共用一套评估框架"""
    __tablename__ = "interview_outlines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    position_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("positions.id"), nullable=True)
    resume_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("resumes.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    jd_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outline_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {summary, dimensions:[{name,weight,questions:[...]}]}
    engine: Mapped[str] = mapped_column(String(16), nullable=False, default="rule")  # deepseek/rule
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), server_default=sa.func.now(3))


class ResumeScreening(Base):
    """简历筛选结果：按 JD 打分排序 + 疑点标注 + 预生成追问"""
    __tablename__ = "resume_screenings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("positions.id"), nullable=True)
    resume_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("resumes.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    # 简历快照（简历被删除后列表仍可展示）
    candidate_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_position: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    resume_summary: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    score: Mapped[float] = mapped_column(DECIMAL(5, 2), nullable=False, default=0)
    verdict: Mapped[str] = mapped_column(
        String(16), nullable=False, default="consider")  # recommend/consider/reject
    summary: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 疑点：[{"type":"gap|rank_mismatch|other","text":"...","followup":"..."}]
    concerns: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    followups: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 预生成追问问题列表
    engine: Mapped[str] = mapped_column(String(16), nullable=False, default="rule")
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=3), server_default=sa.func.now(3))
