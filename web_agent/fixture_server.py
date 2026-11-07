"""Local arXiv-inspired *synthetic* site, never presented as the official arXiv site.

Only used for deterministic integration tests and a known-defect demo.
"""
from __future__ import annotations

from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse, quote
import json

PAPERS = [
    {"id": "2601.00001", "title": "Evaluating GUI Agents for Software Testing", "summary": "GUI Agent testing with browser automation and evidence."},
    {"id": "2601.00002", "title": "Reliable Selenium-Based Web Interface Verification", "summary": "Selenium automation and software testing methodology."},
    {"id": "2601.00003", "title": "Web Testing with Evidence-Aware Agents", "summary": "GUI Agent evaluation, automated verification and test oracles."},
]

STYLE = """
body{font:16px system-ui,Segoe UI,Arial;background:#f7f9fd;color:#17243d;margin:0}
header{background:#16264a;color:#fff;padding:22px 6vw} header b{font-size:24px}
main{max-width:970px;margin:35px auto;padding:0 24px} .label{font-size:13px;color:#62748e}
.notice{background:#fff3d5;border:1px solid #eabf55;color:#76520a;border-radius:8px;padding:12px 16px;margin:0 0 24px}
.card{background:white;border:1px solid #dfe6f1;border-radius:12px;padding:24px;margin:17px 0;box-shadow:0 4px 18px #21365a0b}
label{display:block;font-weight:650;margin:10px 0} input,select{font-size:17px;padding:12px;border:1px solid #b9c8df;border-radius:7px;max-width:94%;min-width:240px}
button{background:#2563ad;color:#fff;font-size:17px;padding:12px 25px;border:0;border-radius:8px;cursor:pointer}
li.arxiv-result{list-style:none;border-bottom:1px solid #e5eaf2;padding:15px 0}
li.arxiv-result a{color:#1d5eaf} .title{font-size:21px;font-weight:620}
footer{font-size:13px;text-align:center;color:#6b7e9b;padding:30px}
"""


def page(body: str) -> bytes:
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Local GUI Test Fixture</title>'
            f'<style>{STYLE}</style></head><body><header><b>Research Search · Local Fixture</b>'
            f'<div>arXiv-inspired interface · Not arXiv.org</div>'
            f'<form action="/header-search" method="get"><button type="submit">Search</button></form></header><main>'
            f'<div class="notice">SYNTHETIC TEST WEBSITE — no real papers or arXiv content. For local GUI testing only.</div>'
            f'{body}</main><footer>Six-Agent Web GUI Testing Demo · fixture environment</footer></body></html>')
    return html.encode("utf-8")


def search_form(*, bug: bool = False) -> bytes:
    hidden_bug = '<input type="hidden" name="bug" value="1">' if bug else ""
    return page('''<div class="card"><h1>Advanced Search</h1>
    <form action="/search/advanced" method="get">
      {hidden_bug}
      <label for="terms-0-term">Search term</label>
      <input id="terms-0-term" name="terms-0-term" placeholder="e.g. GUI Agent" required>
      <label for="terms-0-field">Search field</label>
      <select id="terms-0-field" name="terms-0-field">
      <option value="all">All fields</option><option value="title">Title</option>
      <option value="abstract">Abstract</option><option value="author">Author</option></select>
      <label><input type="checkbox" id="classification-computer_science" name="classification-computer_science" value="y"> Computer Science</label>
      <button type="button">Add another term +</button>
      <button class="is-link">Search</button>
    </form></div>'''.replace("{hidden_bug}", hidden_bug))


def results_page(keyword: str, bad: bool, field: str) -> bytes:
    q = keyword.casefold().strip()
    matches = [p for p in PAPERS if not q or q in (p["title"] + " " + p["summary"]).casefold()]
    if field == "title":
        matches = [p for p in matches if q in p["title"].casefold()]
    records = []
    for index, p in enumerate(matches):
        # Intentional defect in the listing: detail page remains correct.
        display_title = "The Intentionally Wrong Paper Title" if bad and index == 0 else p["title"]
        records.append(f'<li class="arxiv-result"><a href="/abs/{p["id"]}?bug={int(bad)}">arXiv:{p["id"]}</a>'
                       f'<p class="title">{escape(display_title)}</p>'
                       f'<p>{escape(p["summary"])}</p></li>')
    display = "".join(records) if records else '<div id="no-results">No results found</div>'
    return page(f'<div class="card"><h1>Search results</h1><p class="label">Search: {escape(keyword)} · {len(matches)} results</p>'
                f'<ul>{display}</ul></div>')


def detail_page(paper_id: str) -> bytes:
    p = next((p for p in PAPERS if p["id"] == paper_id), None)
    if not p:
        return page('<h1>Not found</h1>')
    return page(f'<div class="card"><div class="label">arXiv:{escape(p["id"])}</div>'
                f'<h1 class="title">Title: {escape(p["title"])}</h1><p>{escape(p["summary"])}</p></div>')


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in {"/", "/search/advanced"}:
            if "terms-0-term" in query:
                payload = results_page(query["terms-0-term"][0],
                                       query.get("bug", ["0"])[0] == "1",
                                       query.get("terms-0-field", ["all"])[0])
            else:
                payload = search_form(bug=query.get("bug", ["0"])[0] == "1")
        elif parsed.path.startswith("/abs/"):
            payload = detail_page(parsed.path.rsplit("/", 1)[-1])
        else:
            payload = page('<h1>Page not found</h1>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


@contextmanager
def local_fixture(*, bug: bool = False):
    """Temporary local server; bug mode is carried in the base URL query."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # A GET form preserves this field only if a hidden input exists.
        # Pass bug mode via a header handled by the route? We instead rely on
        # the form's hidden field, injected by the special fixture instance.
        yield f"http://127.0.0.1:{server.server_port}/search/advanced" + ("?bug=1" if bug else ""), server
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)


@contextmanager
def runtime_fixture(*, seed: str = 'phase1'):
    """Private, in-memory data per session. A fresh instance is a trusted reset."""
    state = {'seed': seed, 'submissions': []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/':
                body = '<h1>Session fixture</h1><a id="next" href="/form">Open form</a>'
            elif path == '/form':
                body = '''<section id="task"><h1>Local form</h1>
                <label for="name">Name</label><input id="name">
                <button id="load" onclick="setTimeout(() => {document.querySelector('#status').textContent='Ready'; if (!document.querySelector('#continue')) {const b=document.createElement('button');b.id='continue';b.textContent='Continue';b.onclick=()=>document.querySelector('#status').textContent='Continued';document.querySelector('#task').append(b);}}, 150)">Load</button>
                <p id="status">Waiting</p><button id="menu" onclick="document.querySelector('#dialog').hidden=false">Menu</button>
                <div id="dialog" role="dialog" hidden>Options</div>
                <button id="submit" onclick="fetch('/submit', {method:'POST',body:document.querySelector('#name').value}).then(r=>r.json()).then(d=>{document.querySelector('#receipt').textContent=d.count;})">Submit</button>
                <p id="receipt">0</p></section>'''
            else:
                self.send_error(404)
                return
            payload = ('<!doctype html><title>Runtime fixture</title>' + body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != '/submit':
                self.send_error(404)
                return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 1000:
                self.send_error(413)
                return
            state['submissions'].append(self.rfile.read(size).decode('utf-8', errors='replace'))
            payload = json.dumps({'count': len(state['submissions'])}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
