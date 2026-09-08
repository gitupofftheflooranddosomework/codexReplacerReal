#!/usr/bin/env python3
import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def post(base, payload):
    request = urllib.request.Request(
        base.rstrip('/') + '/api/usage',
        data=json.dumps(payload, separators=(',', ':')).encode(),
        method='POST',
        headers={'Content-Type':'application/json','Accept':'application/json'},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def status_for(event):
    reason = str(event.get('reason') or '').strip()
    if reason == 'remote-lock-lost':
        return 'lost'
    if reason == 'expired':
        return 'expired'
    return reason or ('released' if event.get('event') == 'sign_out' else 'ended')


def main():
    parser = argparse.ArgumentParser(description='Backfill full-KVM interactive lease history into the scheduler DB.')
    parser.add_argument('--audit', default='/tank/codex-lab-vm/sign-in-out.jsonl')
    parser.add_argument('--scheduler', default='http://192.168.122.1:8766')
    args = parser.parse_args()
    path = Path(args.audit)
    leases = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            try:
                event = json.loads(raw)
            except Exception:
                continue
            lease_id = str(event.get('leaseId') or '').strip()
            station = event.get('station')
            if not lease_id or station is None:
                continue
            item = leases.setdefault(lease_id, {'start':None,'end':None})
            if event.get('event') == 'sign_in':
                if item['start'] is None or str(event.get('time') or '') < str(item['start'].get('time') or ''):
                    item['start'] = event
            elif event.get('event') in ('sign_out','auto_sign_out'):
                if item['end'] is None or str(event.get('time') or '') < str(item['end'].get('time') or ''):
                    item['end'] = event
    imported = 0
    ended = 0
    for lease_id, pair in leases.items():
        start = pair['start'] or pair['end']
        if not start:
            continue
        payload = {
            'action':'start','kind':'lease','refId':lease_id,'station':int(start['station']),
            'owner':start.get('owner'),'project':start.get('project'),'chatLabel':start.get('chatLabel'),
            'chatUrl':start.get('chatUrl'),'startedAt':start.get('time'),'source':'vm-lab-audit-backfill',
        }
        post(args.scheduler, payload); imported += 1
        if pair['end']:
            end = pair['end']
            post(args.scheduler, {
                **payload, 'action':'end','finishedAt':end.get('time'),'status':status_for(end),
            })
            ended += 1
    print(json.dumps({'ok':True,'leasesImported':imported,'leasesEnded':ended,'audit':str(path)},sort_keys=True))


if __name__ == '__main__':
    main()
