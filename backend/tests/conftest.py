import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

ARTIFACTS = tempfile.mkdtemp(prefix='finance-artifacts-')
os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:///:memory:')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('ARTIFACT_DIR', ARTIFACTS)
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')


class _FakeBroker:
    def ping(self):
        return True

    def eval(self, *args):
        return 1


@pytest.fixture()
def db():
    from src.config import database
    from src.models import finance
    from src.models import identity  # noqa: F401
    from src.models import jobs  # noqa: F401
    from src.models import source  # noqa: F401
    engine = create_engine('sqlite+pysqlite:///:memory:',
                           connect_args={'check_same_thread': False, 'timeout': 30},
                           poolclass=StaticPool)
    database.engine = engine
    database.Session.configure(bind=engine)
    finance.Base.metadata.create_all(engine)
    with database.Session.begin() as session:
        yield session
    finance.Base.metadata.drop_all(engine)
    database.engine.dispose()


@pytest.fixture()
def client(monkeypatch):
    import src.utils.wakeup as wakeup
    monkeypatch.setattr(wakeup, 'broker', _FakeBroker())

    from src.config import database
    engine = create_engine('sqlite+pysqlite:///:memory:',
                           connect_args={'check_same_thread': False, 'timeout': 30},
                           poolclass=StaticPool)
    database.engine = engine
    database.Session.configure(bind=engine)

    from src.models import finance  # noqa: F401
    from src.models import identity
    from src.models import jobs  # noqa: F401
    from src.models import source  # noqa: F401
    database.Base.metadata.create_all(engine)

    from src.constants.enums import Review, Role, Status
    from src.domain.analysis import analyze
    from src.domain.demo import demo_rows
    from src.models.jobs import Run
    from scripts.seed_demo import FINANCE_RUN_SPECS
    from src.services.datasets import save
    from src.utils.exports import write_report
    from src.utils.security import hash_password
    from src.utils.time import now
    run_ids = []
    with database.Session.begin() as session:
        session.add(identity.User(username='admin', role=Role.ADMIN,
                                  password_hash=hash_password('admin-demo-2611')))
        session.add(identity.User(username='viewer', role=Role.VIEWER,
                                  password_hash=hash_password('viewer-demo-2611')))
        dataset, _ = save(session, '演示批次', 'sample.csv', demo_rows(), 'seed')
        for name, params, review in FINANCE_RUN_SPECS:
            result = analyze(dataset.rows, params)
            run = Run(name=name, dataset_id=dataset.id, params=params, author='seed',
                      status=Status.COMPLETE, review=review, attempt=1,
                      decision='建议切换' if review == Review.ACCEPTED else None,
                      finished_at=now())
            session.add(run)
            session.flush()
            run.export_file = write_report(run.id, 1, result)
            run.result = result
            run_ids.append(run.id)

    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as test_client:
        token = test_client.post('/auth/login',
                                 json={'username': 'admin', 'password': 'admin-demo-2611'}).json()['token']
        test_client.headers.update({'Authorization': f'Bearer {token}'})
        yield test_client, run_ids[0], run_ids
    engine.dispose()
