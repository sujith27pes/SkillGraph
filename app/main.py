import os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
import csv, io, json
from typing import Literal
from fastapi import Request
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .security import authorize, require_user, employee_scope, require_scope
from .security_models import Evaluation, User
from .governance import baseline
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, case, delete, func, or_, select
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from . import ai
from .db import Base, SessionLocal, engine, get_db, upsert
from .models import (Assessment, Employee, LearningResource, Project, ProjectSkill, Question, Rating, Readiness, Role, RoleSkillReq, Skill, SkillDemand)
from .readiness import recompute_employees, recompute_role, recompute_role_in_session

LEARN_HOURS_PER_LEVEL = 25


app = FastAPI(title="SkillGraph API", version="2.0", docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(authorize)])
if os.getenv('APP_ENV') == 'production' and not os.getenv('APP_ORIGIN','').startswith('https://'):
    raise RuntimeError('Set APP_ORIGIN to the public HTTPS origin in production')
from urllib.parse import urlsplit
allowed_hosts = ['127.0.0.1','localhost','testserver']
if os.getenv('APP_ORIGIN'):
    allowed_hosts.append(urlsplit(os.environ['APP_ORIGIN']).hostname)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store'
    scripts = "'self' 'unsafe-inline'" if request.url.path == '/admin.html' else "'self'"
    response.headers['Content-Security-Policy'] = f"default-src 'self'; script-src {scripts}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if os.getenv('APP_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    # No credentials or request bodies are logged. Workflow approvals have additional transactional audit events.
    if getattr(request.state,'actor_id',None) and (request.method not in ('GET','HEAD','OPTIONS') or request.url.path.startswith('/reports/')):
        from .security import audit
        with SessionLocal() as audit_db:
            audit(audit_db,request.state.actor_id,'request',request.url.path,f'{request.method} status={response.status_code}')
            audit_db.commit()
    return response


Base.metadata.create_all(engine)  # Existing tables are not migrated automatically.


@app.get('/api-schema')
def api_schema():
    return app.openapi()

@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(status_code=422, content={"detail": "Invalid reference or duplicate record. Check the selected skills, roles and people."})


# ---------- schemas ----------
class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r".*\S.*"); category: str | None = None; taxonomy_ref: str | None = None
class Req(BaseModel):
    skill_id: int; level: int = Field(ge=1, le=5); weight: float = Field(1.0, gt=0, allow_inf_nan=False)
class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160, pattern=r".*\S.*"); description: str | None = None; requirements: list[Req] = []; is_critical: bool = False
class EmpIn(BaseModel):
    external_id: str = Field(min_length=1,max_length=80,pattern=r".*\S.*"); name: str = Field(min_length=1,max_length=200,pattern=r".*\S.*"); department: str | None = None; title: str | None = None
    current_role_id: int | None = None; availability: int = Field(100, ge=0, le=100)
class RatingIn(BaseModel):
    skill_id: int; level: int = Field(ge=0, le=5); confidence: float = Field(0.5, ge=0, le=1, allow_inf_nan=False); source: Literal["manual", "import"] = "manual"
class BulkRating(RatingIn):
    employee_id: int
class Answer(BaseModel):
    question_id: int; answer: str = Field(max_length=4000)
class AssessIn(BaseModel):
    skill_id: int; answers: list[Answer] = Field(min_length=4, max_length=4)


def _set_reqs(db, role_id, reqs):
    db.execute(delete(RoleSkillReq).where(RoleSkillReq.role_id == role_id))
    db.add_all(RoleSkillReq(role_id=role_id, **r.model_dump()) for r in reqs)


# ---------- skills & roles (any number, created at runtime) ----------
@app.post("/skills", status_code=201)
def add_skill(b: SkillIn, db: Session = Depends(get_db)):
    s = Skill(**b.model_dump()); db.add(s)
    try: db.commit()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Skill exists")
    return {"id": s.id, **b.model_dump()}

