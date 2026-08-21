from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.anon import fnv1a_anon_token



class UserBase(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    role: str = Field(default="member", pattern=r"^(member|producer)$")
    avatar_color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    avatar_image: str | None = None



class UserCreate(UserBase):
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)


class UserRead(UserBase):
    id: int
    email: str | None = None
    email_verified: bool = False
    is_admin: bool = False
    admin_role: str = "none"
    feishu_contact: str | None = None
    suspended_at: datetime | None = None
    suspension_reason: str | None = None
    deleted_at: datetime | None = None
    created_at: datetime

    @computed_field
    @property
    def anon_token(self) -> str:
        """Stable anonymous-display token (``#<anon_token>``) for this user.

        Authoritative server-side value for the anonymized peer-review UI so
        clients do not need to re-implement the hash (see ``app.anon``).
        """
        return fnv1a_anon_token(self.id)

    model_config = ConfigDict(from_attributes=True)


class UserUpdateProfile(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    feishu_contact: str | None = Field(default=None, max_length=100)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=3)



class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


class DeleteAccountRequest(BaseModel):
    password: str



class LoginRequest(BaseModel):
    email: str
    password: str



class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class RegisterResponse(BaseModel):
    email: str
    message: str = "Verification email sent. Please check your inbox."
