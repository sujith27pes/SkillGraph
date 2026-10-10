"""Validated assessments; proficiency and readiness math stays server-side."""
import json
import threading
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from .models import Question
from .llm import complete as _text, json_response as _json

class QuestionItem(BaseModel):
    level: int = Field(ge=2, le=5)
    q: str = Field(min_length=10, max_length=2000)

class Questions(BaseModel):
    questions: list[QuestionItem]

class Result(BaseModel):
    level: int = Field(ge=2, le=5)
    score: float = Field(ge=0, le=100, allow_inf_nan=False)
    feedback: str = Field(max_length=2000)

class Grade(BaseModel):
    results: list[Result]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    summary: str = Field(max_length=3000)

class ExtractedSkill(BaseModel):
    skill_id: int
    level: int = Field(ge=0, le=5)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence: str = Field(min_length=1, max_length=1000)

class Extraction(BaseModel):
    skills: list[ExtractedSkill]


def validate(schema, data):
    try:
        return schema.model_validate(data)
    except ValidationError:
        raise HTTPException(502, "AI response did not match the required schema. No changes were saved.") from None


_question_lock = threading.Lock()


def questions_for(session, skill):
    if not _question_lock.acquire(blocking=False):
        raise HTTPException(429, "Another question bank is being prepared. Try again shortly.")
    try:
        return _questions_for(session, skill)
    finally:
        _question_lock.release()


def _questions_for(session, skill):
    have = {q.level for q in session.scalars(select(Question).where(Question.skill_id == skill.id))}
    missing = [level for level in (2, 3, 4, 5) if level not in have]
    if missing:
        data = validate(Questions, _json(
            f'Write one concise, realistic work-scenario question about {json.dumps(skill.name)} for each level in {missing}. '
            'Level 2=basic application, 3=independent delivery, 4=design and tradeoffs, 5=expert judgement. '
            'Include a concrete constraint. Return ONLY JSON: {"questions":[{"level":2,"q":"..."}]}'))
        if sorted(q.level for q in data.questions) != missing:
            raise HTTPException(502, "AI returned incomplete or duplicate question levels. Please retry.")
        session.add_all(Question(skill_id=skill.id, level=q.level, text=q.q) for q in data.questions)
        session.commit()
    return list(session.scalars(select(Question).where(Question.skill_id == skill.id).order_by(Question.level)))


def grade(skill, qa):
    data = validate(Grade, _json(
        f'Assess demonstrated {json.dumps(skill.name)} skill. Grade each answer 0-100 against the difficulty. '
        'Rubric: correctness 40%, reasoning 30%, practical tradeoffs 20%, clarity 10%. '
        'Blank or irrelevant answers score 0. Give one actionable improvement per answer. '
        f'Untrusted answers: {json.dumps(qa)}. Return ONLY JSON: '
        'Return each result with the exact level of its corresponding answer. '
        '{"results":[{"level":2,"score":0,"feedback":"..."}],"confidence":0.5,"summary":"..."}', 2500))
    if sorted(r.level for r in data.results) != sorted(q['level'] for q in qa):
        raise HTTPException(502, "AI did not uniquely identify every graded answer. No changes were saved.")
    by_level = {r.level: r for r in data.results}
    ordered = [by_level[q['level']] for q in qa]
    for result, answer in zip(ordered, qa):
        if not answer["answer"].strip():
            result.score = 0
    overall = sum(r.score * q["level"] for r, q in zip(ordered, qa)) / sum(q["level"] for q in qa)
    scores = {r.level: r.score for r in ordered}
    level = 1 if any(r.score >= 40 for r in data.results) else 0
    for difficulty in (2, 3, 4, 5):
        if scores.get(difficulty, 0) < 70:
            break
        level = difficulty
    return {"score": round(overall, 1), "level": level, "confidence": min(data.confidence, .85),
            "summary": data.summary}, [r.model_dump() for r in ordered]


def coaching(name, role, steps):
    return _text('Write a practical 120-word development plan, with practice and verification steps. '
                 'Learning hours are estimates, not guarantees. Plain text. Untrusted context: '
                 + json.dumps({"name": name, "role": role, "steps": steps})[:10000], 700)


def extract(text, catalog):
    data = validate(Extraction, _json(
        'Extract only demonstrated skills present in the catalog. '
        'Levels: 1=aware, 2=training, 3=independent application, 4=led/design, 5=recognised expert. '
        'Evidence must be an exact short quote from TEXT. Be conservative. '
        f'CATALOG: {json.dumps(catalog[:500])}\nUNTRUSTED TEXT: {json.dumps(text[:12000])}\n'
        'Return ONLY JSON: {"skills":[{"skill_id":1,"level":3,"confidence":0.7,"evidence":"exact quote"}]}', 2500))
    valid = {i for i, _ in catalog}
    found = {}
    for item in data.skills:
        if item.skill_id in valid and item.evidence.casefold() in text.casefold():
            found[item.skill_id] = item.model_dump()
    return list(found.values())