@app.get("/skills")
def list_skills(db: Session = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "category": s.category} for s in db.scalars(select(Skill).order_by(Skill.id))]

@app.post("/roles", status_code=201)
def add_role(b: RoleIn, bg: BackgroundTasks, db: Session = Depends(get_db)):
    r = Role(name=b.name, description=b.description, is_critical=b.is_critical); db.add(r)
    try: db.flush()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Role exists")
    _set_reqs(db, r.id, b.requirements)
    recompute_role_in_session(db, r.id)
    db.commit()
    return {"id": r.id, "name": r.name, "version": r.version}

@app.get("/roles")
def list_roles(db: Session = Depends(get_db), after: int = 0, limit: int = Query(100, ge=1, le=500)):
    rows = db.scalars(select(Role).where(Role.id > after).order_by(Role.id).limit(limit))
    return [{"id": r.id, "name": r.name, "version": r.version, "is_critical": r.is_critical} for r in rows]

@app.get("/roles/{role_id}")
def get_role(role_id: int, db: Session = Depends(get_db)):
    r = db.get(Role, role_id) or _404()
    reqs = db.execute(select(RoleSkillReq.skill_id, RoleSkillReq.level, RoleSkillReq.weight).where(RoleSkillReq.role_id == role_id))
    return {"id": r.id, "name": r.name, "version": r.version, "is_critical": r.is_critical,
            "requirements": [{"skill_id": a, "level": b, "weight": c} for a, b, c in reqs]}

@app.put("/roles/{role_id}/requirements")
def put_reqs(role_id: int, reqs: list[Req], bg: BackgroundTasks, db: Session = Depends(get_db)):
    r = db.get(Role, role_id) or _404()
    _set_reqs(db, role_id, reqs); r.version += 1
    recompute_role_in_session(db, role_id)
    db.commit()
    return {"version": r.version, "recompute": "complete"}

@app.delete("/roles/{role_id}", status_code=204)
def del_role(role_id: int, db: Session = Depends(get_db)):
    if db.scalar(select(func.count()).select_from(Employee).where(Employee.current_role_id == role_id)):
        raise HTTPException(409, "Reassign employees before deleting their current role")
    db.execute(delete(Readiness).where(Readiness.role_id == role_id))
    db.execute(delete(RoleSkillReq).where(RoleSkillReq.role_id == role_id))
    db.execute(delete(Role).where(Role.id == role_id)); db.commit()


def _404(): raise HTTPException(404, "Not found")


# ---------- employees & ratings ----------
@app.post("/employees/bulk")
def bulk_employees(rows: list[EmpIn], db: Session = Depends(get_db)):
    if len(rows) > 10_000: raise HTTPException(413, "Max 10,000 per call")
    upsert(db, Employee, [r.model_dump() for r in rows], ["external_id"], ["name", "department", "title", "current_role_id", "availability"])
    ids = list(db.scalars(select(Employee.id).where(Employee.external_id.in_([r.external_id for r in rows]))))
    recompute_employees(db, ids, commit=False)
    db.commit(); return {"upserted": len(rows)}

@app.get("/employees")
def list_employees(db: Session = Depends(get_db), after: int = 0, limit: int = Query(50, ge=1, le=200), department: str | None = None, q: str = Query("", max_length=100), user: User = Depends(require_user)):
    statement = select(Employee).where(Employee.id > after, employee_scope(user)).order_by(Employee.id).limit(limit)
    if department: statement = statement.where(Employee.department == department)
    if q.strip(): statement = statement.where(or_(Employee.name.icontains(q.strip(), autoescape=True), Employee.external_id.icontains(q.strip(), autoescape=True)))
    items = [{"id": e.id, "external_id": e.external_id, "name": e.name, "department": e.department} for e in db.scalars(statement)]
    return {"items": items, "next_after": items[-1]["id"] if len(items) == limit else None}

