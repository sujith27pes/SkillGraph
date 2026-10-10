"""Create the first administrator interactively; no shipped/default passwords."""
import getpass
from sqlalchemy import select, func
from app.db import Base, SessionLocal, engine
from app.security import password_hash
from app.security_models import User, AuditEvent

def main():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise SystemExit('Accounts already exist. Sign in as administrator to manage access.')
        username=input('Administrator username: ').strip().lower()
        import re
        if not re.fullmatch(r'[a-z0-9_.@-]{3,100}',username): raise SystemExit('Use 3-100 letters, numbers, or _.@-')
        password=getpass.getpass('Password (at least 15 characters): ')
        if len(password)<15 or len(password)>256: raise SystemExit('Password must contain 15-256 characters.')
        if password!=getpass.getpass('Confirm password: '): raise SystemExit('Passwords did not match.')
        user=User(username=username,password_hash=password_hash(password),role='admin')
        db.add(user);db.flush()
        db.add(AuditEvent(actor_id=user.id,action='admin_bootstrapped',target=str(user.id),detail='Local administrator setup'))
        db.commit()
    print('Administrator created. Start the app and sign in. Add HR, managers, and employees in Access management.')

if __name__=='__main__':main()
