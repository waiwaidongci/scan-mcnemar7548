import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Review, Role, Status
from src.domain.analysis import analyze
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import Delivery, Run
from src.models.source import Dataset
from src.services.datasets import save
from src.utils.exports import write_report
from src.utils.security import hash_password
from src.utils.time import now


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        result = {'users': db.scalar(select(func.count()).select_from(User)),
                  'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                  'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                  'created_dataset': created, 'finance_runs': seed_finance_runs(db, item)}
        return result


# 三个可交付报告：原门槛达标、净增门槛提高后未达标、调整后重跑达标。
FINANCE_RUN_SPECS = (
    ('财务交付·原门槛运行', {'alpha': 0.05, 'min_pairs': 30, 'min_gain_pp': 5}, Review.ACCEPTED),
    ('财务交付·净增20门槛复跑', {'alpha': 0.05, 'min_pairs': 30, 'min_gain_pp': 20}, Review.PENDING),
    ('财务交付·复核后重跑', {'alpha': 0.05, 'min_pairs': 30, 'min_gain_pp': 5}, Review.PENDING),
)


def seed_finance_runs(db, dataset):
    """幂等生成带导出 CSV 的已完成运行和站内交付回执，供财务整批换版演示。"""
    run_ids = []
    recipient = '财务变更负责人'
    for index, (name, params, review) in enumerate(FINANCE_RUN_SPECS, start=1):
        existing = db.scalar(select(Run).where(Run.name == name).order_by(Run.id.desc()).limit(1))
        if existing:
            run_ids.append(existing.id)
            continue
        result = analyze(dataset.rows, params)
        job = Run(name=name, dataset_id=dataset.id, params=params, author='seed',
                  status=Status.COMPLETE, review=review, attempt=1,
                  decision='建议切换' if review == Review.ACCEPTED else None,
                  review_note='达标结论与逐单复核记录随报告交付。' if review == Review.ACCEPTED else None,
                  finished_at=now())
        db.add(job)
        db.flush()
        job.export_file = write_report(job.id, job.attempt, result)
        job.result = result
        db.add(Delivery(run_id=job.id, recipient=recipient, state='delivered',
                        detail=f'交付版本 v1.r{job.id}，已保存 CSV 到站内收件箱'))
        run_ids.append(job.id)
    return run_ids


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
