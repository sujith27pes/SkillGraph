"""Authorization regression tests exercise real sessions, not dependency overrides."""
import json
import uuid
from datetime import datetime,timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,delete
from app.main import app
from app.db import SessionLocal
from app.models import Employee,Rating,Skill,Role,RoleSkillReq
from app.security import password_hash, fingerprint, COOKIE
from app.security_models import User,LoginSession,LoginAttempt,Evaluation,AuditEvent
from conftest import client as admin,TEST_PASSWORD

@pytest.fixture
def organization():
    suffix=uuid.uuid4().hex[:8]
    names=[f'North-{suffix}',f'South-{suffix}']
    with SessionLocal() as db:
        people=[Employee(external_id=f'{suffix}-{i}',name=f'Person {suffix} {i}',department=names[i//2],availability=100) for i in range(4)]
        db.add_all(people);db.flush()
        skill=Skill(name='Security-'+suffix);db.add(skill);db.flush()
        role=Role(name='Role-'+suffix);db.add(role);db.flush()
        db.add(RoleSkillReq(role_id=role.id,skill_id=skill.id,level=4,weight=1))
        db.add_all([Rating(employee_id=p.id,skill_id=skill.id,level=2,confidence=.8,source='manual') for p in people])
        # Hash once per fixture; each account uses strong hashing without wasting test time.
        digest=password_hash(TEST_PASSWORD)
        users={r:User(username=f'{r}.{suffix}',password_hash=digest,role=r,employee_id=people[0].id if r=='employee' else None,department=names[0] if r=='manager' else None) for r in ['employee','manager','hr']}
        db.add_all(users.values());db.flush()
        ids=[p.id for p in people];uid={r:u.id for r,u in users.items()}
        db.commit()
        db.execute(delete(LoginAttempt));db.commit()
    clients={}
    for name,u in users.items():
        c=TestClient(app);response=c.post('/auth/login',json={'username':u.username,'password':TEST_PASSWORD})
        assert response.status_code==200,response.text
        c.headers['X-CSRF-Token']=response.json()['csrf_token'];clients[name]=c
    return {'clients':clients,'people':ids,'skill':skill.id,'role':role.id,'users':uid,'departments':names}


def test_anonymous_and_old_api_key_cannot_access():
    c=TestClient(app)
    for path in ['/employees','/analytics/summary','/intelligence/passport/1','/governance/reviews','/auth/users','/reports/skill-matrix.csv']:
        assert c.get(path,headers={'X-API-Key':'change-me'}).status_code==401
    assert c.get('/login.html').status_code==200


def test_employee_self_only_and_no_directory_leaks(organization):
    o=organization;c=o['clients']['employee'];own,other,foreign,_=o['people']
    assert [p['id'] for p in c.get('/employees').json()['items']]==[own]
    assert c.get(f'/employees/{own}').status_code==200
    for path in [f'/employees/{other}',f'/intelligence/passport/{foreign}',f'/employees/{foreign}/assessments',f'/employees/{foreign}/top-roles']:
        assert c.get(path).status_code==403
    assert c.post('/intelligence/simulate',json={'employee_id':foreign,'role_id':o['role']}).status_code==403
    own_sim=c.post('/intelligence/simulate',json={'employee_id':own,'role_id':o['role']}).json()
    assert own_sim['mentors']==[]
    for path in ['/analytics/summary','/succession','/auth/users','/governance/audit','/projects','/reports/workforce-plan.csv']:
        assert c.get(path).status_code==403
    assert c.put(f'/employees/{own}/ratings',json=[{'skill_id':o['skill'],'level':5}]).status_code==403


def test_manager_scoped_lists_and_direct_access(organization):
    o=organization;c=o['clients']['manager']
    ids={p['id'] for p in c.get('/employees').json()['items']}
    assert ids==set(o['people'][:2])
    assert c.get('/employees',params={'department':o['departments'][1]}).json()['items']==[]
    assert c.get(f"/employees/{o['people'][2]}").status_code==403
    assert c.put(f"/employees/{o['people'][2]}/availability?value=100").status_code==403
    assert c.get('/reports/skill-matrix.csv').status_code==403
    assert c.post('/skills',json={'name':'unauthorized'}).status_code==403


def test_hr_cannot_manage_accounts(organization):
    c=organization['clients']['hr']
    assert c.get('/analytics/summary').status_code==200
    assert c.get('/governance/audit').status_code==200
    assert c.get('/auth/users').status_code==403
    assert c.post('/auth/users',json={'username':'illegal','password':TEST_PASSWORD,'role':'admin'}).status_code==403


def test_csrf_origin_cookie_and_logout(organization):
    c=organization['clients']['employee'];token=c.headers.pop('X-CSRF-Token')
    assert c.post('/auth/logout').status_code==403
    c.headers['X-CSRF-Token']=token
    assert c.post('/auth/logout',headers={'Origin':'https://evil.example'}).status_code==403
    assert c.post('/auth/logout').status_code==200
    assert c.get('/auth/me').status_code==401
    anonymous=TestClient(app)
    assert anonymous.post('/auth/login',json={'username':'test.admin','password':TEST_PASSWORD},headers={'Origin':'https://evil.example'}).status_code==403


def test_revocation_and_expired_sessions(organization):
    o=organization;c=o['clients']['manager']
    result=admin.put(f"/auth/users/{o['users']['manager']}",json={'role':'manager','active':False,'department':o['departments'][0]})
    assert result.status_code==200
    assert c.get('/employees').status_code==401
    c=o['clients']['hr']
    with SessionLocal() as db:
        session=db.get(LoginSession,fingerprint(c.cookies.get(COOKIE)));session.expires_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
    assert c.get('/auth/me').status_code==401


def make_review(o,submitted_by=None):
    with SessionLocal() as db:
        rating=db.get(Rating,(o['people'][0],o['skill']))
        evaluation=Evaluation(employee_id=o['people'][0],skill_id=o['skill'],submitted_by=submitted_by or o['users']['employee'],baseline=rating.updated_at.isoformat(),payload=json.dumps({'score':90,'level':4,'confidence':.85,'summary':'Good evidence','per_question':[],'questions':[]}))
        db.add(evaluation);db.commit();return evaluation.id


def test_human_approval_atomic_and_not_replayable(organization):
    o=organization;rid=make_review(o)
    result=o['clients']['manager'].post(f'/governance/reviews/{rid}/decision',json={'decision':'approve','reason':'Reviewed practical examples and correctness','level':3})
    assert result.status_code==200,result.text
    with SessionLocal() as db:
        rating=db.get(Rating,(o['people'][0],o['skill']))
        assert rating.level==3 and rating.source=='human_review'
        assert db.get(Evaluation,rid).status=='approved'
        assert db.scalar(select(AuditEvent).where(AuditEvent.action=='evaluation_approved',AuditEvent.target==str(rid)))
    assert o['clients']['hr'].post(f'/governance/reviews/{rid}/decision',json={'decision':'reject','reason':'Second conflicting decision'}).status_code==409


def test_review_rejects_self_and_stale_evidence(organization):
    o=organization;rid=make_review(o,o['users']['manager'])
    decision={'decision':'approve','reason':'Checked answers against the rubric'}
    assert o['clients']['manager'].post(f'/governance/reviews/{rid}/decision',json=decision).status_code==403
    assert o['clients']['employee'].post(f'/governance/reviews/{rid}/decision',json=decision).status_code==403
    with SessionLocal() as db:
        r=db.get(Rating,(o['people'][0],o['skill']));r.updated_at=datetime.utcnow()+timedelta(seconds=1);db.commit()
    assert o['clients']['hr'].post(f'/governance/reviews/{rid}/decision',json=decision).status_code==409
    with SessionLocal() as db:
        assert db.get(Evaluation,rid).status=='pending'
        assert db.get(Rating,(o['people'][0],o['skill'])).level==2
    assert o['clients']['hr'].post(f'/governance/reviews/{rid}/decision',json={'decision':'reject','reason':'Evidence is stale; reassessment needed'}).status_code==200


def test_review_queue_does_not_leak_other_employees(organization):
    o=organization;rid=make_review(o)
    own=o['clients']['employee'].get('/governance/reviews').json()
    assert any(r['id']==rid for r in own)
    with SessionLocal() as db:
        r=Evaluation(employee_id=o['people'][2],skill_id=o['skill'],submitted_by=o['users']['hr'],payload='{}')
        db.add(r);db.commit();foreign=r.id
    assert not any(r['id']==foreign for r in o['clients']['manager'].get('/governance/reviews').json())


def test_password_change_revokes_all_sessions(organization):
    c=organization['clients']['employee']
    assert c.post('/auth/password',json={'current_password':'wrong','new_password':TEST_PASSWORD+'new'}).status_code==400
    assert c.post('/auth/password',json={'current_password':TEST_PASSWORD,'new_password':TEST_PASSWORD+'new'}).status_code==200
    assert c.get('/auth/me').status_code==401


def test_rate_limit_persists_in_database():
    with SessionLocal() as db:
        db.execute(delete(LoginAttempt));db.add_all(LoginAttempt(account_key='blocked',address='testclient') for _ in range(20));db.commit()
    assert TestClient(app).post('/auth/login',json={'username':'test.admin','password':TEST_PASSWORD}).status_code==429
    with SessionLocal() as db: db.execute(delete(LoginAttempt));db.commit()


def test_csv_cursor_and_source_regressions(organization):
    assert admin.get('/reports/workforce-plan.csv').status_code==200
    assert admin.get(f"/roles/{organization['role']}/candidates?cursor=bad").status_code==422
    assert admin.get('/employees?limit=-1').status_code==422
    assert admin.put(f"/employees/{organization['people'][0]}/ratings",json=[{'skill_id':organization['skill'],'level':5,'source':'human_review'}]).status_code==422


def test_admin_self_lockout_and_mapping_validation():
    current=admin.get('/auth/me').json()['user']
    assert admin.put(f"/auth/users/{current['id']}",json={'role':'employee','active':False}).status_code==409
    suffix=uuid.uuid4().hex
    assert admin.post('/auth/users',json={'username':suffix,'password':TEST_PASSWORD,'role':'employee'}).status_code==422
    assert admin.post('/auth/users',json={'username':suffix,'password':TEST_PASSWORD,'role':'manager','department':'nonexistent'}).status_code==422


def test_backup_preserves_database_and_refuses_overwrite(tmp_path):
    from scripts.backup import backup
    import sqlite3
    target=backup(tmp_path/'verified.db')
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0]>0
    with pytest.raises(FileExistsError):backup(target)

def test_approval_failure_rolls_back_review_rating_and_audit(organization,monkeypatch):
    import app.main as main
    o=organization;rid=make_review(o)
    def fail(*args,**kwargs):raise RuntimeError('Injected readiness failure')
    monkeypatch.setattr(main,'recompute_employees',fail)
    with pytest.raises(RuntimeError):
        o['clients']['hr'].post(f'/governance/reviews/{rid}/decision',json={'decision':'approve','reason':'Evaluated practical skills thoroughly'})
    with SessionLocal() as db:
        assert db.get(Evaluation,rid).status=='pending'
        assert db.get(Rating,(o['people'][0],o['skill'])).level==2
        assert db.scalar(select(AuditEvent).where(AuditEvent.action=='evaluation_approved',AuditEvent.target==str(rid))) is None


def test_manager_team_and_mentor_scope(organization):
    o=organization
    with SessionLocal() as db:
        for eid in [o['people'][1],o['people'][2]]:
            r=db.get(Rating,(eid,o['skill']));r.level=5;r.confidence=.9
        db.commit()
    pid=admin.post('/projects',json={'name':'Scoped-'+uuid.uuid4().hex,'skills':[{'skill_id':o['skill'],'level':4}]}).json()['id']
    c=o['clients']['manager']
    team=c.get(f'/intelligence/teams/{pid}').json()
    assert {m['employee_id'] for m in team['members']}=={o['people'][1]}
    staffing=c.get(f'/projects/{pid}/staffing').json()
    assert {m['employee_id'] for m in staffing['items']} <= set(o['people'][:2])
    simulation=c.post('/intelligence/simulate',json={'employee_id':o['people'][0],'role_id':o['role']}).json()
    assert {m['employee_id'] for m in simulation['mentors']}=={o['people'][1]}


def test_cookie_security_and_successful_account_creation():
    with SessionLocal() as db:db.execute(delete(LoginAttempt));db.commit()
    c=TestClient(app);r=c.post('/auth/login',json={'username':'test.admin','password':TEST_PASSWORD})
    cookie=r.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert c.get('/employees',headers={'Host':'evil.example'}).status_code==400
    username='new.'+uuid.uuid4().hex
    created=admin.post('/auth/users',json={'username':username,'password':TEST_PASSWORD,'role':'hr'})
    assert created.status_code==201,created.text
    assert 'password_hash' not in created.json()
    assert TestClient(app).post('/auth/login',json={'username':username,'password':TEST_PASSWORD}).status_code==200

def test_csv_formula_prefix_whitespace_is_neutralized():
    from app.main import _s
    for value in ['=1+1','\t=1+1','  +1+1','\r\n@SUM(1)','\ufeff-1+2']:
        assert _s(value).startswith("'")
    assert _s('Normal name')=='Normal name'


def test_role_requirement_failure_keeps_previous_version(organization,monkeypatch):
    import app.main as main
    o=organization
    before=admin.get(f"/roles/{o['role']}").json()
    def fail(*args,**kwargs):raise RuntimeError('Injected role refresh failure')
    monkeypatch.setattr(main,'recompute_role_in_session',fail)
    with pytest.raises(RuntimeError):admin.put(f"/roles/{o['role']}/requirements",json=[{'skill_id':o['skill'],'level':5}])
    assert admin.get(f"/roles/{o['role']}").json()==before

def test_question_generation_requires_csrf_and_get_is_read_only(organization,monkeypatch):
    from app import ai
    o=organization;c=o['clients']['employee']
    def unexpected(*args,**kwargs):raise AssertionError('GET must not invoke AI')
    monkeypatch.setattr(ai,'questions_for',unexpected)
    assert c.get(f"/skills/{o['skill']}/questions").json()==[]
    csrf=c.headers.pop('X-CSRF-Token')
    assert c.post(f"/skills/{o['skill']}/questions").status_code==403
    c.headers['X-CSRF-Token']=csrf
    assert c.get('/api-schema').status_code==403
    assert admin.get('/api-schema').status_code==200

def test_invalid_record_identifiers_do_not_crash():
    for value in ['not-a-number','-1','99999999999999999999999999999']:
        assert admin.get('/employees/'+value).status_code==422