@app.put("/employees/{eid}/ratings")
def put_ratings(eid: int, ratings: list[RatingIn], db: Session = Depends(get_db)):
    db.get(Employee, eid) or _404()
    _save_ratings(db, eid, [r.model_dump() for r in ratings]); return {"updated": len(ratings)}

def _save_ratings(db, eid, rows, extra=None, evidence=None):
    ts = datetime.utcnow()
    upsert(db, Rating, [{"employee_id": eid, "updated_at": ts, **r} for r in rows],
           ["employee_id", "skill_id"], ["level", "confidence", "source", "updated_at"])
    db.add_all(Assessment(employee_id=eid, skill_id=r["skill_id"], level=r["level"], confidence=r["confidence"],
                          source=r["source"], **({"summary": evidence[r["skill_id"]]} if evidence and r["skill_id"] in evidence else (extra or {}))) for r in rows)
    db.flush()
    recompute_employees(db, [eid], commit=False)
    db.commit()

@app.post("/ratings/bulk")
def bulk_ratings(rows: list[BulkRating], bg: BackgroundTasks, db: Session = Depends(get_db)):
    """HR imports: ratings, history and readiness commit atomically."""
    if len(rows) > 20_000: raise HTTPException(413, "Max 20,000 per call")
    requested_ids = {r.employee_id for r in rows}
    existing_ids = set(db.scalars(select(Employee.id).where(Employee.id.in_(requested_ids))))
    if requested_ids != existing_ids:
        raise HTTPException(422, "Every rating must reference an existing employee")
    ts = datetime.utcnow()
    upsert(db, Rating, [{**r.model_dump(), "updated_at": ts} for r in rows], ["employee_id", "skill_id"],
           ["level", "confidence", "source", "updated_at"])
    db.add_all(Assessment(employee_id=r.employee_id, skill_id=r.skill_id, level=r.level, confidence=r.confidence, source=r.source) for r in rows)
    db.flush()
    ids = sorted({r.employee_id for r in rows})
    recompute_employees(db, ids, commit=False)
    db.commit()
    return {"upserted": len(rows), "employees": len(ids), "recompute": "complete"}



# ---------- readiness & planning ----------
@app.get("/employees/{eid}/readiness/{role_id}")
def readiness_detail(eid: int, role_id: int, db: Session = Depends(get_db)):
    """Per-skill gap analysis for one person (small live query)."""
    db.get(Employee, eid) or _404()
    db.get(Role, role_id) or _404()
    rows = db.execute(select(Skill.id, Skill.name, RoleSkillReq.level, func.coalesce(Rating.level, 0))
        .select_from(RoleSkillReq).join(Skill, Skill.id == RoleSkillReq.skill_id)
        .outerjoin(Rating, (Rating.skill_id == Skill.id) & (Rating.employee_id == eid))
        .where(RoleSkillReq.role_id == role_id)).all()
    if not rows: _404()
    gaps = sorted(({"skill_id": i, "skill": n, "have": h, "need": r, "gap": r - h, "est_hours": (r - h) * LEARN_HOURS_PER_LEVEL}
                   for i, n, r, h in rows if h < r), key=lambda g: -g["gap"])
    stored = db.get(Readiness, (eid, role_id))
    return {"score": round(stored.score, 1) if stored else None, "gaps": gaps,
            "total_est_hours": sum(g["est_hours"] for g in gaps)}

