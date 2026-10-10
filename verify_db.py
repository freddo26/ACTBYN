import sys
sys.path.insert(0, '.')
import app as A
c = A.app.test_client()

# 1. landing page boots without DB server
r = c.get('/')
print('GET / ->', r.status_code)

# 2. login as LGU via SQLite (form fields are e + pass)
r = c.post('/login', data={'e': 'lgu@actbayan.gov.ph', 'pass': 'lgu12345'}, follow_redirects=False)
print('POST /login LGU ->', r.status_code, r.headers.get('Location', '')[:80])
assert r.status_code == 302 and '/lgu' in (r.headers.get('Location', '') or ''), 'LGU login failed'
r = c.post('/login', data={'e': 'lgu@actbayan.gov.ph', 'pass': 'WRONG'}, follow_redirects=True)
assert b'Invalid credentials' in r.data, 'wrong-password path broken'
print('login OK (good + bad password)')

# 3. analytics page (needs cached summaries; simulate lgu_analytics context by priming cache)
ml = ['Apr 2026', 'May 2026', 'Jun 2026']
mc = [2, 4, 3]
mr = [1, 3, 2]
ms = A.generate_monthly_analytics_summary(ml, mc, mr, None,
    {'total': 12, 'resolved': 5, 'pending': 5, 'in_progress': 2},
    [{'locname': 'Picaleon', 'count': 4}, {'locname': 'B', 'count': 2}],
    [{'name': 'Flooding', 'count': 5}, {'name': 'Road', 'count': 3}])
print('offline summaries ->', [len(x) for x in ms])

with c.session_transaction() as s:
    s['user_id'] = 1
    s['user_role'] = 'lgu'
r = c.get('/api/analytics_summary')
j = r.get_json()
print('GET /api/analytics_summary ->', r.status_code, '| keys:', sorted(j.keys()))
print('exec len:', len(j.get('executive_summary') or ''), '| ai_version:', j.get('ai_version'))

# 4. regenerate cycles AI version 1..5
for k in range(6):
    r = c.post('/api/analytics_summary/regenerate')
    print('regen', k, '->', r.get_json().get('message'))

# 5. duplicate check with OpenRouter offline fallback (no network needed for fallback path)
with c.session_transaction() as s:
    s['user_id'] = 10
r = c.post('/api/check_duplicate', json={'title': 'flood', 'description': 'baha sa kalsada', 'geox': 15.6, 'geoy': 121.0})
j = r.get_json()
print('POST /api/check_duplicate ->', r.status_code, '| has_duplicates:', j.get('has_duplicates'), '| n:', len(j.get('duplicates', [])))
print('LIVE TESTS PASSED')

