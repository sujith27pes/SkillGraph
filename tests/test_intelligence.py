import uuid
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app import ai, llm
from app.db import SessionLocal
from app.models import Question, Rating

from conftest import client

@pytest.fixture
def setup():
    suffix = uuid.uuid4().hex[:8]
    skills = [client.post('/skills', json={'name': f'{name}-{suffix}'}).json()['id'] for name in ['Engineering','Communication']]
    role = client.post('/roles', json={'name': 'Role-'+suffix, 'requirements': [{'skill_id': s, 'level': 4} for s in skills]}).json()['id']
    client.post('/employees/bulk', json=[{'external_id': suffix+str(i), 'name': 'Person-'+suffix+str(i), 'availability': av} for i,av in enumerate([100,100,0])])
    people = client.get('/employees', params={'q':suffix}).json()['items']
    for i,p in enumerate(people):
        client.put(f"/employees/{p['id']}/ratings",json=[{'skill_id':s,'level':level,'confidence':.9} for s,level in zip(skills,[(2,4),(4,2),(2,2)][i])])
    return skills, role, [p['id'] for p in people]


def test_simulation_is_explainable_and_read_only(setup):
    skills, role, people = setup
    before=client.get(f'/employees/{people[0]}').json()
    result=client.post('/intelligence/simulate',json={'employee_id':people[0],'role_id':role,'changes':[{'skill_id':skills[0],'level':4}]}).json()
    assert result['current_score']==75 and result['projected_score']==100
    assert result['estimated_hours']==50 and result['estimated_weeks']==10
    assert result['evidence_adjusted_score']<=result['current_score']
    assert result['mentors'][0]['employee_id']==people[1]
    assert client.get(f'/employees/{people[0]}').json()==before


def test_scenario_rejects_duplicates_unknown_people_and_skills(setup):
    skills,role,people=setup
    base={'employee_id':people[0],'role_id':role}
    assert client.post('/intelligence/simulate',json={**base,'changes':[{'skill_id':skills[0],'level':5}]*2}).status_code==422
    assert client.post('/intelligence/simulate',json={**base,'changes':[{'skill_id':999999,'level':5}]}).status_code==422
    assert client.post('/intelligence/simulate',json={**base,'employee_id':999999}).status_code==404


def test_investment_respects_budget_capacity_and_no_writes(setup):
    skills,role,people=setup
    for s in skills: client.put(f'/demand/{s}?target_headcount=3')
    with SessionLocal() as db: before=[(r.employee_id,r.skill_id,r.level) for r in db.query(Rating).all()]
    result=client.post('/intelligence/investment',json={'budget_hours':25,'weeks':8,'weekly_hours':5}).json()
    assert result['hours_used']<=25 and sum(a['hours'] for a in result['actions'])==result['hours_used']
    assert not any(a['employee_id']==people[2] for a in result['actions'])
    assert result['shortfall_before']-result['shortfall_after']==len(result['actions'])
    zero=client.post('/intelligence/investment',json={'budget_hours':0}).json()
    assert zero['actions']==[] and zero['hours_used']==0
    short=client.post('/intelligence/investment',json={'weeks':1,'weekly_hours':1}).json()
    assert short['actions']==[]
    with SessionLocal() as db: assert before==[(r.employee_id,r.skill_id,r.level) for r in db.query(Rating).all()]


def test_complementary_team(setup):
    skills,role,people=setup
    pid=client.post('/projects',json={'name':uuid.uuid4().hex,'skills':[{'skill_id':s,'level':4} for s in skills]}).json()['id']
    one=client.get(f'/intelligence/teams/{pid}?size=1').json()
    two=client.get(f'/intelligence/teams/{pid}?size=2').json()
    assert one['coverage']==75 and two['coverage']==100
    assert {m['employee_id'] for m in two['members']}==set(people[:2])
    assert client.get(f'/intelligence/teams/{pid}?size=0').status_code==422


def test_passport_flags_stale_evidence(setup):
    from datetime import datetime, timedelta
    skills,role,people=setup
    with SessionLocal() as db:
        rating=db.get(Rating,(people[0],skills[0]));rating.updated_at=datetime.utcnow()-timedelta(days=730);db.commit()
    passport=client.get(f'/intelligence/passport/{people[0]}').json()
    stale=next(s for s in passport['skills'] if s['skill_id']==skills[0])
    assert stale['needs_review'] and stale['evidence_strength']==22
    assert client.get('/intelligence/passport/9999999').status_code==404