@app.get("/roles/{role_id}/candidates")
def candidates(role_id: int, db: Session = Depends(get_db), min_score: float = Query(0, ge=0, le=100, allow_inf_nan=False), limit: int = Query(50, ge=1, le=200),
               cursor: str | None = None, department: str | None = None, user: User = Depends(require_user)):
    """Top-K by readiness via index (role_id, score desc). Keyset cursor = 'score:employee_id'."""
    q = (select(Readiness.employee_id, Employee.name, Employee.department, Readiness.score, Readiness.gap_count)
         .join(Employee, Employee.id == Readiness.employee_id)
         .where(Readiness.role_id == role_id, Readiness.score >= min_score, employee_scope(user)))
    if department: q = q.where(Employee.department == department)
    if cursor:
        try:
            s, i = cursor.split(":"); s, i = float(s), int(i)
            import math
            if not math.isfinite(s) or i < 0: raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid pagination cursor")
        q = q.where((Readiness.score < s) | ((Readiness.score == s) & (Readiness.employee_id > i)))
    rows = db.execute(q.order_by(Readiness.score.desc(), Readiness.employee_id).limit(limit)).all()
    items = [{"employee_id": a, "name": b, "department": c, "score": round(d, 1), "gap_count": g} for a, b, c, d, g in rows]
    nxt = f"{rows[-1][3]}:{rows[-1][0]}" if len(rows) == limit else None
    return {"items": items, "next_cursor": nxt}

@app.put("/demand/{skill_id}")
def set_demand(skill_id: int, target_headcount: int = Query(ge=0), db: Session = Depends(get_db)):
    upsert(db, SkillDemand, [{"skill_id": skill_id, "target_headcount": target_headcount}], ["skill_id"], ["target_headcount"])
    db.commit(); return {"ok": True}

@app.get("/analytics/workforce-plan")
def workforce_plan(db: Session = Depends(get_db), min_level: int = Query(3, ge=1, le=5)):
    """Supply (level>=min), upskill pool (level == min-1) and demand per skill. Uses index (skill_id, level)."""
    supply = dict(db.execute(select(Rating.skill_id, func.count()).where(Rating.level >= min_level).group_by(Rating.skill_id)).all())
    pool = dict(db.execute(select(Rating.skill_id, func.count()).where(Rating.level == min_level - 1).group_by(Rating.skill_id)).all())
    dem = dict(db.execute(select(SkillDemand.skill_id, SkillDemand.target_headcount)).all())
    out = []
    for sid, name in db.execute(select(Skill.id, Skill.name).order_by(Skill.id)):
        s, p, d = supply.get(sid, 0), pool.get(sid, 0), dem.get(sid, 0)
        short = max(0, d - s)
        out.append({"skill_id": sid, "skill": name, "supply": s, "demand": d, "shortfall": short,
                    "upskill_pool": p, "action": "sufficient" if not short else
                    (f"upskill {short} from pool" if p >= short else f"upskill {p}, hire {short - p}")})
    return out


# ---------- AI assessment ----------
@app.get("/skills/{skill_id}/questions")
def get_questions(skill_id: int, db: Session = Depends(get_db)):
    db.get(Skill, skill_id) or _404()
    questions = db.scalars(select(Question).where(Question.skill_id == skill_id).order_by(Question.level))
    return [{"id": q.id, "level": q.level, "question": q.text} for q in questions]

@app.post("/skills/{skill_id}/questions")
def generate_questions(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id) or _404()
    return [{"id": q.id, "level": q.level, "question": q.text} for q in ai.questions_for(db, skill)]

@app.post("/employees/{eid}/assessments")
def take_assessment(eid: int, b: AssessIn, db: Session = Depends(get_db), user: User = Depends(require_user)):
    db.get(Employee, eid) or _404(); skill = db.get(Skill, b.skill_id) or _404()
    qs = {q.id: q for q in db.scalars(select(Question).where(Question.skill_id == skill.id))}
    answer_ids = [a.question_id for a in b.answers]
    if len(set(answer_ids)) != len(answer_ids) or any(i not in qs for i in answer_ids):
        raise HTTPException(422, "Submit unique questions belonging to this skill")
    if {qs[i].level for i in answer_ids} != {2, 3, 4, 5}:
        raise HTTPException(422, "Submit one question at each difficulty level 2 through 5")
    qa = [{"question": qs[a.question_id].text, "level": qs[a.question_id].level, "answer": a.answer}
          for a in b.answers if a.question_id in qs]
    if not qa: raise HTTPException(422, "No valid answers")
    if db.scalar(select(Evaluation.id).where(Evaluation.employee_id == eid, Evaluation.skill_id == skill.id, Evaluation.status == "pending")):
        raise HTTPException(409, "This skill already has an evaluation awaiting review")
    prior = baseline(db.get(Rating, (eid, skill.id)))
    summary, per = ai.grade(skill, qa)
    evaluation = Evaluation(employee_id=eid, skill_id=skill.id, submitted_by=user.id, baseline=prior,
        payload=json.dumps({**summary, 'per_question': per, 'questions': qa}))
    db.add(evaluation); db.commit()
    return {**summary, 'per_question': per, 'review_id': evaluation.id, 'status': 'pending',
            'detail': 'Submitted for independent human review. Official proficiency has not changed.'}


