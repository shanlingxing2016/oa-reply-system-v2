from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from config import LOGIN_PASSWORD
import secrets
import time
import hashlib

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 简单 token 存储（个人用，内存即可）
active_tokens: dict[str, float] = {}

# 用 SHA-256 哈希密码，避免 passlib/bcrypt 兼容性问题
_password_hash = hashlib.sha256(LOGIN_PASSWORD.encode()).hexdigest()


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(req: LoginRequest):
    if hashlib.sha256(req.password.encode()).hexdigest() != _password_hash:
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_urlsafe(32)
    active_tokens[token] = time.time() + 86400 * 7  # 7天有效
    return {"token": token, "message": "登录成功"}


@router.post("/logout")
def logout():
    return {"message": "已退出"}


def verify_token(token: str) -> bool:
    """验证 token 是否有效"""
    if not token:
        return False
    expire = active_tokens.get(token)
    if not expire:
        return False
    if time.time() > expire:
        del active_tokens[token]
        return False
    return True


def require_auth(token: str = None) -> str:
    """依赖注入：从请求中提取并验证 token"""
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return token
