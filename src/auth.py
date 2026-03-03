"""
auth.py — User authentication & API key management for QgisRemoteMCP multi-user mode.

Provides:
  - User registration / login
  - API key generation & verification  (prefix: "qgis_")
  - JWT tokens (HS256, 24h)
  - JSON file persistence  (data/auth.json)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

try:
    import jwt as _jwt
    _HAS_JWT = True
except ImportError:
    _HAS_JWT = False

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 86400  # 24 h


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class User:
    id: str
    email: str
    password_hash: str   # "salt:sha256hex"
    api_key: str         # "qgis_<urlsafe 32>"
    created_at: float = field(default_factory=time.time)
    last_login: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(**d)


# ──────────────────────────────────────────────────────────────────────────────
# Manager
# ──────────────────────────────────────────────────────────────────────────────

class AuthManager:
    """
    In-memory auth store backed by a JSON file.
    Thread-safe for concurrent reads; writes use a simple file lock via rename.
    """

    def __init__(self, data_dir: str = "data"):
        self._data_dir = data_dir
        self._auth_file = os.path.join(data_dir, "auth.json")
        self.users: Dict[str, User] = {}      # user_id  → User
        self.api_keys: Dict[str, str] = {}    # api_key  → user_id
        self.emails: Dict[str, str] = {}      # email    → user_id

    async def initialize(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        self._load()

    # ── Registration / Login ──────────────────────────────────────────────────

    def register(self, email: str, password: str) -> dict:
        """
        Create a new user.
        Returns {"api_key": str, "token": str} or raises ValueError.
        """
        email = email.strip().lower()
        if not email or not password:
            raise ValueError("email and password are required")
        if email in self.emails:
            raise ValueError(f"email already registered: {email}")

        user_id = secrets.token_hex(16)
        api_key = f"qgis_{secrets.token_urlsafe(32)}"
        password_hash = self._hash_password(password)

        user = User(
            id=user_id,
            email=email,
            password_hash=password_hash,
            api_key=api_key,
        )
        self.users[user_id] = user
        self.api_keys[api_key] = user_id
        self.emails[email] = user_id
        self._save()

        return {"api_key": api_key, "token": self._create_token(user_id)}

    def login(self, email: str, password: str) -> dict:
        """
        Verify credentials.
        Returns {"api_key": str, "token": str} or raises ValueError.
        """
        email = email.strip().lower()
        user_id = self.emails.get(email)
        if not user_id:
            raise ValueError("Invalid email or password")

        user = self.users[user_id]
        if not self._verify_password(password, user.password_hash):
            raise ValueError("Invalid email or password")

        user.last_login = time.time()
        self._save()
        return {"api_key": user.api_key, "token": self._create_token(user_id)}

    # ── Verification ─────────────────────────────────────────────────────────

    def verify_api_key(self, api_key: str) -> Optional[User]:
        """Return User if key is valid, else None."""
        user_id = self.api_keys.get(api_key)
        return self.users.get(user_id) if user_id else None

    def verify_token(self, token: str) -> Optional[str]:
        """Return user_id if JWT is valid & not expired, else None."""
        if not _HAS_JWT:
            return None
        try:
            payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            return payload.get("sub")
        except Exception:
            return None

    # ── Key management ────────────────────────────────────────────────────────

    def regenerate_api_key(self, user_id: str) -> str:
        """Generate a new API key for a user, invalidate old one."""
        user = self.users.get(user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        # Invalidate old key
        self.api_keys.pop(user.api_key, None)

        # Generate new key
        new_key = f"qgis_{secrets.token_urlsafe(32)}"
        user.api_key = new_key
        self.api_keys[new_key] = user_id
        self._save()
        return new_key

    def get_user_info(self, user_id: str) -> Optional[dict]:
        """Return public user info (no password hash)."""
        user = self.users.get(user_id)
        if not user:
            return None
        return {
            "id": user.id,
            "email": user.email,
            "created_at": user.created_at,
            "last_login": user.last_login,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _create_token(self, user_id: str) -> str:
        if not _HAS_JWT:
            return ""
        payload = {
            "sub": user_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
        }
        return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    @staticmethod
    def _hash_password(password: str, salt: Optional[str] = None) -> str:
        if salt is None:
            salt = secrets.token_hex(16)
        h = hashlib.sha256((password + salt).encode()).hexdigest()
        return f"{salt}:{h}"

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        try:
            salt, expected_hash = stored.split(":", 1)
        except ValueError:
            return False
        actual = hashlib.sha256((password + salt).encode()).hexdigest()
        return hmac.compare_digest(actual, expected_hash)

    def _load(self) -> None:
        if not os.path.exists(self._auth_file):
            return
        try:
            with open(self._auth_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for user_dict in data.get("users", []):
                user = User.from_dict(user_dict)
                self.users[user.id] = user
                self.api_keys[user.api_key] = user.id
                self.emails[user.email] = user.id
        except Exception as e:
            print(f"[auth] Warning: could not load {self._auth_file}: {e}")

    def _save(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        tmp = self._auth_file + ".tmp"
        data = {"users": [u.to_dict() for u in self.users.values()]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._auth_file)