@app.get("/employees/{eid}/assessments")
def history(eid: int, db: Session = Depends(get_db), limit: int = Query(20, ge=1, le=100)):
    rows = db.scalars(select(Assessment).where(Assessment.employee_id == eid).order_by(Assessment.id.desc()).limit(limit))
    return [{"skill_id": a.skill_id, "level": a.level, "score": a.score, "source": a.source, "summary": a.summary,
             "at": a.created_at.isoformat()} for a in rows]

@app.get("/employees/{eid}")
def get_employee(eid: int, db: Session = Depends(get_db)):
    e = db.get(Employee, eid) or _404()
    rows = db.execute(select(Rating.skill_id, Rating.level, Rating.confidence, Rating.source).where(Rating.employee_id == eid)).all()
    return {"id": e.id, "external_id": e.external_id, "name": e.name, "department": e.department, "title": e.title, "availability": e.availability, "current_role_id": e.current_role_id,
            "ratings": [{"skill_id": a, "level": b, "confidence": c, "source": d} for a, b, c, d in rows]}

@app.get("/employees/{eid}/top-roles")
def top_roles(eid: int, db: Session = Depends(get_db), limit: int = Query(5, ge=1, le=50)):
    rows = db.execute(select(Readiness.role_id, Role.name, Readiness.score, Readiness.gap_count)
                      .join(Role, Role.id == Readiness.role_id).where(Readiness.employee_id == eid)
                      .order_by(Readiness.score.desc()).limit(limit)).all()
    return [{"role_id": a, "name": b, "score": round(c, 1), "gap_count": d} for a, b, c, d in rows]

@app.get("/analytics/summary")
def summary(db: Session = Depends(get_db)):
    n = lambda m: db.scalar(select(func.count()).select_from(m))
    return {"employees": n(Employee), "skills": n(Skill), "roles": n(Role), "ratings": n(Rating)}  # use cached counts at 100M+ rows

@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1)); return {"ok": True}


# ================= succession planning =================
BANDS = {"ready_now": (85, 101), "ready_soon": (65, 85), "developing": (40, 65)}

@app.put("/roles/{role_id}/critical")
def set_critical(role_id: int, value: bool, db: Session = Depends(get_db)):
    r = db.get(Role, role_id) or _404(); r.is_critical = value; db.commit(); return {"is_critical": value}

def _bench(db, role_id, limit):
    notinc = or_(Employee.current_role_id != role_id, Employee.current_role_id.is_(None))
    out = {}
    for k, (lo, hi) in BANDS.items():
        w = (Readiness.role_id == role_id, Readiness.score >= lo, Readiness.score < hi, notinc)
        n = db.scalar(select(func.count()).select_from(Readiness).join(Employee, Employee.id == Readiness.employee_id).where(*w))
        top = []
        if limit:
            top = [{"employee_id": a, "name": b, "department": c, "score": round(d, 1)} for a, b, c, d in db.execute(
                select(Employee.id, Employee.name, Employee.department, Readiness.score).select_from(Readiness)
                .join(Employee, Employee.id == Readiness.employee_id).where(*w).order_by(Readiness.score.desc()).limit(limit))]
        out[k] = {"count": n, "top": top}
    return out

