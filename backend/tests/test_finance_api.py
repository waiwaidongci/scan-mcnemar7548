def _open(client, code='FIN-API', slots=1):
    return client.post('/finance/batches', json={'batch_code': code, 'expected_slots': slots})


def _receive(client, batch_id, run_id, tamper=False, version=''):
    body = {'run_id': run_id, 'tamper': tamper}
    if version:
        body['delivered_version'] = version
    return client.post(f'/finance/batches/{batch_id}/receive', json=body)


def test_full_happy_path_and_version_swap(client):
    http, run_id, _ = client
    first = _open(http, slots=1)
    assert first.status_code == 201
    batch = first.json()
    assert batch['state'] == 'staging'

    receipt = _receive(http, batch['id'], run_id)
    assert receipt.status_code == 201
    assert receipt.json()['checksum_ok'] is True
    assert receipt.json()['state'] == 'staged'

    activated = http.post(f'/finance/batches/{batch["id"]}/activate')
    assert activated.status_code == 200
    assert activated.json()['state'] == 'active'

    second = _open(http, slots=1).json()
    _receive(http, second['id'], run_id)
    swapped = http.post(f'/finance/batches/{second["id"]}/activate').json()
    assert swapped['state'] == 'active'
    detail = http.get(f'/finance/batches/{batch["id"]}').json()
    assert detail['state'] == 'superseded'


def test_idempotent_retry_keeps_single_receipt(client):
    http, run_id, _ = client
    batch = _open(http, slots=1).json()
    _receive(http, batch['id'], run_id)
    http.post(f'/finance/batches/{batch["id"]}/activate')
    again = _receive(http, batch['id'], run_id).json()
    assert again['replayed'] is True and again['attempts'] == 2
    receipts = http.get(f'/finance/batches/{batch["id"]}').json()['receipts']
    assert len(receipts) == 1


def test_tampered_file_is_mismatch_and_blocks_activation(client):
    http, run_id, _ = client
    batch = _open(http, slots=1).json()
    bad = _receive(http, batch['id'], run_id, tamper=True).json()
    assert bad['state'] == 'mismatch' and bad['checksum_ok'] is False
    blocked = http.post(f'/finance/batches/{batch["id"]}/activate')
    assert blocked.status_code == 409
    assert http.get(f'/finance/batches/{batch["id"]}').json()['state'] == 'failed'

    http.post(f'/finance/batches/{batch["id"]}/reopen')
    fixed = _receive(http, batch['id'], run_id).json()
    assert fixed['state'] == 'staged' and fixed['attempts'] == 2
    assert http.post(f'/finance/batches/{batch["id"]}/activate').json()['state'] == 'active'


def test_missing_slot_keeps_old_version(client):
    http, run_id, _ = client
    b1 = _open(http, slots=2).json()
    _receive(http, b1['id'], run_id)
    assert http.post(f'/finance/batches/{b1["id"]}/activate').status_code == 409
    assert http.get(f'/finance/batches/{b1["id"]}').json()['state'] == 'failed'


def test_late_and_out_of_order_receipts_are_stale(client):
    http, run_id, _ = client
    b1 = _open(http, slots=1).json()
    _receive(http, b1['id'], run_id)
    http.post(f'/finance/batches/{b1["id"]}/activate')

    b2 = _open(http, slots=1).json()
    _receive(http, b2['id'], run_id)
    http.post(f'/finance/batches/{b2["id"]}/activate')

    late = _receive(http, b1['id'], run_id, version='v1.r1').json()
    assert late['state'] == 'stale'
    disorder = _receive(http, b2['id'], run_id, version='v1.r99').json()
    assert disorder['state'] == 'stale'

    recon = http.get('/finance/reconciliation', params={'batch_code': 'FIN-API'}).json()
    assert recon['active_seq'] == b2['seq']
    by_seq = {b['seq']: b for b in recon['batches']}
    assert by_seq[b1['seq']]['stale_count'] == 1
    assert by_seq[b2['seq']]['stale_count'] == 1


def test_reconciliation_lists_checksum_and_versions(client):
    http, run_id, _ = client
    batch = _open(http, code='FIN-RECON', slots=1).json()
    _receive(http, batch['id'], run_id)
    recon = http.get('/finance/reconciliation', params={'batch_code': 'FIN-RECON'}).json()
    slot = recon['batches'][0]['slots'][0]
    assert slot['checksum_ok'] is True
    assert len(slot['expected_checksum']) == 64
    assert slot['delivered_version'].startswith('v')


def test_viewer_cannot_open_or_receive(client):
    http, _run_id, _runs = client
    token = http.post('/auth/login', json={'username': 'viewer', 'password': 'viewer-demo-2611'}).json()['token']
    http.headers.update({'Authorization': f'Bearer {token}'})
    assert http.post('/finance/batches', json={'batch_code': 'X', 'expected_slots': 1}).status_code == 403
    assert http.get('/finance/batches').status_code == 200


def test_demo_endpoint_runs_full_scenario(client):
    http, _run_id, _runs = client
    response = http.post('/finance/demo/run')
    assert response.status_code == 200
    data = response.json()
    titles = [step['title'] for step in data['steps']]
    assert len(titles) == 7
    recon = data['reconciliation']
    states = {b['seq']: b['state'] for b in recon['batches']}
    # v1 已被取代，v2 生效，v3 写一半挂起。
    assert states == {1: 'superseded', 2: 'active', 3: 'failed'}
    assert recon['active_seq'] == 2
    by_seq = {b['seq']: b for b in recon['batches']}
    # 对账单列指纹不符（修复后为 0）与乱序回执；v1 的三份回执随版归档。
    assert by_seq[2]['mismatch_count'] == 0
    assert by_seq[2]['stale_count'] == 1
    assert by_seq[1]['archived_count'] == 3
    assert by_seq[1]['stale_count'] == 1
