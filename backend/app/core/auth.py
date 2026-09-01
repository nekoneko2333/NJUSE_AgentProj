from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, Response

from app.core.settings import Settings


COOKIE_NAME = "mosscode_session"


class LocalAuth:
    """面向本机单用户部署的轻量会话认证；凭据只来自环境变量。"""

    def __init__(self, settings: Settings) -> None:
        self.username = settings.app_username
        self.password = settings.app_password
        self.tokens: set[str] = set()

    def authenticate(self, username: str, password: str) -> str | None:
        valid_user = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        valid_password = hmac.compare_digest(password.encode("utf-8"), self.password.encode("utf-8"))
        if not (valid_user and valid_password):
            return None
        token = secrets.token_urlsafe(32)
        self.tokens.add(token)
        return token

    def user_for(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE_NAME, "")
        return self.username if token in self.tokens else None

    def require_user(self, request: Request) -> str:
        user = self.user_for(request)
        if user is None:
            raise HTTPException(401, detail="authentication_required")
        return user

    def set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=False, max_age=60 * 60 * 12)

    def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(COOKIE_NAME, "")
        self.tokens.discard(token)
        response.delete_cookie(COOKIE_NAME)