def _risk(incumbents, ready_now):
    return "high" if ready_now == 0 else "medium" if ready_now < max(incumbents, 1) else "low"

@app.get("/succession")
def succession_overview(db: Session = Depends(get_db)):
    out = []
    for r in db.scalars(select(Role).where(Role.is_critical == True).order_by(Role.id)):  # noqa: E712
        inc = db.scalar(select(func.count()).select_from(Employee).where(Employee.current_role_id == r.id))
        b = _bench(db, r.id, 0)
        out.append({"role_id": r.id, "role": r.name, "incumbents": inc, "ready_now": b["ready_now"]["count"],
                    "ready_soon": b["ready_soon"]["count"], "developing": b["developing"]["count"],
                    "risk": _risk(inc, b["ready_now"]["count"])})
    return out

@app.get("/succession/{role_id}")
def succession_detail(role_id: int, db: Session = Depends(get_db), limit: int = Query(8, ge=1, le=50)):
    r = db.get(Role, role_id) or _404()
    inc = db.execute(select(Employee.id, Employee.name, Readiness.score).select_from(Employee)
        .outerjoin(Readiness, and_(Readiness.employee_id == Employee.id, Readiness.role_id == role_id))
        .where(Employee.current_role_id == role_id).limit(50)).all()
    b = _bench(db, role_id, limit)
    return {"role": r.name, "is_critical": r.is_critical, "incumbents": len(inc),
            "incumbent_list": [{"employee_id": a, "name": n, "score": round(s, 1) if s is not None else None} for a, n, s in inc],
            "bands": b, "risk": _risk(len(inc), b["ready_now"]["count"])}

# ================= resource optimisation (projects) =================
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=160, pattern=r".*\S.*"); description: str | None = None; skills: list[Req] = []

@app.post("/projects", status_code=201)
def add_project(b: ProjectIn, db: Session = Depends(get_db)):
    p = Project(name=b.name, description=b.description); db.add(p)
    try: db.flush()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Project exists")
    db.add_all(ProjectSkill(project_id=p.id, **s.model_dump()) for s in b.skills); db.commit()
    return {"id": p.id, "name": p.name}

@app.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    return [{"id": p.id, "name": p.name, "description": p.description} for p in db.scalars(select(Project).order_by(Project.id))]

@app.get("/projects/{pid}/staffing")
def staffing(pid: int, db: Session = Depends(get_db), limit: int = Query(25, ge=1, le=200), min_available: int = Query(0, ge=0, le=100), user: User = Depends(require_user)):
    """Best-fit people for a project: skill fit x availability. Sparse inner join keeps it to people holding relevant skills."""
    reqs = db.execute(select(ProjectSkill.level, ProjectSkill.weight).where(ProjectSkill.project_id == pid)).all()
    if not reqs: _404()
    total = sum(l * w for l, w in reqs) or 1
    got = case((Rating.level < ProjectSkill.level, Rating.level), else_=ProjectSkill.level)
    score = (func.sum(got * ProjectSkill.weight) * 100.0 / total).label("score")
    rows = db.execute(select(Employee.id, Employee.name, Employee.department, Employee.availability, score, func.count())
        .select_from(Rating).join(ProjectSkill, and_(ProjectSkill.skill_id == Rating.skill_id, ProjectSkill.project_id == pid))
        .join(Employee, Employee.id == Rating.employee_id).where(Employee.availability >= min_available, employee_scope(user))
        .group_by(Employee.id).order_by(score.desc()).limit(limit)).all()
    return {"skills_required": len(reqs), "items": [{"employee_id": a, "name": n, "department": d, "availability": av,
            "score": round(s, 1), "skills_covered": c} for a, n, d, av, s, c in rows]}

@app.put("/employees/{eid}/availability")
def set_avail(eid: int, value: int = Query(ge=0, le=100), db: Session = Depends(get_db)):
    e = db.get(Employee, eid) or _404(); e.availability = value; db.commit(); return {"ok": True}

