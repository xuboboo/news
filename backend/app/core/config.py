"""全局配置：环境变量优先（.env），与 PRD 技术栈一一对应。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "华军软件园资讯雷达系统 API"
    version: str = "2.0.0"

    # —— 运行环境（production = 启动时执行安全自检，见 get_settings）——
    environment: str = "development"

    # —— 基础设施 ——
    database_url: str = "postgresql+psycopg://hjradar:hjradar@postgres:5432/hjradar"
    redis_url: str = "redis://redis:6379/0"

    # —— LLM 服务（OpenAI 兼容 / Anthropic 原生双协议，可选）——
    llm_base_url: str = ""      # 例：https://open.bigmodel.cn/api/paas/v4
    llm_api_key: str = ""
    llm_model: str = ""         # 例：glm-4-flash
    llm_protocol: str = "openai"  # openai=兼容协议；anthropic=原生 /v1/messages（DeepSeek /anthropic 等网关）

    # —— Embedding 服务（OpenAI 兼容协议，可选）——
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""

    # —— 向量 ——
    embed_dim: int = 256

    # —— 管理面板 ——
    admin_token: str = ""       # 运维接口应急后门（脚本/回归用）；正式访问走登录会话
    admin_no_auth: bool = False # 免鉴权开关（仅限内网/本机使用；公网部署必须为 False）

    # —— 登录与会话（除 bootstrap 外均可管理面板热调）——
    bootstrap_admin_email: str = ""      # 首启引导：user 表为空时自动创建的超管邮箱
    bootstrap_admin_password: str = ""   # 首启引导：初始密码（首登强制改密；建号后可从 .env 移除）
    session_ttl_hours: int = 72          # 会话绝对有效期（小时，OWASP 建议 4-8h 量级；调低不影响存量会话）
    session_idle_hours: int = 24         # 会话空闲超时（小时）：连续无请求超此值即失效；0 = 关闭
    login_max_attempts: int = 5          # 连续失败锁定阈值
    login_lock_minutes: int = 15         # 锁定时长（分钟）
    login_ip_throttle_max: int = 30      # 单 IP 登录尝试上限（窗口内，含失败；成功登录清零）
    login_ip_throttle_window_seconds: int = 300  # IP 限速窗口（秒）
    cookie_secure: bool = False          # 会话 Cookie Secure 标记；HTTPS 部署必须为 true
    audit_retention_days: int = 180      # 审计日志保留天数（beat 每周清理超期行）
    enable_docs: bool = True             # 是否公开 /docs /redoc /openapi.json（公网部署建议 false）

    # —— 日报 Webhook 推送（面板可热调；.env 仅作默认值，留空 = 该通道关闭） ——
    report_push_enabled: bool = False
    report_push_wecom_webhook_url: str = ""
    report_push_feishu_webhook_url: str = ""
    report_push_feishu_secret: str = ""  # 飞书机器人开启「签名校验」时必填，未开启留空

    # —— AI 成本治理（面板可热调） ——
    ai_daily_cost_budget: float = 0.0    # 单日（北京时间日界）AI 成本预算（元）；0 = 不告警
    ai_usage_retention_days: int = 365   # ai_usage_log 流水保留天数；0 = 永久保留

    # —— 安全 ——
    cors_origins: str = "http://localhost,http://127.0.0.1"  # 允许跨域的前端来源（逗号分隔）
    allow_private_ai_urls: bool = False  # 允许 LLM/Embedding 地址指向私网（仅内网部署）

    # —— Embedding ——
    embedding_strict: bool = True  # API 失败/维度不符时硬失败（false = 静默降级本地哈希，会污染聚类）

    # —— 热度算法：HotScore = Engagement × TimeDecay ——
    # λ=0.02（半衰期 ≈34.7h，24h 衰减 38%）：贴合科技新闻 24-48h 生命周期。
    # 旧值 0.0075 半衰期 92h，大事件霸榜首 3-4 天，榜单观感近乎冻结。
    decay_per_hour: float = 0.02
    comment_weight: int = 4
    like_weight: int = 2
    share_weight: int = 6

    # —— 事件聚合（PRD：相似度 > 0.85 + 标题关键词 + 实体 + 发布时间）——
    sim_threshold: float = 0.85
    keyword_jaccard_threshold: float = 0.2
    high_band_kw_floor: float = 0.1  # 高位带合并的最低关键词重合（跨来源佐证下限）
    cluster_window_hours: int = 48
    cluster_lookback_hours: int = 96

    # —— 爬虫 ——
    crawler_interval_minutes: int = 30
    crawl_limit_per_feed: int = 30

    # —— 互动数据轮询（crawler/stats.py；总开关与批次可在管理面板热调）——
    stats_poll_enabled: bool = True
    stats_poll_batch: int = 50
    # 放弃终态自愈：每健康轮次重排的「连败放弃」文章上限；0 = 关闭自愈。
    # 站点熔断/停机 ≥8h 会把黄金期文章打进放弃终态（6 连败），不重排则热度
    # 数据永久冻结在故障时刻
    stats_heal_batch: int = 10

    # —— 日报 ——
    report_issue_epoch: str = "2026-04-15"  # 第 1 期基准日

    # —— 读接口响应缓存（services/api_cache.py）——
    # 热榜/最新资讯的 Redis 缓存 TTL（秒），0 = 禁用（回滚开关）；
    # 与前端 60s 轮询对齐，DB 压力从每客户端降为每缓存键
    api_cache_ttl_seconds: int = 45


def _assert_production_safe(s: "Settings") -> None:
    """ENVIRONMENT=production 的启动 fail-fast 自检（2026-09-09 审计）：
    关键安全项必须显式收紧，禁止把开发默认值带进生产。任一不满足直接抛错拒绝启动。"""
    if s.environment.strip().lower() != "production":
        return
    problems: list[str] = []
    if not s.cookie_secure:
        problems.append("cookie_secure 必须为 true（HTTPS 部署的会话 Cookie Secure 标记）")
    if s.enable_docs:
        problems.append("enable_docs 必须为 false（不对外公开 /docs /redoc /openapi.json）")
    if s.admin_no_auth:
        problems.append("admin_no_auth 必须为 false（管理面板不得免鉴权暴露）")
    if ":hjradar@" in s.database_url:
        problems.append("database_url 仍在使用默认口令 hjradar，必须更换")
    # .env.example 的占位密钥原样带进生产 = 管理后门/引导密码公开于仓库可猜
    if s.admin_token == "please-change-me":
        problems.append("admin_token 仍是 .env.example 的占位值，必须更换")
    if s.bootstrap_admin_password == "please-change-me-too":
        problems.append("bootstrap_admin_password 仍是 .env.example 的占位值，必须更换")
    if problems:
        raise RuntimeError(
            "ENVIRONMENT=production 安全自检未通过（禁止弱默认值上线）：\n  - "
            + "\n  - ".join(problems)
        )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    _assert_production_safe(s)
    return s
