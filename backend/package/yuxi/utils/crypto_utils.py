"""IM 机器人凭证与 API Key 的对称加解密工具。

使用 Fernet 做可逆加密；密钥优先取 IM_CREDENTIAL_ENCRYPTION_KEY，
未配置时回退到 JWT_SECRET_KEY（复用其开发环境自动生成 / 生产必填的语义）。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    """构建基于配置密钥的 Fernet 实例。"""
    secret = os.environ.get("IM_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not secret:
        from yuxi.utils.auth_utils import _get_jwt_secret_key

        secret = _get_jwt_secret_key()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """加密明文，返回可存库的密文字符串。"""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """解密密文；密文损坏时由 Fernet 抛出 InvalidToken。"""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
