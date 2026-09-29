"""Authenticated, durable static-site publishing; no model or account credentials in pages."""
import base64
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

PUBLIC = Path(__file__).parent / "public"
LIMIT = 8 * 1024 * 1024
EXTENSIONS = {'.html', '.css', '.js', '.json', '.txt', '.xml', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.woff', '.woff2', '.wasm', '.mp3', '.mp4', '.csv'}

class Asset(BaseModel):
    content: str
    encoding: str = Field(default="utf-8", pattern="^(utf-8|base64)$")

class Site(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=1, max_length=100)
    html: str = Field(min_length=20, max_length=600_000)
    title: str = Field(default="", max_length=160)
    task_token: str = Field(min_length=1, max_length=160, description="Stable publication identity. Reuse on retries; never use another site's token.")
    source_url: str = Field(default="", max_length=2048)
    assets: dict[str, Asset] = Field(default_factory=dict, description="Relative file paths mapped to UTF-8 or base64 contents. Example: social-card.png.")
    expected_revision: int | None = Field(default=None, description="Required when replacing content. Use revision from the last successful response. Exact retries are idempotent.")
    social_card_path: str = Field(default="", max_length=200, description="Optional PNG asset path. Must be a 1200 by 630 PNG. Enables preview-ready responses.")

class Publication(BaseModel):
    success: bool = True
    slug: str
    task_token: str
    url: str
    canonical_url: str
    card_url: str
    social_card_ready: bool
    social_card_url: str
    revision: int
    sha256: str


def create_app(data_dir=None, api_key=None, public_base=None):
    directory = Path(data_dir or os.environ.get('PUBLISH_DATA_DIR', './data'))
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / 'sites.sqlite3'
    key = api_key or os.environ.get('PUBLISH_API_KEY', '')
    base = (public_base or os.environ.get('PUBLIC_BASE_URL', '')).rstrip('/')
    if not key or not base.startswith('https://'):
        raise RuntimeError('PUBLISH_API_KEY and an HTTPS PUBLIC_BASE_URL must be configured.')
    @contextmanager
    def db():
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    with db() as conn:
        conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sites (slug TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, revision INTEGER NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (slug TEXT NOT NULL, path TEXT NOT NULL, content BLOB NOT NULL, mime TEXT NOT NULL, PRIMARY KEY(slug,path));
        CREATE TABLE IF NOT EXISTS requests (slug TEXT NOT NULL, request_hash TEXT NOT NULL, PRIMARY KEY(slug,request_hash));
        ''')
        if 'indexable' not in {row[1] for row in conn.execute('PRAGMA table_info(sites)')} :
            conn.execute('ALTER TABLE sites ADD COLUMN indexable INTEGER NOT NULL DEFAULT 1')
            for row in conn.execute("SELECT slug,content FROM files WHERE path='index.html'").fetchall():
                old_soup = BeautifulSoup(bytes(row['content']).decode(), 'html.parser')
                indexable = not any('noindex' in tag.get('content', '').lower() for tag in old_soup.find_all('meta', attrs={'name': re.compile('^(robots|googlebot)$', re.I)}))
                conn.execute('UPDATE sites SET indexable=? WHERE slug=?', (int(indexable), row['slug']))
    app = FastAPI(title='Wing Span Site Publishing API', version='1.0.0', description='POST HTML and optional assets to publish a durable website. Bearer authentication is required for writes. Blank hosting endpoints in the AEO/SEO agent retain default hosting.', servers=[{'url': base}])
    bearer = HTTPBearer(auto_error=False)
    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.scheme.lower() != 'bearer' or not secrets.compare_digest(credentials.credentials.encode(), key.encode()):
            raise HTTPException(401, 'A valid publishing API key is required.', headers={'WWW-Authenticate': 'Bearer'})

    @app.middleware('http')
    async def boundaries(request: Request, call_next):
        # Reject large bodies before FastAPI buffers or parses them. Chunked bodies have the same limit.
        if request.method in {'POST', 'PUT', 'PATCH'}:
            # Authenticate before buffering uploads, without cookies or browser-stored credentials.
            auth = request.headers.get('authorization', '')
            if not secrets.compare_digest(auth.encode(), ('Bearer ' + key).encode()):
                return JSONResponse({'detail': 'A valid publishing API key is required.'}, status_code=401)
            parts, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > LIMIT:
                    return JSONResponse({'detail': 'Upload exceeds 8 MiB.'}, status_code=413)
                parts.append(chunk)
            request._body = b''.join(parts)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/health', include_in_schema=False)
    def health():
        with db() as conn:
            conn.execute('SELECT 1').fetchone()
        return {'success': True, 'storage': 'persistent-sqlite'}

    @app.post('/api/sites', response_model=Publication, dependencies=[Depends(authenticate)], operation_id='publishSite')
    def publish(site: Site):
        if '<html' not in site.html.lower():
            raise HTTPException(422, 'Supply a complete HTML document.')
        raw = site.model_dump(exclude={'expected_revision'})
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        url = f'{base}/sites/{site.slug}/'
        files = {}
        for path, asset in site.assets.items():
            clean = PurePosixPath(path)
            if (not path or path != str(clean) or clean.is_absolute() or '..' in clean.parts or '\\' in path or '%' in path or '?' in path or '#' in path or any(p.startswith('.') for p in clean.parts) or path == 'index.html' or clean.suffix.lower() not in EXTENSIONS):
                raise HTTPException(422, 'Assets must use safe relative file paths.')
            try:
                files[path] = base64.b64decode(asset.content, validate=True) if asset.encoding == 'base64' else asset.content.encode()
            except (ValueError, UnicodeError):
                raise HTTPException(422, 'An asset has invalid encoding.')
        if sum(map(len, files.values())) + len(site.html.encode()) > LIMIT:
            raise HTTPException(413, 'Decoded site exceeds 8 MiB.')
        card_path, card_url = site.social_card_path, ''
        if card_path:
            card = files.get(card_path, b'')
            if not card_path.endswith('.png') or len(card) < 24 or not card.startswith(b'\x89PNG\r\n\x1a\n') or int.from_bytes(card[16:20], 'big') != 1200 or int.from_bytes(card[20:24], 'big') != 630:
                raise HTTPException(422, 'social_card_path must identify a supplied 1200x630 PNG asset.')
            card_url = url + card_path + '?v=' + hashlib.sha256(card).hexdigest()[:20]
        # Transport-owned URL metadata follows the assigned host; page content and scripts stay model-authored.
        soup = BeautifulSoup(site.html, 'html.parser')
        if soup.html is None or soup.head is None:
            raise HTTPException(422, 'The HTML document requires html and head elements.')
        for tag in soup.find_all('link', rel='canonical'):
            tag.decompose()
        canonical = soup.new_tag('link', rel='canonical', href=url)
        soup.head.append(canonical)
        for attr, name, value in [('property', 'og:url', url), ('property', 'og:image', card_url), ('name', 'twitter:image', card_url)]:
            if not value:
                continue
            for tag in soup.find_all('meta', attrs={attr: name}):
                tag.decompose()
            tag = soup.new_tag('meta', attrs={attr: name, 'content': value})
            soup.head.append(tag)
        files['index.html'] = str(soup).encode()
        sha = hashlib.sha256(files['index.html']).hexdigest()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM sites WHERE slug=? OR token=?', (site.slug, site.task_token)).fetchall()
            if existing and (len(existing) != 1 or existing[0]['slug'] != site.slug or existing[0]['token'] != site.task_token):
                raise HTTPException(409, 'This slug or task token belongs to another publication.')
            old = existing[0] if existing else None
            if old and conn.execute('SELECT 1 FROM requests WHERE slug=? AND request_hash=?', (site.slug, digest)).fetchone():
                return json.loads(old['result'])
            if old and site.expected_revision != old['revision']:
                raise HTTPException(409, 'Content changed. Supply the current expected_revision to update this publication.')
            if not old and site.expected_revision not in (None, 0):
                raise HTTPException(409, 'The expected publication does not exist.')
            revision = old['revision'] + 1 if old else 1
            result = dict(success=True, slug=site.slug, task_token=site.task_token, url=url, canonical_url=url, card_url=url, social_card_ready=bool(card_url), social_card_url=card_url, revision=revision, sha256=sha)
            now = datetime.now(timezone.utc).isoformat()
            indexable = not any('noindex' in tag.get('content', '').lower() for tag in soup.find_all('meta', attrs={'name': re.compile('^(robots|googlebot)$', re.I)}))
            conn.execute('INSERT OR REPLACE INTO sites VALUES (?,?,?,?,?,?,?)', (site.slug, site.task_token, revision, digest, json.dumps(result), now, int(indexable)))
            conn.execute('DELETE FROM files WHERE slug=?', (site.slug,))
            for path, content in files.items():
                mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
                conn.execute('INSERT INTO files VALUES (?,?,?,?)', (site.slug, path, content, mime))
            conn.execute('INSERT INTO requests VALUES (?,?)', (site.slug, digest))
        return result

    @app.get('/api/sites/{slug}', response_model=Publication, dependencies=[Depends(authenticate)], operation_id='getPublication')
    def publication(slug: str):
        with db() as conn:
            row = conn.execute('SELECT result FROM sites WHERE slug=?', (slug,)).fetchone()
        if not row:
            raise HTTPException(404, 'Site not found.')
        return json.loads(row['result'])

    @app.get('/sites/{slug}/', include_in_schema=False)
    @app.get('/sites/{slug}/{path:path}', include_in_schema=False)
    def asset(slug: str, path: str = 'index.html'):
        with db() as conn:
            row = conn.execute('SELECT content,mime FROM files WHERE slug=? AND path=?', (slug, path or 'index.html')).fetchone()
        if not row:
            raise HTTPException(404, 'Asset not found.')
        return Response(bytes(row['content']), media_type=row['mime'], headers={'Cache-Control': 'public, max-age=0, must-revalidate'})

    @app.get('/sitemap.xml', include_in_schema=False)
    def sitemap():
        with db() as conn:
            rows = conn.execute('SELECT slug,updated_at FROM sites WHERE indexable=1 ORDER BY slug').fetchall()
        items = '<url><loc>' + escape(base + '/') + '</loc></url>'
        items += ''.join('<url><loc>' + escape(f"{base}/sites/{row['slug']}/") + '</loc><lastmod>' + escape(row['updated_at']) + '</lastmod></url>' for row in rows)
        return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + items + '</urlset>', media_type='application/xml', headers={'Cache-Control': 'no-cache'})

    @app.get('/robots.txt', include_in_schema=False)
    def robots():
        return Response(f'User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {base}/sitemap.xml\n', media_type='text/plain')

    @app.get('/', include_in_schema=False)
    def home():
        return FileResponse(PUBLIC / 'index.html')

    @app.get('/{asset_name}', include_in_schema=False)
    def public_asset(asset_name: str):
        if asset_name not in {'style.css', 'app.js'}:
            raise HTTPException(404, 'Not found.')
        return FileResponse(PUBLIC / asset_name)
    return app
