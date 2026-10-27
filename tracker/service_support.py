"""Portable verified TLS and non-secret connection diagnostics."""
import os
from pathlib import Path


def configure_tls_trust():
    """Use Mozilla CA roots in headless environments, preserving custom roots."""
    existing = os.environ.get("SSL_CERT_FILE")
    if existing:
        if not Path(existing).is_file():
            raise ValueError("SSL_CERT_FILE points to a missing CA bundle")
        return existing
    import certifi
    path = certifi.where()
    os.environ["SSL_CERT_FILE"] = path
    return path


def service_failure(exc):
    """Return classifications, never SDK payloads or private connection URLs."""
    text = str(exc).lower()
    if "401003" in text or "token expired" in text:
        return {"code": "token_expired", "message": "长桥访问令牌已过期，请在官方账户更新后重新配置"}
    if "not configured" in text:
        return {"code": "not_configured", "message": "服务凭证尚未配置"}
    if "capability unavailable" in text:
        return {"code": "capability_unavailable", "message": "当前SDK缺少财经日历能力，请检查SDK版本"}
    if "401" in text or "unauthorized" in text or "invalid token" in text:
        return {"code": "authentication_failed", "message": "服务鉴权失败，请检查凭证"}
    if "certificate" in text or "keychain" in text or "platform certs" in text:
        return {"code": "tls_trust_failed", "message": "TLS证书链不可用，请检查CA配置"}
    if any(word in text for word in ("connect", "dns", "timeout", "proxy")):
        return {"code": "connection_failed", "message": "服务连接失败，请检查网络与代理"}
    return {"code": "service_error", "message": "服务请求失败，请查看本地诊断"}
