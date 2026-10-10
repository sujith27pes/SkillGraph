"""Every test run uses a separate SQLite database and no live AI credentials."""
import os
import tempfile
os.environ['DATABASE_URL'] = f"sqlite:///{tempfile.mkdtemp()}/tests.db"
os.environ['NVIDIA_API_KEY'] = ''
os.environ['API_KEY'] = ''
os.environ.pop('APP_ENV', None)
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.security import password_hash
from app.security_models import User
TEST_PASSWORD='A long test-only password 2026!'
with SessionLocal() as db:
    db.add(User(username='test.admin',password_hash=password_hash(TEST_PASSWORD),role='admin'))
    db.commit()
client=TestClient(app)
login=client.post('/auth/login',json={'username':'test.admin','password':TEST_PASSWORD})
assert login.status_code==200,login.text
client.headers['X-CSRF-Token']=login.json()['csrf_token']
