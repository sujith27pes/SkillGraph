"""Fail-closed route authorization and opaque, revocable cookie sessions."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from .db import get_db
from .models import Employee
from .security_models import AuditEvent, LoginAttempt, LoginSession, User

router = APIRouter(prefix='/auth', tags=['Accounts and access'])
COOKIE = 'skillgraph_session'
ROLES = {'admin','hr','manager','employee'}


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def verify_password(password, stored):
    try:
        algorithm, iterations, salt, expected = stored.split('$')
        if algorithm != 'pbkdf2_sha256': return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

_DUMMY = password_hash('unavailable-account-padding')

def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()


def audit(db, user, action, target, detail=''):
    db.add(AuditEvent(actor_id=user if isinstance(user,int) else user.id if user else None, action=action, target=str(target), detail=detail))


def require_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE, '')
    session = db.get(LoginSession, fingerprint(token)) if token else None
    user = db.get(User, session.user_id) if session and session.expires_at > datetime.utcnow() else None
    if user is None or not user.active:
        raise HTTPException(401, 'Sign in to continue')
    request.state.user = user
    request.state.actor_id = user.id
    request.state.session = session
    return user


def require_scope(db, user, eid):
    employee = db.get(Employee, eid)
    if employee is None:
        raise HTTPException(404, 'Employee not found')
    if user.role in ('admin','hr'):
        return employee
    if user.role == 'employee' and user.employee_id == eid:
        return employee
    if user.role == 'manager' and user.department and employee.department == user.department:
        return employee
    raise HTTPException(403, 'This employee is outside your access scope')


def employee_scope(user):
    if user.role == 'employee': return Employee.id == user.employee_id
    if user.role == 'manager': return Employee.department == user.department
    return Employee.id > 0


def check_origin(request):
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        if parsed.netloc != request.url.netloc or parsed.scheme != request.url.scheme:
            raise HTTPException(403, 'Cross-origin requests are not permitted')


async def authorize(request: Request, db: Session = Depends(get_db)):
    name = request.scope['route'].endpoint.__name__
    if name == 'auth_login':
        check_origin(request)
        return
    user = require_user(request, db)
    for key, value in request.path_params.items():
        if key in {'eid','role_id','skill_id','pid','rid','uid'}:
            try:
                parsed_id = int(value)
                if not 1 <= parsed_id <= 9223372036854775807: raise ValueError()
            except (ValueError,TypeError):
                raise HTTPException(422,'Invalid record identifier')
    if request.method not in ('GET','HEAD','OPTIONS') or (name == 'dev_plan' and request.query_params.get('ai','').lower() in ('true','1','yes','on')):
        check_origin(request)
        supplied = request.headers.get('x-csrf-token','')
        if not hmac.compare_digest(supplied, request.state.session.csrf):
            raise HTTPException(403, 'Session verification failed. Refresh and try again.')
    common = {'auth_me','auth_logout','auth_password','list_skills','list_roles','get_role','list_resources','ai_status','get_questions','generate_questions','health','list_employees','review_list'}
    if name in common: return
    if name in {'auth_users','auth_create_user','auth_update_user','auth_reset_password','api_schema'}:
        if user.role == 'admin': return
    elif name in {'get_employee','history','readiness_detail','top_roles','dev_plan','passport','take_assessment','set_avail','extract_skills'}:
        require_scope(db,user,int(request.path_params['eid']))
        if name == 'extract_skills' and user.role not in ('admin','hr') and (await request.json()).get('apply'):
            raise HTTPException(403,'Only HR can apply extracted ratings')
        return
    elif name == 'simulate':
        body = await request.json()
        try: eid = int(body['employee_id'])
        except (KeyError,ValueError,TypeError): raise HTTPException(422,'Select an employee')
        require_scope(db,user,eid)
        return
    elif name in {'team','list_projects','staffing','candidates','review_decide'}:
        if user.role in ('admin','hr','manager'): return
    elif name in {'add_skill','add_role','put_reqs','del_role','bulk_employees','put_ratings','bulk_ratings','set_demand','workforce_plan','investment','summary','set_critical','succession_overview','succession_detail','add_project','add_resource','continuity','rep_matrix','rep_role','rep_plan','rep_succ','audit_list','governance_summary'}:
        if user.role in ('admin','hr'): return
    raise HTTPException(403,'Your role cannot perform this action')


class Login(BaseModel):
    username: str = Field(min_length=1,max_length=100)
    password: str = Field(min_length=1,max_length=256)


def user_json(user):
    return {'id':user.id,'username':user.username,'role':user.role,'employee_id':user.employee_id,'department':user.department,'active':user.active}


@router.post('/login')
def auth_login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    username = body.username.strip().lower()
    account_key = fingerprint(username)
    address = request.client.host if request.client else 'unknown'
    since = datetime.utcnow()-timedelta(minutes=15)
    db.execute(delete(LoginAttempt).where(LoginAttempt.at < since))
    attempts = db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.at >= since,
        (LoginAttempt.account_key == account_key) | (LoginAttempt.address == address)))
    if attempts >= 20:
        raise HTTPException(429,'Too many sign-in attempts. Try again in 15 minutes.',headers={'Retry-After':'900'})
    db.add(LoginAttempt(account_key=account_key,address=address))
    db.commit()  # Persist attempt before expensive password verification.
    user = db.scalar(select(User).where(User.username == username))
    valid = verify_password(body.password,user.password_hash if user else _DUMMY)
    if not user or not user.active or not valid:
        audit(db,None,'login_failed',account_key)
        db.commit()
        raise HTTPException(401,'Invalid username or password')
    token,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    old = request.cookies.get(COOKIE)
    if old: db.execute(delete(LoginSession).where(LoginSession.token_hash==fingerprint(old)))
    db.execute(delete(LoginSession).where(LoginSession.expires_at < datetime.utcnow()))
    db.add(LoginSession(token_hash=fingerprint(token),user_id=user.id,csrf=csrf,expires_at=datetime.utcnow()+timedelta(hours=8)))
    audit(db,user,'login',user.id)
    db.commit()
    response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=os.getenv('APP_ENV')=='production',max_age=28800,path='/')
    return {'user':user_json(user),'csrf_token':csrf}


@router.get('/me')
def auth_me(request: Request, user: User = Depends(require_user)):
    return {'user':user_json(user),'csrf_token':request.state.session.csrf}


@router.post('/logout')
def auth_logout(request: Request,response: Response,db: Session=Depends(get_db),user: User=Depends(require_user)):
    db.execute(delete(LoginSession).where(LoginSession.token_hash==request.state.session.token_hash))
    audit(db,user,'logout',user.id);db.commit()
    response.delete_cookie(COOKIE,path='/')
    return {'ok':True}


class NewUser(BaseModel):
    username: str = Field(min_length=3,max_length=100,pattern=r'^[a-zA-Z0-9_.@-]+$')
    password: str = Field(min_length=15,max_length=256)
    role: str = Field(pattern=r'^(admin|hr|manager|employee)$')
    employee_id: int | None = None
    department: str | None = Field(None,max_length=120)


def validate_mapping(db,role,employee_id,department):
    if employee_id is not None and not db.get(Employee,employee_id):
        raise HTTPException(422,'Employee does not exist')
    if role == 'employee' and employee_id is None:
        raise HTTPException(422,'An employee account must be linked to an employee record')
    if role == 'manager' and (not department or not department.strip()):
        raise HTTPException(422,'A manager must have a department scope')
    if role == 'manager' and not db.scalar(select(Employee.id).where(Employee.department==department).limit(1)):
        raise HTTPException(422,'Choose an existing department')


@router.get('/users')
def auth_users(db: Session=Depends(get_db)):
    return [user_json(u) for u in db.scalars(select(User).order_by(User.id))]


@router.post('/users',status_code=201)
def auth_create_user(body: NewUser,db: Session=Depends(get_db),actor: User=Depends(require_user)):
    department=body.department.strip() if body.department else None
    validate_mapping(db,body.role,body.employee_id,department)
    user=User(username=body.username.lower(),password_hash=password_hash(body.password),role=body.role,employee_id=body.employee_id,department=department)
    db.add(user);db.flush();audit(db,actor,'user_created',user.id,body.role);db.commit()
    return user_json(user)


class UserUpdate(BaseModel):
    role: str = Field(pattern=r'^(admin|hr|manager|employee)$')
    active: bool
    employee_id: int | None = None
    department: str | None = Field(None,max_length=120)


@router.put('/users/{uid}')
def auth_update_user(uid:int,body:UserUpdate,db:Session=Depends(get_db),actor:User=Depends(require_user)):
    user=db.get(User,uid)
    if not user: raise HTTPException(404,'Account not found')
    if uid==actor.id: raise HTTPException(409,'You cannot change your own role or disable your own account')
    department=body.department.strip() if body.department else None
    validate_mapping(db,body.role,body.employee_id,department)
    for key,value in {**body.model_dump(),'department':department}.items(): setattr(user,key,value)
    db.execute(delete(LoginSession).where(LoginSession.user_id==uid))
    audit(db,actor,'user_access_changed',uid,body.role);db.commit()
    return user_json(user)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=15,max_length=256)


@router.post('/password')
def auth_password(body:PasswordChange,db:Session=Depends(get_db),user:User=Depends(require_user)):
    if not verify_password(body.current_password,user.password_hash): raise HTTPException(400,'Current password is incorrect')
    user.password_hash=password_hash(body.new_password)
    db.execute(delete(LoginSession).where(LoginSession.user_id==user.id))
    audit(db,user,'password_changed',user.id);db.commit()
    return {'ok':True,'detail':'Password changed. Sign in again on every device.'}


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=15,max_length=256)


@router.post('/users/{uid}/password')
def auth_reset_password(uid:int,body:PasswordReset,db:Session=Depends(get_db),actor:User=Depends(require_user)):
    user=db.get(User,uid)
    if not user: raise HTTPException(404,'Account not found')
    if uid==actor.id: raise HTTPException(409,'Use the change-password screen for your own account')
    user.password_hash=password_hash(body.new_password)
    db.execute(delete(LoginSession).where(LoginSession.user_id==uid))
    audit(db,actor,'password_reset',uid);db.commit()
    return {'ok':True}
