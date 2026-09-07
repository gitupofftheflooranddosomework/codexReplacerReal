#!/usr/bin/env python3
import importlib.util
import os
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix='codex-pinned-scheduler-') as tmp:
        os.environ['CODEX_LAB_SCHEDULER_DB'] = str(Path(tmp) / 'jobs.sqlite3')
        os.environ['CODEX_LAB_DASHBOARD_AUTH'] = str(Path(tmp) / 'auth.json')
        spec = importlib.util.spec_from_file_location('sched_pin', Path(__file__).with_name('scheduler.py'))
        sched = importlib.util.module_from_spec(spec); spec.loader.exec_module(sched)
        queued = [
            {'id':'pin3-a','requested_station':3},
            {'id':'auto-a','requested_station':None},
            {'id':'pin3-b','requested_station':3},
            {'id':'auto-b','requested_station':None},
            {'id':'pin6','requested_station':6},
            {'id':'auto-c','requested_station':None},
        ]
        plan = [(job['id'], station) for job, station in sched.plan_assignments(queued, [1,2,3,4,5,6])]
        assert plan == [('pin3-a',3),('pin6',6),('auto-a',1),('auto-b',2),('auto-c',4)], plan
        busy_plan = [(job['id'], station) for job, station in sched.plan_assignments(queued, [1,2,4,5,6])]
        assert busy_plan == [('pin6',6),('auto-a',1),('auto-b',2),('auto-c',4)], busy_plan

        job = sched.submit_job({'owner':'PinnedTestBot','project':'pinning','command':'true','requestedStation':5})
        assert job['requestedStation'] == 5 and job['status'] == 'queued'
        try:
            sched.submit_job({'owner':'PinnedTestBot','command':'true','requestedStation':9})
            raise AssertionError('invalid station accepted')
        except ValueError:
            pass

        conn = sched.db()
        sched.record_usage_start(conn,'job','job-history',2,owner='HistoryBot',project='history',chat_label='Chat history proof',chat_url='https://chatgpt.com/c/example',source='test')
        sched.record_usage_end(conn,'job','job-history','succeeded')
        history = sched.worker_usage_history(conn,2,10)
        conn.commit(); conn.close()
        row = next(item for item in history if item['ref_id']=='job-history')
        assert row['owner']=='HistoryBot' and row['chatLabel']=='Chat history proof' and row['status']=='succeeded'

        ext = sched.record_external_usage({'action':'start','kind':'lease','refId':'lease-history','station':4,'owner':'LeaseHistoryBot','project':'browser','chatLabel':'Lease chat','source':'test'})
        assert ext['station']==4 and ext['status']=='running'
        ext = sched.record_external_usage({'action':'end','kind':'lease','refId':'lease-history','station':4,'owner':'LeaseHistoryBot','status':'released','source':'test'})
        assert ext['status']=='released' and ext['finished_at']
        print('{"ok":true,"pinnedPlanning":true,"pinnedPersistence":true,"history":true,"leaseHistory":true}')


if __name__ == '__main__':
    main()
