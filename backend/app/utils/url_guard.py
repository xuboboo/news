"""AI 服务地址校验：仅放行公网 http(s) 目标，防止管理面板/配置被用作内网 SSRF 跳板。

管理面板可运行时修改 llm_base_url / embedding_base_url，若不校验，攻击者
（尤其 ADMIN_NO_AUTH 模式下）可让后端向任意内网地址发 POST 并回读响应。
"""
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class UrlNotAllowed(ValueError):
    """AI 服务地址违反公网限制。"""


def assert_public_http_url(url: str) -> str:
    """校验并归一化 base_url（去尾部 /）。

    - scheme 必须是 http/https 且带主机名；
    - 主机名解析到私网/环回/链路本地/保留段时拒绝（ALLOW_PRIVATE_AI_URLS=true 豁免，
      供全内网部署使用）；
    - 域名解析失败不在此拦截，交由后续 HTTP 调用自然报错。
    """
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlNotAllowed(f"AI 服务地址必须是合法的 http(s) URL，收到：{url!r}")
    if get_settings().allow_private_ai_urls:
        logger.debug("allow_private_ai_urls=true，跳过私网校验：%s", url)
        return url.rstrip("/")

    hostname = parts.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".internal", ".local")):
        raise UrlNotAllowed(f"AI 服务地址不允许指向本机/内部主机名：{hostname}")

    try:
        addr_infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        return url.rstrip("/")

    for info in addr_infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UrlNotAllowed(
                f"AI 服务地址解析到非公网 IP（{ip}），已拦截；"
                "全内网部署可设 ALLOW_PRIVATE_AI_URLS=true 放行"
            )
    return url.rstrip("/")
