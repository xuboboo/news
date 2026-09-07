"""全局配置：环境变量优先（.env），与 PRD 技术栈一一对应。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "华军资讯雷达 API"
    version: str = "2.0.0"

    # —— 基础设施 ——
    database_url: str = "postgresql+psycopg://hjradar:hjradar@postgres:5432/hjradar"
    redis_url: str = "redis://redis:6379/0"

    # —— LLM 服务（OpenAI 兼容协议，可选）——
    llm_base_url: str = ""      # 例：https://open.bigmodel.cn/api/paas/v4
    llm_api_key: str = ""
    llm_model: str = ""         # 例：glm-4-flash

    # —— Embedding 服务（OpenAI 兼容协议，可选）——
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""

    # —— 向量 ——
    embed_dim: int = 256

    # —— 管理面板 ——
    admin_token: str = ""       # 运维接口鉴权；为空则管理面板不可用
    admin_no_auth: bool = False # 免鉴权开关（仅限内网/本机使用；公网部署必须为 False）

    # —— 安全 ——
    cors_origins: str = "http://localhost,http://127.0.0.1"  # 允许跨域的前端来源（逗号分隔）
    allow_private_ai_urls: bool = False  # 允许 LLM/Embedding 地址指向私网（仅内网部署）

    # —— Embedding ——
    embedding_strict: bool = True  # API 失败/维度不符时硬失败（false = 静默降级本地哈希，会污染聚类）

    # —— 热度算法：HotScore = Engagement × TimeDecay ——
    decay_per_hour: float = 0.0075
    comment_weight: int = 4
    share_weight: int = 6

    # —— 事件聚合（PRD：相似度 > 0.85 + 标题关键词 + 实体 + 发布时间）——
    sim_threshold: float = 0.85
    keyword_jaccard_threshold: float = 0.2
    cluster_window_hours: int = 48
    cluster_lookback_hours: int = 96

    # —— 爬虫 ——
    crawler_interval_minutes: int = 30
    crawl_limit_per_feed: int = 30

    # —— 日报 ——
    report_issue_epoch: str = "2026-04-15"  # 第 1 期基准日


@lru_cache
def get_settings() -> Settings:
    return Settings()