def test_grade_cannot_inflate_expertise(monkeypatch):
    monkeypatch.setattr(ai,'_json',lambda *a: {'results':[{'level':i,'score':score,'feedback':'ok'} for i,score in [(2,100),(3,40),(4,100),(5,100)]], 'confidence':1,'summary':'result'})
    qa=[{'question':'q','level':i,'answer':'answer'} for i in [2,3,4,5]]
    summary,_=ai.grade(SimpleNamespace(name='Python'),qa)
    assert summary['level']==2 and summary['confidence']==.85


def test_blank_answers_score_zero(monkeypatch):
    monkeypatch.setattr(ai,'_json',lambda *a: {'results':[{'level':i,'score':100,'feedback':'ok'} for i in [2,3,4,5]],'confidence':.9,'summary':'test'})
    summary,_=ai.grade(SimpleNamespace(name='Python'),[{'question':'q','level':i,'answer':'  '} for i in [2,3,4,5]])
    assert summary['level']==0 and summary['score']==0

@pytest.mark.parametrize('response',[
    {'results':[],'confidence':.5,'summary':'short'},
    {'results':[{'level':i,'score':float('nan'),'feedback':'bad'} for i in [2,3,4,5]],'confidence':.5,'summary':'bad'},
    {'results':[{'level':i,'score':101,'feedback':'bad'} for i in [2,3,4,5]],'confidence':.5,'summary':'bad'},
])
def test_malformed_grades_are_rejected(monkeypatch,response):
    monkeypatch.setattr(ai,'_json',lambda *a:response)
    with pytest.raises(HTTPException) as error:
        ai.grade(SimpleNamespace(name='Python'),[{'question':'q','level':i,'answer':'a'} for i in [2,3,4,5]])
    assert error.value.status_code==502


def test_question_validation_and_cache(setup,monkeypatch):
    skills,role,people=setup
    calls=[]
    def generate(*args):
        calls.append(1)
        return {'questions':[{'level':i,'q':'Describe how you solve this scenario.'} for i in [2,3,4,5]]}
    monkeypatch.setattr(ai,'_json',generate)
    q=client.post(f'/skills/{skills[0]}/questions').json()
    assert len(q)==4
    assert client.post(f'/skills/{skills[0]}/questions').json()==q and len(calls)==1
    answers=[{'question_id':q[0]['id'],'answer':'a'}]*4
    assert client.post(f'/employees/{people[0]}/assessments',json={'skill_id':skills[0],'answers':answers}).status_code==422
    assert client.post(f'/employees/{people[0]}/assessments',json={'skill_id':skills[0],'answers':answers[:1]}).status_code==422


def test_extraction_requires_real_quotes(monkeypatch):
    monkeypatch.setattr(ai,'_json',lambda *a:{'skills':[
        {'skill_id':1,'level':3,'confidence':.8,'evidence':'Built Python tools'},
        {'skill_id':2,'level':5,'confidence':.9,'evidence':'Invented evidence'}]})
    assert len(ai.extract('Built Python tools for reports',[(1,'Python'),(2,'SQL')]))==1


def test_ai_status_and_credentials(monkeypatch):
    monkeypatch.delenv('NVIDIA_API_KEY',raising=False)
    assert client.get('/intelligence/status').json()['configured'] is False
    with pytest.raises(HTTPException) as error: llm.complete('test')
    assert error.value.status_code==503
    anonymous=TestClient(app)
    assert anonymous.get('/intelligence/status').status_code==401
    assert client.get('/intelligence/status').status_code==200
    assert anonymous.get('/').status_code==200


def test_gateway_rate_limit_and_timeout(monkeypatch):
    import httpx
    monkeypatch.setenv('NVIDIA_API_KEY','not-a-real-key')
    monkeypatch.setattr(llm,'_cooldown_until',0)
    class FakeClient:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,*args,**kwargs): return httpx.Response(429)
    monkeypatch.setattr(llm.httpx,'Client',FakeClient)
    with pytest.raises(HTTPException) as error: llm.complete('test')
    assert error.value.status_code==429 and llm.status()['cooldown_seconds']>0
    monkeypatch.setattr(llm,'_cooldown_until',0)
    def timeout(*args,**kwargs): raise httpx.ReadTimeout('timeout')
    monkeypatch.setattr(FakeClient,'post',timeout)
    with pytest.raises(HTTPException) as error: llm.complete('test')
    assert error.value.status_code==504
    assert llm._gate.acquire(blocking=False)
    llm._gate.release()

