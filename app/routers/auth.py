from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.schemas import LoginRequest, UserRead

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserRead)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or user.password != payload.password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return user
