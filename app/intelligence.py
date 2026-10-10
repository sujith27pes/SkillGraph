"""Explainable decision support. Simulations never mutate employee evidence."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import Assessment, Employee, Rating, Skill, Role, RoleSkillReq, SkillDemand, Project, ProjectSkill
from .llm import status
from .security import require_user, employee_scope
from .security_models import User

router = APIRouter(prefix="/intelligence", tags=["Decision intelligence"])


def require(db, model, key):
    item = db.get(model, key)
    if item is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return item


def evidence_factor(rating):
    if rating is None:
        return 0
    age = max(0, (datetime.utcnow() - rating.updated_at).days)
    return rating.confidence * max(.25, 1 - age / 730)


@router.get("/status")
def ai_status():
    return status()


@router.get("/passport/{eid}")
def passport(eid: int, db: Session = Depends(get_db)):
    employee = require(db, Employee, eid)
    rows = db.execute(select(Rating, Skill.name, Skill.category).join(Skill, Skill.id == Rating.skill_id)
                      .where(Rating.employee_id == eid).order_by(Skill.name)).all()
    evidence = {}
    for assessment in db.scalars(select(Assessment).where(Assessment.employee_id == eid).order_by(Assessment.id.desc())):
        evidence.setdefault(assessment.skill_id, assessment.summary)
    return {"employee_id": eid, "name": employee.name, "department": employee.department,
            "skills": [{"skill_id": r.skill_id, "skill": name, "category": category, "level": r.level,
                "confidence": r.confidence, "source": r.source, "evidence": evidence.get(r.skill_id), "updated_at": r.updated_at.isoformat(),
                "age_days": max(0, (datetime.utcnow() - r.updated_at).days),
                "evidence_strength": round(evidence_factor(r) * 100),
                "needs_review": evidence_factor(r) < .6} for r, name, category in rows],
            "note": "Evidence strength = recorded confidence × freshness (linear decay over 730 days; minimum freshness 25%). It is a heuristic, not a validated probability."}


class Change(BaseModel):
    skill_id: int
    level: int = Field(ge=0, le=5)

class Scenario(BaseModel):
    employee_id: int
    role_id: int
    changes: list[Change] = Field(default_factory=list, max_length=100)
    weekly_hours: int = Field(5, ge=1, le=40)


@router.post("/simulate")
def simulate(body: Scenario, db: Session = Depends(get_db), user: User = Depends(require_user)):
    employee = require(db, Employee, body.employee_id)
    role = require(db, Role, body.role_id)
    reqs = db.execute(select(RoleSkillReq, Skill.name).join(Skill, Skill.id == RoleSkillReq.skill_id)
                      .where(RoleSkillReq.role_id == role.id, RoleSkillReq.level > 0)).all()
    if not reqs:
        raise HTTPException(422, "This role has no positive skill requirements yet.")
    required_ids = {r.skill_id for r, _ in reqs}
    changes = {c.skill_id: c.level for c in body.changes}
    if len(changes) != len(body.changes) or not changes.keys() <= required_ids:
        raise HTTPException(422, "Changes must contain unique skills required by this role.")
    ratings = {r.skill_id: r for r in db.scalars(select(Rating).where(Rating.employee_id == employee.id))}
    denominator = sum(r.level * r.weight for r, _ in reqs)
    current = projected = trusted = hours = 0
    skills = []
    for req, name in reqs:
        rating = ratings.get(req.skill_id)
        have = rating.level if rating else 0
        target = changes.get(req.skill_id, have)
        current += min(have, req.level) * req.weight
        projected += min(target, req.level) * req.weight
        trusted += min(have, req.level) * req.weight * evidence_factor(rating)
        estimate = max(0, target - have) * 25
        hours += estimate
        skills.append({"skill_id": req.skill_id, "skill": name, "have": have, "need": req.level,
            "projected": target, "weight": req.weight, "estimated_hours": estimate,
            "gain_per_level": round(100 * req.weight / denominator, 2),
            "evidence_strength": round(evidence_factor(rating) * 100), "missing_evidence": rating is None})
    # One query for all relevant mentors; do not issue a query per skill.
    mentor_rows = db.execute(select(Employee, Rating, Skill.name).join(Rating, Rating.employee_id == Employee.id)
        .join(Skill, Skill.id == Rating.skill_id).where(Rating.skill_id.in_(required_ids),
            Employee.id != employee.id, Employee.availability > 0, employee_scope(user), Rating.level >= 3,
            Rating.confidence >= .7).order_by(Rating.level.desc(), Employee.id).limit(2000)).all()
    mentors, used = [], set()
    gaps = {s["skill_id"]: s for s in skills if s["have"] < s["need"]}
    for mentor, rating, name in mentor_rows:
        if rating.skill_id in gaps and rating.level >= gaps[rating.skill_id]["need"] and rating.skill_id not in used:
            mentors.append({"employee_id": mentor.id, "name": mentor.name, "skill": name,
                            "skill_id": rating.skill_id, "level": rating.level})
            used.add(rating.skill_id)
    return {"employee": employee.name, "role": role.name, "current_score": round(current / denominator * 100, 1),
            "projected_score": round(projected / denominator * 100, 1),
            "evidence_adjusted_score": round(trusted / denominator * 100, 1),
            "estimated_hours": hours, "estimated_weeks": round(hours / body.weekly_hours, 1),
            "skills": skills, "mentors": mentors,
            "assumptions": "Weighted proficiency coverage; missing evidence counts as 0 for planning, not proof of inability. 25 learning hours per level is an estimate. Simulated progress requires reassessment."}


class Investment(BaseModel):
    budget_hours: int = Field(200, ge=0, le=100000)
    weeks: int = Field(8, ge=1, le=52)
    weekly_hours: int = Field(5, ge=1, le=40)
    demand_multiplier: float = Field(1, ge=.1, le=5, allow_inf_nan=False)


@router.post("/investment")
def investment(body: Investment, db: Session = Depends(get_db)):
    import math
    demands = db.execute(select(SkillDemand, Skill.name).join(Skill, Skill.id == SkillDemand.skill_id)
                        .where(SkillDemand.target_headcount > 0)).all()
    ids = [d.skill_id for d, _ in demands]
    rows = db.execute(select(Rating, Employee).join(Employee, Employee.id == Rating.employee_id)
                      .where(Rating.skill_id.in_(ids))).all() if ids else []
    supply = {sid: 0 for sid in ids}
    for rating, _ in rows:
        if rating.level >= 3:
            supply[rating.skill_id] += 1
    gaps = {d.skill_id: max(0, math.ceil(d.target_headcount * body.demand_multiplier) - supply[d.skill_id]) for d, _ in demands}
    original = dict(gaps)
    names = {d.skill_id: name for d, name in demands}
    options = sorted(((25 * (3 - r.level), r, e) for r, e in rows
                      if 0 < r.level < 3 and e.availability > 0), key=lambda x: (x[0], -x[1].confidence, x[2].id, x[1].skill_id))
    spent, per_person, actions = 0, {}, []
    for cost, rating, employee in options:
        capacity = body.weeks * body.weekly_hours * employee.availability / 100
        if gaps[rating.skill_id] <= 0 or spent + cost > body.budget_hours or per_person.get(employee.id, 0) + cost > capacity:
            continue
        spent += cost
        per_person[employee.id] = per_person.get(employee.id, 0) + cost
        gaps[rating.skill_id] -= 1
        actions.append({"employee_id": employee.id, "name": employee.name, "skill": names[rating.skill_id],
            "skill_id": rating.skill_id, "from_level": rating.level, "to_level": 3, "hours": cost,
            "reason": "Closes one demanded skill slot within learning budget and available capacity."})
    return {"hours_used": spent, "budget_hours": body.budget_hours, "people": len(per_person),
        "skill_slots_closed": len(actions), "shortfall_before": sum(original.values()),
        "shortfall_after": sum(gaps.values()), "actions": actions,
        "skills": [{"skill": names[s], "supply": supply[s], "gap_before": original[s], "gap_after": gaps[s]} for s in ids],
        "method": "Greedy lowest-hours-first heuristic; not a guaranteed global optimum. Each slot is one person-skill pair, not one hire. Learning outcomes are hypothetical until assessed. Only existing L1/L2 learners qualify. Capacity = weeks × weekly hours × availability."}


@router.get("/teams/{pid}")
def team(pid: int, size: int = Query(3, ge=1, le=8), min_available: int = Query(20, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(require_user)):
    project = require(db, Project, pid)
    reqs = db.execute(select(ProjectSkill, Skill.name).join(Skill, Skill.id == ProjectSkill.skill_id)
                      .where(ProjectSkill.project_id == pid, ProjectSkill.level > 0)).all()
    if not reqs:
        raise HTTPException(422, "Add project skill requirements first.")
    required = {r.skill_id: r for r, _ in reqs}
    pool = {}
    rows = db.execute(select(Employee, Rating).join(Rating, Rating.employee_id == Employee.id)
        .where(Employee.availability >= min_available, Rating.skill_id.in_(required), employee_scope(user))).all()
    for employee, rating in rows:
        pool.setdefault(employee.id, {"employee": employee, "ratings": {}})["ratings"][rating.skill_id] = rating.level
    covered = {sid: 0 for sid in required}
    selected = []
    total = sum(r.level * r.weight for r in required.values())
    for _ in range(size):
        def gain(item):
            return sum(max(0, min(item["ratings"].get(sid, 0), r.level) - covered[sid]) * r.weight for sid, r in required.items())
        if not pool:
            break
        best_id = max(pool, key=lambda eid: (gain(pool[eid]), pool[eid]["employee"].availability, -eid))
        best = pool.pop(best_id)
        added = gain(best)
        if added <= 0:
            break
        employee = best["employee"]
        contributions = [name for r, name in reqs if min(best["ratings"].get(r.skill_id, 0), r.level) > covered[r.skill_id]]
        for sid, req in required.items():
            covered[sid] = max(covered[sid], min(best["ratings"].get(sid, 0), req.level))
        selected.append({"employee_id": employee.id, "name": employee.name, "department": employee.department,
            "availability": employee.availability, "contributions": contributions, "added_coverage": round(added / total * 100, 1)})
    return {"project": project.name, "members": selected,
        "coverage": round(sum(covered[s] * r.weight for s, r in required.items()) / total * 100, 1),
        "skills": [{"skill": name, "need": r.level, "covered": covered[r.skill_id]} for r, name in reqs],
        "method": "Greedy complementary skill coverage, then availability. Team coverage uses the highest member level per skill. This is a suggestion; capacity is not reserved across projects."}