def test_failed_recompute_rolls_back_rating_and_audit(setup,monkeypatch):
    import app.main as main
    from app.models import Assessment
    skills,role,people=setup
    before=client.get(f'/employees/{people[0]}').json()
    with SessionLocal() as db: count=db.query(Assessment).count()
    def fail(*args,**kwargs): raise RuntimeError('Injected failure')
    monkeypatch.setattr(main,'recompute_employees',fail)
    with pytest.raises(RuntimeError):
        client.put(f'/employees/{people[0]}/ratings',json=[{'skill_id':skills[0],'level':5}])
    assert client.get(f'/employees/{people[0]}').json()==before
    with SessionLocal() as db: assert db.query(Assessment).count()==count


def test_extraction_retains_quote_and_protects_assessed_skills(setup,monkeypatch):
    skills,role,people=setup
    with SessionLocal() as db:
        rating=db.get(Rating,(people[0],skills[0]));rating.level=4;rating.source='human_review';db.commit()
    monkeypatch.setattr(ai,'extract',lambda *args:[{'skill_id':s,'level':3,'confidence':.8,'evidence':'Delivered a project'} for s in skills])
    result=client.post(f'/employees/{people[0]}/extract-skills',json={'text':'Delivered a project','apply':True}).json()
    assert result['applied']==1
    passport=client.get(f'/intelligence/passport/{people[0]}').json()
    mapping={s['skill_id']:s for s in passport['skills']}
    assert mapping[skills[0]]['level']==4
    assert mapping[skills[1]]['evidence']=='Delivered a project'


def test_invalid_question_bank_never_persists(setup,monkeypatch):
    skills,role,people=setup
    monkeypatch.setattr(ai,'_json',lambda *args:{'questions':[{'level':2,'q':'Duplicate scenario question'}]*4})
    assert client.post(f'/skills/{skills[0]}/questions').status_code==502
    with SessionLocal() as db: assert db.query(Question).filter_by(skill_id=skills[0]).count()==0


def test_unknown_employee_bulk_rating_rejected(setup):
    skills,role,people=setup
    assert client.post('/ratings/bulk',json=[{'employee_id':999999,'skill_id':skills[0],'level':3}]).status_code==422

def test_successful_assessment_updates_history_and_readiness(setup,monkeypatch):
    skills,role,people=setup
    def response(prompt,*args):
        if 'questions' in prompt:
            return {'questions':[{'level':i,'q':'Explain a practical approach to this scenario.'} for i in [2,3,4,5]]}
        return {'results':[{'level':i,'score':90,'feedback':'Sound reasoning'} for i in [2,3,4,5]],'confidence':.9,'summary':'Demonstrated expertise across the supplied scenarios.'}
    monkeypatch.setattr(ai,'_json',response)
    questions=client.post(f'/skills/{skills[0]}/questions').json()
    result=client.post(f'/employees/{people[0]}/assessments',json={'skill_id':skills[0],'answers':[{'question_id':q['id'],'answer':'A detailed practical answer'} for q in questions]})
    assert result.status_code==200 and result.json()['level']==5
    history=client.get(f'/employees/{people[0]}/assessments').json()
    assert result.json()['status']=='pending'
    assert not any(item['source']=='human_review' for item in history)
    assert client.get(f'/employees/{people[0]}/readiness/{role}').json()['score']==75


def test_reordered_ai_grades_match_question_levels(monkeypatch):
    monkeypatch.setattr(ai, '_json', lambda *a: {'results': [
        {'level':5,'score':100,'feedback':'expert'},
        {'level':3,'score':40,'feedback':'gap'},
        {'level':4,'score':90,'feedback':'design'},
        {'level':2,'score':80,'feedback':'basic'}], 'confidence':.8,'summary':'test'})
    summary, per = ai.grade(SimpleNamespace(name='SQL'), [{'question':'q','level':i,'answer':'answer'} for i in [2,3,4,5]])
    assert [r['feedback'] for r in per] == ['basic','gap','design','expert']
    assert summary['level'] == 2
    assert summary['score'] == 81.4


def test_duplicate_grade_levels_rejected(monkeypatch):
    monkeypatch.setattr(ai, '_json', lambda *a: {'results': [{'level':2,'score':100,'feedback':'ok'}]*4,'confidence':.8,'summary':'test'})
    with pytest.raises(HTTPException) as error:
        ai.grade(SimpleNamespace(name='SQL'), [{'question':'q','level':i,'answer':'answer'} for i in [2,3,4,5]])
    assert error.value.status_code == 502