# ================= learning & development =================
class ResIn(BaseModel):
    skill_id: int; title: str; provider: str | None = None; level: int = Field(ge=1, le=5); hours: int = Field(ge=1)
    url: str | None = Field(None, pattern=r"^https?://")

@app.post("/learning", status_code=201)
def add_resource(b: ResIn, db: Session = Depends(get_db)):
    r = LearningResource(**b.model_dump()); db.add(r); db.commit(); return {"id": r.id}

@app.get("/learning")
def list_resources(db: Session = Depends(get_db), skill_id: int | None = None, limit: int = Query(100, ge=1, le=500)):
    q = select(LearningResource, Skill.name).join(Skill, Skill.id == LearningResource.skill_id).order_by(LearningResource.skill_id, LearningResource.level).limit(limit)
    if skill_id: q = q.where(LearningResource.skill_id == skill_id)
    return [{"id": r.id, "skill_id": r.skill_id, "skill": n, "title": r.title, "provider": r.provider, "level": r.level,
             "hours": r.hours, "url": r.url} for r, n in db.execute(q)]

@app.get("/employees/{eid}/development-plan/{role_id}")
def dev_plan(eid: int, role_id: int, ai_coach: bool = Query(False, alias="ai"), db: Session = Depends(get_db)):
    """Personalised plan: gaps (largest first) -> catalog courses that bridge have+1..need; optional AI coaching narrative."""
    d = readiness_detail(eid, role_id, db)
    steps, hours = [], 0
    for g in d["gaps"]:
        res = db.scalars(select(LearningResource).where(LearningResource.skill_id == g["skill_id"],
              LearningResource.level > g["have"], LearningResource.level <= g["need"]).order_by(LearningResource.level)).all()
        items = [{"title": r.title, "provider": r.provider, "level": r.level, "hours": r.hours, "url": r.url} for r in res]
        hours += sum(i["hours"] for i in items) if items else g["est_hours"]
        steps.append({"skill": g["skill"], "have": g["have"], "need": g["need"], "est_hours": g["est_hours"], "resources": items})
    out = {"score": d["score"], "steps": steps, "total_hours": hours}
    if ai_coach:
        out["coaching"] = ai.coaching(db.get(Employee, eid).name, db.get(Role, role_id).name, steps)
    return out

# ================= automatic skill extraction =================
class ExtractIn(BaseModel):
    text: str = Field(max_length=20000); apply: bool = False

@app.post("/employees/{eid}/extract-skills")
def extract_skills(eid: int, b: ExtractIn, db: Session = Depends(get_db)):
    """Infer skills from a resume/profile/project notes. Preview by default; apply=true saves (never overwrites AI-assessed ratings)."""
    db.get(Employee, eid) or _404()
    catalog = [(i, n) for i, n in db.execute(select(Skill.id, Skill.name).order_by(Skill.id))]
    valid = {i for i, _ in catalog}
    found = [{"skill_id": int(f["skill_id"]), "level": max(0, min(5, int(f["level"]))),
              "confidence": max(0.0, min(1.0, float(f.get("confidence", 0.5)))), "evidence": str(f.get("evidence", ""))[:160]}
             for f in ai.extract(b.text, catalog) if int(f.get("skill_id", -1)) in valid]
    applied = 0
    if b.apply and found:
        verified = {s for (s,) in db.execute(select(Rating.skill_id).where(Rating.employee_id == eid, Rating.source.in_(["ai_assessment", "human_review"])))}
        rows = [{"skill_id": f["skill_id"], "level": f["level"], "confidence": f["confidence"], "source": "ai_extraction"}
                for f in found if f["skill_id"] not in verified]
        if rows: _save_ratings(db, eid, rows, evidence={f["skill_id"]: f["evidence"] for f in found})
        applied = len(rows)
    return {"found": found, "applied": applied}

