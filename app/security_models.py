"""Account, session, review and audit tables; additive to the original schema."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey('employees.id'), unique=True)
    department: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class LoginSession(Base):
    __tablename__ = 'login_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

class LoginAttempt(Base):
    __tablename__ = 'login_attempts'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), index=True)
    address: Mapped[str] = mapped_column(String(100), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(160))
    target: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default='')
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Evaluation(Base):
    __tablename__ = 'evaluation_reviews'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey('skills.id'))
    submitted_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    payload: Mapped[str] = mapped_column(Text)
    baseline: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (Index('uq_pending_evaluation', 'employee_id','skill_id',unique=True,sqlite_where=text("status = 'pending'")),)
