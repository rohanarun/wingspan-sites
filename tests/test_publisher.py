import base64
import struct
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from publisher import create_app

KEY='test-publisher-key'
HEAD={'Authorization':'Bearer '+KEY}
HTML='<!doctype html><html><head><title>Sample</title></head><body><h1>Sample</h1></body></html>'

def client(path):
    return TestClient(create_app(path, KEY, 'https://agency.example'))

def payload(**kw):
    return dict(slug='sample-tool', html=HTML, task_token='sample-task', **kw)

def test_auth_publish_retry_and_restart(tmp_path):
    c=client(tmp_path)
    assert c.post('/api/sites',json=payload()).status_code==401
    r=c.post('/api/sites',json=payload(),headers=HEAD)
    assert r.status_code==200, r.text
    data=r.json()
    assert data['canonical_url']=='https://agency.example/sites/sample-tool/'
    assert not data['social_card_ready']
    assert c.post('/api/sites',json=payload(),headers=HEAD).json()==data
    c=client(tmp_path)
    assert 'Sample' in c.get('/sites/sample-tool/').text
    assert data['canonical_url'] in c.get('/sitemap.xml').text
    assert c.get('/api/sites/sample-tool').status_code==401
    assert c.get('/api/sites/sample-tool',headers=HEAD).json()==data

def test_conflict_update_card_and_stale_retry(tmp_path):
    c=client(tmp_path)
    initial=c.post('/api/sites',json=payload(),headers=HEAD).json()
    other=payload(); other['task_token']='other-task'
    assert c.post('/api/sites',json=other,headers=HEAD).status_code==409
    updated=payload(); updated['html']=HTML.replace('Sample','Revised')
    assert c.post('/api/sites',json=updated,headers=HEAD).status_code==409
    png=b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',1200,630)
    updated.update(expected_revision=1,social_card_path='card.png',assets={'card.png':{'encoding':'base64','content':base64.b64encode(png).decode()}})
    result=c.post('/api/sites',json=updated,headers=HEAD)
    assert result.status_code==200,result.text
    assert result.json()['social_card_ready'] and result.json()['revision']==2
    assert c.get('/sites/sample-tool/card.png').content==png
    assert c.post('/api/sites',json=payload(),headers=HEAD).json()['revision']==2
    assert 'Revised' in c.get('/sites/sample-tool/').text

def test_unsafe_and_invalid_assets(tmp_path):
    c=client(tmp_path)
    for name in ['../hack.js','/hack.js','a/../../hack.js','index.html','.secret.txt','a%2fb.js','a\\b.js']:
        r=c.post('/api/sites',json=payload(assets={name:{'content':'x'}}),headers=HEAD)
        assert r.status_code==422,(name,r.text)
    assert c.post('/api/sites',json=payload(social_card_path='missing.png'),headers=HEAD).status_code==422
    assert c.post('/api/sites',content=b'x'*(8*1024*1024+1),headers=HEAD).status_code==413

def test_simultaneous_claims_are_atomic(tmp_path):
    c=client(tmp_path)
    def write(i):
        p=payload();p['task_token']=str(i)
        return c.post('/api/sites',json=p,headers=HEAD).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes=list(pool.map(write,range(4)))
    assert sorted(codes)==[200,409,409,409]
    schema=c.get('/openapi.json').json()
    assert schema['paths']['/api/sites']['post']['security']


def test_noindex_excluded_from_sitemap(tmp_path):
    c=client(tmp_path)
    p=payload();p['html']=HTML.replace('<head>', '<head><meta name="robots" content="noindex">')
    assert c.post('/api/sites',json=p,headers=HEAD).status_code==200
    assert 'sample-tool' not in c.get('/sitemap.xml').text
    assert c.get('/sites/sample-tool/').status_code==200