# ================= business continuity =================
@app.get("/analytics/continuity-risks")
def continuity(db: Session = Depends(get_db), threshold: int = Query(2, ge=0)):
    """Skills that matter (demanded, or required by a critical role) with few proficient holders, plus critical roles without a ready successor."""
    needed = select(SkillDemand.skill_id).where(SkillDemand.target_headcount > 0).union(
        select(RoleSkillReq.skill_id).join(Role, Role.id == RoleSkillReq.role_id).where(Role.is_critical == True))  # noqa: E712
    ids = {i for (i,) in db.execute(needed)}
    holders = dict(db.execute(select(Rating.skill_id, func.count()).where(Rating.skill_id.in_(ids), Rating.level >= 3).group_by(Rating.skill_id)).all()) if ids else {}
    dem = dict(db.execute(select(SkillDemand.skill_id, SkillDemand.target_headcount)).all())
    names = dict(db.execute(select(Skill.id, Skill.name)).all())
    skills = sorted(({"skill": names[i], "holders": holders.get(i, 0), "demand": dem.get(i, 0)} for i in ids if holders.get(i, 0) <= threshold),
                    key=lambda x: x["holders"])
    return {"skills_at_risk": skills, "roles_at_risk": [r for r in succession_overview(db) if r["risk"] != "low"]}

# ================= reports (streamed CSV) =================
def _s(v):  # neutralise spreadsheet formula injection
    return "'" + v if isinstance(v, str) and v.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) else v

def _stream(name, header, rows_fn):
    def gen():
        buf = io.StringIO(); w = csv.writer(buf)
        def flush():
            d = buf.getvalue(); buf.seek(0); buf.truncate(); return d
        w.writerow(header); yield flush()
        with SessionLocal() as s:  # own session: dependency sessions may close before streaming ends
            for row in rows_fn(s):
                w.writerow([_s(v) for v in row]); yield flush()
    return StreamingResponse(gen(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

@app.get("/reports/skill-matrix.csv")
def rep_matrix():
    return _stream("skill-matrix.csv", ["employee_id", "name", "department", "skill", "level", "confidence", "source"],
        lambda s: s.execute(select(Employee.external_id, Employee.name, Employee.department, Skill.name, Rating.level, Rating.confidence, Rating.source)
            .select_from(Rating).join(Employee, Employee.id == Rating.employee_id).join(Skill, Skill.id == Rating.skill_id)
            .order_by(Rating.employee_id).execution_options(yield_per=5000)))

@app.get("/reports/role-readiness.csv")
def rep_role(role_id: int):
    return _stream("role-readiness.csv", ["employee_id", "name", "department", "readiness_pct", "skill_gaps"],
        lambda s: s.execute(select(Employee.external_id, Employee.name, Employee.department, Readiness.score, Readiness.gap_count)
            .select_from(Readiness).join(Employee, Employee.id == Readiness.employee_id).where(Readiness.role_id == role_id)
            .order_by(Readiness.score.desc()).execution_options(yield_per=5000)))

@app.get("/reports/workforce-plan.csv")
def rep_plan():
    return _stream("workforce-plan.csv", ["skill", "supply", "demand", "shortfall", "upskill_pool", "action"],
        lambda s: ([p["skill"], p["supply"], p["demand"], p["shortfall"], p["upskill_pool"], p["action"]] for p in workforce_plan(s, 3)))

@app.get("/reports/succession.csv")
def rep_succ():
    return _stream("succession.csv", ["critical_role", "incumbents", "ready_now", "ready_soon", "developing", "risk"],
        lambda s: ([r["role"], r["incumbents"], r["ready_now"], r["ready_soon"], r["developing"], r["risk"]] for r in succession_overview(s)))


# Web UI served from the same origin (no CORS needed). Must stay last so API routes match first.
from .intelligence import router as intelligence_router
app.include_router(intelligence_router)
from .security import router as security_router
from .governance import router as governance_router
app.include_router(security_router)
app.include_router(governance_router)

app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="ui")
