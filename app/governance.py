"""Human review queue. Approval is an atomic, auditable state transition."""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session
from .db import get_db
from .models import Employee, Rating, Skill, Assessment
from .security_models import AuditEvent, Evaluation, User
from .security import audit, employee_scope, require_scope, require_user

router=APIRouter(prefix='/governance',tags=['Human review and audit'])


def baseline(rating):
    return rating.updated_at.isoformat() if rating else None


@router.get('/reviews')
def review_list(db:Session=Depends(get_db),user:User=Depends(require_user),status:str=Query('pending',pattern='^(pending|approved|rejected|all)$'),limit:int=Query(100,ge=1,le=200)):
    query=select(Evaluation,Employee.name,Skill.name).join(Employee,Employee.id==Evaluation.employee_id).join(Skill,Skill.id==Evaluation.skill_id).where(employee_scope(user))
    if status!='all': query=query.where(Evaluation.status==status)
    rows=db.execute(query.order_by(Evaluation.id.desc()).limit(limit)).all()
    return [{'id':e.id,'employee_id':e.employee_id,'employee':name,'skill':skill,'status':e.status,
        'submitted_by':e.submitted_by,'created_at':e.created_at.isoformat(),'reviewed_by':e.reviewed_by,
        'reason':e.reason,'result':json.loads(e.payload),'can_review':e.status=='pending' and user.role!='employee' and e.submitted_by!=user.id and user.employee_id!=e.employee_id}
        for e,name,skill in rows]


class Decision(BaseModel):
    decision:str=Field(pattern='^(approve|reject)$')
    reason:str=Field(min_length=10,max_length=2000)
    level:int|None=Field(None,ge=0,le=5)


@router.post('/reviews/{rid}/decision')
def review_decide(rid:int,body:Decision,db:Session=Depends(get_db),user:User=Depends(require_user)):
    from .main import _save_ratings
    if len(body.reason.strip())<10: raise HTTPException(422,'Provide a meaningful review rationale')
    evaluation=db.get(Evaluation,rid)
    if not evaluation: raise HTTPException(404,'Evaluation not found')
    require_scope(db,user,evaluation.employee_id)
    if evaluation.submitted_by==user.id or user.employee_id==evaluation.employee_id:
        raise HTTPException(403,'A different reviewer must evaluate this submission')
    # Claim in a conditional write. SQLite serializes writers; exactly one reviewer can succeed.
    claimed=db.execute(update(Evaluation).where(Evaluation.id==rid,Evaluation.status=='pending').values(status='reviewing'))
    if claimed.rowcount!=1: raise HTTPException(409,'This evaluation has already been reviewed')
    data=json.loads(evaluation.payload)
    if body.decision=='approve':
        rating=db.get(Rating,(evaluation.employee_id,evaluation.skill_id))
        if baseline(rating)!=evaluation.baseline:
            db.rollback()
            raise HTTPException(409,'The skill rating changed since submission. Reject this stale evaluation and reassess.')
        level=body.level if body.level is not None else data['level']
        evaluation.status='approved'
        evaluation.reviewed_by=user.id;evaluation.reason=body.reason.strip();evaluation.reviewed_at=datetime.utcnow()
        audit(db,user,'evaluation_approved',rid,f'level={level}; {body.reason.strip()}')
        _save_ratings(db,evaluation.employee_id,[{'skill_id':evaluation.skill_id,'level':level,'confidence':data['confidence'],'source':'human_review'}],extra={'score':data['score'],'summary':f"{data['summary']} Reviewer rationale: {body.reason.strip()}"})
    else:
        evaluation.status='rejected'
        evaluation.reviewed_by=user.id;evaluation.reason=body.reason.strip();evaluation.reviewed_at=datetime.utcnow()
        audit(db,user,'evaluation_rejected',rid,body.reason.strip());db.commit()
    return {'id':rid,'status':evaluation.status}


@router.get('/audit')
def audit_list(db:Session=Depends(get_db),before:int|None=None,limit:int=Query(100,ge=1,le=200)):
    q=select(AuditEvent,User.username).outerjoin(User,User.id==AuditEvent.actor_id).order_by(AuditEvent.id.desc()).limit(limit)
    if before: q=q.where(AuditEvent.id<before)
    return [{'id':event.id,'actor':name or 'Unauthenticated','action':event.action,'target':event.target,'detail':event.detail,'at':event.at.isoformat()} for event,name in db.execute(q)]


@router.get('/summary')
def governance_summary(db:Session=Depends(get_db)):
    return {'pending_reviews':db.scalar(select(func.count()).select_from(Evaluation).where(Evaluation.status=='pending')),
        'reviewed_evaluations':db.scalar(select(func.count()).select_from(Evaluation).where(Evaluation.status.in_(['approved','rejected']))),
        'active_accounts':db.scalar(select(func.count()).select_from(User).where(User.active==True)),
        'manual_ratings':db.scalar(select(func.count()).select_from(Rating).where(Rating.source=='manual')),
        'reviewed_ratings':db.scalar(select(func.count()).select_from(Rating).where(Rating.source=='human_review'))}
