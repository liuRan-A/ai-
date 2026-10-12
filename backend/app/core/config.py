"""Mockwise AI 模拟面试助手 · 后端配置"""
import os
from typing import List, Optional
from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    # 应用
    APP_NAME: str = "Mockwise API"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # MySQL 数据库
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "root")
    DB_NAME: str = os.getenv("DB_NAME", "mockwise")

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    # JWT
    SECRET_KEY: str = "mockwise-dev-secret-change-in-prod"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8  # 8h

    # 科大讯飞 语音听写（流式 IAT）WebAPI
    IFLY_APP_ID: str = ""
    IFLY_API_KEY: str = ""
    IFLY_API_SECRET: str = ""

    # DeepSeek 大模型（评分 / 反馈 / 追问）。未配置时自动降级为规则评分，业务不中断
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT_S: int = 40
    # —— LLM 可靠性参数（全部可在 .env 覆盖）——
    LLM_RETRY_TIMES: int = 1          # 可重试错误（超时/断连/429）的最大重试次数（不含首次）
    LLM_RETRY_BACKOFF_S: float = 1.5  # 重试退避基数（秒），第 n 次重试等待 base * n
    LLM_RATE_LIMIT_RPM: int = 0       # 本地限流：每分钟最大调用数，0 = 不限
    MONITOR_RECENT_ERRORS: int = 50   # 监控接口保留的最近错误条数

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000"]

    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings()
