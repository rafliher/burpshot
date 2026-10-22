#!/usr/bin/env python3
"""
burpshot.py — Send raw HTTP request, render response in Burp Suite UI, save screenshot.

Usage:
  python burpshot.py -r request.txt -t https://example.com
  python burpshot.py -r request.txt -t https://example.com -o output.png
  cat request.txt | python burpshot.py -t https://example.com
  python burpshot.py -r request.txt -t https://example.com --print-only
  python burpshot.py -r request.txt -t https://example.com --json

Requirements:
  pip install playwright && playwright install chromium
"""

import argparse
import gzip
import http.client
import json as _json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.parse
import zlib
from datetime import datetime
from pathlib import Path


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def parse_raw_request(raw: str):
    text = raw.replace('\r\n', '\n').replace('\r', '\n')
    blank = text.find('\n\n')
    header_section = text[:blank] if blank >= 0 else text
    body = text[blank + 2:] if blank >= 0 else ''
    lines = header_section.split('\n')
    parts = lines[0].strip().split()
    method = parts[0] if parts else 'GET'
    path = parts[1] if len(parts) > 1 else '/'
    headers: dict[str, str] = {}
    for line in lines[1:]:
        colon = line.find(':')
        if colon > 0:
            k = line[:colon].strip().lower()
            v = line[colon + 1:].strip()
            headers[k] = v
    return method, path, headers, body


def send_http_request(host: str, raw_request: str) -> dict:
    method, path, headers, body = parse_raw_request(raw_request)

    parsed_host = urllib.parse.urlparse(host)
    is_https = parsed_host.scheme == 'https'
    hostname = parsed_host.hostname
    port = parsed_host.port or (443 if is_https else 80)

    # Strip accept-encoding so we always get plain text back
    headers.pop('accept-encoding', None)
    headers['host'] = hostname + (f':{port}' if parsed_host.port else '')

    if is_https:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(hostname, port, context=ctx, timeout=30)
    else:
        conn = http.client.HTTPConnection(hostname, port, timeout=30)

    # Resolve full path
    if path.startswith('http://') or path.startswith('https://'):
        p = urllib.parse.urlparse(path)
        full_path = (p.path or '/') + (f'?{p.query}' if p.query else '')
    else:
        full_path = path

    t0 = time.time()
    conn.request(method, full_path, body=body.encode() if body else None, headers=headers)
    resp = conn.getresponse()
    raw_body = resp.read()
    elapsed_ms = int((time.time() - t0) * 1000)

    # Decompress
    ce = (resp.getheader('content-encoding') or '').lower().strip()
    try:
        if ce == 'gzip':
            raw_body = gzip.decompress(raw_body)
        elif ce == 'deflate':
            raw_body = zlib.decompress(raw_body)
    except Exception:
        pass

    body_str = raw_body.decode('utf-8', errors='replace')
    resp_headers = dict(resp.getheaders())

    header_lines = [f'HTTP/1.1 {resp.status} {resp.reason}']
    for k, v in resp.getheaders():
        header_lines.append(f'{k}: {v}')
    raw_response = '\n'.join(header_lines) + '\n\n' + body_str

    return {
        'status_code': resp.status,
        'status_message': resp.reason,
        'headers': resp_headers,
        'body': body_str,
        'raw_response': raw_response,
        'bytes': len(raw_body),
        'millis': elapsed_ms,
        'redirect_url': resp.getheader('location'),
    }


# ─── Syntax highlighting (Python → HTML spans) ─────────────────────────────────

def esc(s: str) -> str:
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


_METHOD_COLORS = {
    'GET': '#61afef', 'POST': '#e5c07b', 'PUT': '#c678dd',
    'DELETE': '#e06c75', 'PATCH': '#98c379', 'HEAD': '#56b6c2',
    'OPTIONS': '#d19a66', 'TRACE': '#56b6c2',
}


def _hl_req_line(line: str, idx: int) -> str:
    if idx == 0:
        m = re.match(r'^(\S+)\s+(\S*)\s*(.*)', line)
        if m:
            mc = _METHOD_COLORS.get(m.group(1), '#569cd6')
            return (f'<span style="color:{mc};font-weight:600">{esc(m.group(1))}</span> '
                    f'<span style="color:#d4d4d4">{esc(m.group(2))}</span> '
                    f'<span style="color:#888">{esc(m.group(3))}</span>')
        return f'<span style="color:#569cd6">{esc(line)}</span>'

    if not line:
        return ''
    colon = line.find(':')
    if colon > 0 and line[0] not in ' \t':
        key, val = line[:colon], line[colon + 1:].lstrip()
        vc = '#4ec94e' if key.lower() == 'cookie' else '#d4d4d4'
        return (f'<span style="color:#56b6c2">{esc(key)}:</span> '
                f'<span style="color:{vc}">{esc(val)}</span>')
    return f'<span style="color:#4ec94e">{esc(line)}</span>'


def _hl_resp_line(line: str, idx: int) -> str:
    if idx == 0:
        m = re.match(r'^(HTTP/[\d.]+)\s+(\d+)\s*(.*)', line)
        if m:
            sc = int(m.group(2))
            c = ('#e06c75' if sc >= 500 else '#e5c07b' if sc >= 400
                 else '#d19a66' if sc >= 300 else '#98c379' if sc >= 200
                 else '#d4d4d4')
            return (f'<span style="color:#888">{esc(m.group(1))}</span> '
                    f'<span style="color:{c};font-weight:600">{esc(m.group(2))}</span> '
                    f'<span style="color:#e5c07b">{esc(m.group(3))}</span>')
        return f'<span style="color:#e5c07b">{esc(line)}</span>'

    if not line:
        return ''
    colon = line.find(':')
    if colon > 0 and line[0] not in ' \t':
        key, val = line[:colon], line[colon + 1:].lstrip()
        kl = key.lower()
        vc = ('#4ec94e' if kl in ('set-cookie', 'cookie')
              else '#4a9eff' if kl in ('location', 'referer', 'origin')
              else '#d19a66' if kl == 'content-type'
              else '#d4d4d4')
        return (f'<span style="color:#56b6c2">{esc(key)}:</span> '
                f'<span style="color:{vc}">{esc(val)}</span>')
    return f'<span style="color:#4ec94e">{esc(line)}</span>'


def render_code_lines(text: str, is_response: bool = False) -> str:
    lines = text.split('\n')
    in_body = False
    parts = []
    for i, line in enumerate(lines):
        if not in_body and i > 0 and line == '':
            in_body = True
        if in_body and i > 0:
            hl = f'<span style="color:#4ec94e">{esc(line)}</span>'
        elif is_response:
            hl = _hl_resp_line(line, i)
        else:
            hl = _hl_req_line(line, i)
        parts.append(
            f'<div class="cl"><span class="ln">{i + 1}</span>'
            f'<span class="lc">{hl}</span></div>'
        )
    return '\n'.join(parts)


# ─── Inspector data ────────────────────────────────────────────────────────────

def _inspector_data(request_text: str, resp: dict) -> list[tuple[str, int]]:
    lines = request_text.replace('\r', '').split('\n')
    req_headers: dict[str, str] = {}
    body_start = -1
    for i, l in enumerate(lines[1:], 1):
        if l == '':
            body_start = i + 1
            break
        c = l.find(':')
        if c > 0:
            req_headers[l[:c].strip().lower()] = l[c + 1:].strip()

    path_val = lines[0].split()[1] if len(lines[0].split()) > 1 else '/'
    try:
        from urllib.parse import urlparse, parse_qs
        q_count = len(parse_qs(urlparse(path_val).query))
    except Exception:
        q_count = 0

    body_text = '\n'.join(lines[body_start:]).strip() if body_start >= 0 else ''
    ct = req_headers.get('content-type', '')
    b_count = 0
    if body_text:
        if 'x-www-form-urlencoded' in ct:
            b_count = len([p for p in body_text.split('&') if '=' in p])
        elif 'application/json' in ct:
            try:
                b_count = len(_json.loads(body_text))
            except Exception:
                b_count = 1

    cookie_val = req_headers.get('cookie', '')
    c_count = len([c for c in cookie_val.split(';') if c.strip()]) if cookie_val else 0

    return [
        ('Request attributes', 3 if lines and lines[0].strip() else 0),
        ('Request query parameters', q_count),
        ('Request body parameters', b_count),
        ('Request cookies', c_count),
        ('Request headers', len(req_headers)),
        ('Response headers', len(resp.get('headers', {}))),
    ]


# ─── HTML template ─────────────────────────────────────────────────────────────

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1e1e1e;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#d4d4d4;font-size:12px}
.w{--bg2:#252526;--bg3:#2d2d2d;--bg4:#333;--bd:#3a3a3a;--bd2:#454545;--or:#ff6633;--t2:#888;--t3:#666;--mono:'JetBrains Mono',Consolas,monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden;background:#1e1e1e}
.ib{background:none;border:none;color:var(--t2);padding:4px 5px;border-radius:3px;display:flex;align-items:center;justify-content:center}
.nav-item{padding:0 4px;height:38px;display:flex;align-items:center;font-size:11px;flex-shrink:0;color:var(--t2);white-space:nowrap;border-bottom:2px solid transparent}
.nav-item.a{color:var(--or);border-bottom-color:var(--or)}
.bt{background:var(--bg3);border:1px solid var(--bd2);color:var(--t2);padding:4px 10px;border-radius:3px;font-size:11px;display:flex;align-items:center;gap:5px;height:26px}
.pn{display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;overflow:hidden}
.ph{background:var(--bg3);border-bottom:1px solid var(--bd);display:flex;align-items:center;flex-shrink:0;height:26px}
.pt{padding:0 10px;font-size:12px;font-weight:600;flex:1}
.sub{background:var(--bg2);border-bottom:1px solid var(--bd);display:flex;align-items:center;padding:0 6px;flex-shrink:0;height:26px}
.pk{padding:0 9px;height:26px;display:flex;align-items:center;font-size:11px;color:var(--t2);border-bottom:2px solid transparent}
.pk.a{color:#d4d4d4;border-bottom-color:var(--or)}
.code{flex:1;overflow:hidden;font-family:var(--mono);font-size:11px;line-height:1.65;padding:4px 0;background:#1a1a1a}
.cl{display:flex;padding:0 8px;min-height:18px}
.ln{color:#444;min-width:26px;text-align:right;margin-right:12px;flex-shrink:0;user-select:none}
.lc{flex:1;word-break:break-all}
.sb{border-top:1px solid var(--bd);padding:3px 8px;display:flex;align-items:center;gap:8px;background:var(--bg3);flex-shrink:0;color:var(--t2);font-size:11px}
.rt{flex-shrink:0;display:flex;flex-direction:column;align-items:center;gap:4px;padding:4px 0;color:var(--t2);width:100%}
.rt.a{color:#d4d4d4;background:var(--bg4)}
.rt span{writing-mode:vertical-rl;font-size:10px;letter-spacing:.2px;white-space:nowrap}
.isec{border-bottom:1px solid var(--bd);display:flex;align-items:center;padding:7px 10px;font-size:11px}
.isec b{flex:1;font-weight:400}
.cnt{background:var(--bg4);color:var(--t2);padding:1px 7px;border-radius:10px;font-size:10px;margin-right:6px}
.fb{display:flex;align-items:center;gap:5px;color:var(--t2);font-size:10.5px;padding:2px 6px;border-radius:3px}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-thumb{background:#444;border-radius:3px}
"""


def generate_html(request_text: str, resp: dict, host: str) -> str:
    req_html = render_code_lines(request_text, is_response=False)
    resp_html = render_code_lines(resp['raw_response'], is_response=True)

    sc = resp['status_code']
    sc_color = ('#e06c75' if sc >= 500 else '#e5c07b' if sc >= 400
                else '#d19a66' if sc >= 300 else '#98c379' if sc >= 200
                else '#d4d4d4')

    insp_rows = _inspector_data(request_text, resp)
    insp_html = '\n'.join(
        f'<div class="isec"><b>{name}</b>'
        f'<span class="cnt">{count}</span>'
        f'<svg width="8" height="8" viewBox="0 0 8 8" fill="#888"><path d="M2 1l4 3-4 3z"/></svg>'
        f'</div>'
        for name, count in insp_rows
    )

    def _fmt(n: int) -> str:
        if n < 1024: return f'{n} B'
        if n < 1048576: return f'{n/1024:.1f} KB'
        return f'{n/1048576:.2f} MB'

    nav_items = ['Dashboard','Target','Proxy','Intruder','Repeater','Collaborator',
                 'Sequencer','Decoder','Comparer','Logger','Organizer','Extensions','Discover']
    nav_html = ''.join(
        f'<div class="nav-item{"  a" if n == "Repeater" else ""}">{n}'
        + (' <span style="display:inline-block;width:5px;height:5px;background:#e05;border-radius:50%;margin-left:4px"></span>' if n == 'Discover' else '')
        + '</div>'
        for n in nav_items
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_CSS}</style>
</head>
<body>
<div class="w">

  <!-- Title bar -->
  <div style="background:#2b2b2b;height:28px;display:flex;align-items:center;padding:0 14px;flex-shrink:0;position:relative;border-bottom:1px solid #1a1a1a">
    <div style="display:flex;gap:8px">
      <span style="width:12px;height:12px;border-radius:50%;background:#ff5f57"></span>
      <span style="width:12px;height:12px;border-radius:50%;background:#febc2e"></span>
      <span style="width:12px;height:12px;border-radius:50%;background:#28c840"></span>
    </div>
    <div style="position:absolute;left:0;right:0;text-align:center;font-size:12px;font-weight:500;color:#9a9a9a">Burp Suite Professional v2026.9.2 - Temporary Project - licensed to BSSN</div>
  </div>

  <!-- Nav bar -->
  <div style="background:#1a1a1a;border-bottom:1px solid #111;display:flex;align-items:center;padding:0 8px;height:38px;flex-shrink:0;overflow:hidden">
    {nav_html}
    <div style="flex:1"></div>
    <span style="background:var(--or);color:#fff;padding:3px 8px 3px 6px;border-radius:3px;font-size:10px;font-weight:700;display:flex;align-items:center;gap:4px;flex-shrink:0;white-space:nowrap">
      <svg width="9" height="11" viewBox="0 0 11 14" fill="#fff"><path d="M6.5 0L0 8h5L4.5 14 11 6H6L6.5 0z"/></svg>Burp AT
    </span>
  </div>

  <!-- Tab bar -->
  <div style="background:var(--bg2);border-bottom:1px solid var(--bd);display:flex;align-items:center;padding:0 12px 0 8px;height:34px;gap:2px;flex-shrink:0">
    <div style="background:var(--bg3);border:1px solid var(--bd2);padding:4px 12px;font-size:11px;border-radius:3px 3px 0 0;display:flex;gap:6px;align-items:center">1 <span style="color:var(--t3)">&#215;</span></div>
    <button class="ib" style="font-size:16px;padding:0 4px">+</button>
  </div>

  <!-- Toolbar -->
  <div style="background:var(--bg2);border-bottom:1px solid var(--bd);padding:5px 10px;display:flex;align-items:center;gap:6px;flex-shrink:0">
    <button style="background:var(--or);border:none;color:#fff;padding:0 14px;height:26px;border-radius:3px;font-size:12px;font-weight:600">Send</button>
    <div class="bt">Cancel</div>
    <div class="bt" style="padding:0 6px;gap:3px">
      <svg width="10" height="10" viewBox="0 0 9 10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 1L2 5l5 4"/></svg>
      <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor"><path d="M1 2.5h6L4 6z"/></svg>
    </div>
    <div class="bt" style="padding:0 6px;gap:3px">
      <svg width="10" height="10" viewBox="0 0 9 10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2 1l5 4-5 4"/></svg>
      <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor"><path d="M1 2.5h6L4 6z"/></svg>
    </div>
    <div class="bt">Follow redirection</div>
    <button style="background:linear-gradient(135deg,#7c3aed,#a855f7);border:none;color:#fff;padding:0 12px;height:26px;border-radius:3px;font-size:11px;font-weight:600;display:flex;align-items:center;gap:5px">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M9 3l1.8 5.2L16 10l-5.2 1.8L9 17l-1.8-5.2L2 10l5.2-1.8z"/><path d="M18 11l.9 2.6 2.6.9-2.6.9L18 18l-.9-2.6-2.6-.9 2.6-.9z"/><path d="M17.5 2l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9-1.9-.6 1.9-.6z"/></svg>
      Burp AI
    </button>
    <div style="flex:1"></div>
    <span style="color:var(--t2);font-size:11px">Target:</span>
    <span style="font-family:var(--mono);font-size:11px">{esc(host)}</span>
    <div class="bt" style="height:22px;padding:0 8px;border-radius:2px">HTTP/1 &#9662;</div>
    <div class="bt" style="height:22px;padding:0 6px;border-radius:2px">?</div>
  </div>

  <!-- Main content -->
  <div style="display:flex;flex:1;overflow:hidden;min-height:0">

    <!-- Request pane -->
    <div class="pn" style="border-right:1px solid var(--bd)">
      <div class="ph"><div class="pt">Request</div></div>
      <div class="sub">
        <div class="pk a">Pretty</div><div class="pk">Raw</div><div class="pk">Hex</div>
        <div style="flex:1"></div>
      </div>
      <div class="code">{req_html}</div>
      <div class="sb">
        <span>?</span>
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.2"><circle cx="4.5" cy="4.5" r="3.5"/><line x1="7.5" y1="7.5" x2="10" y2="10"/></svg>
        <span>&#8592;</span><span>&#8594;</span>
        <span style="flex:1;background:var(--bg2);border:1px solid var(--bd);padding:2px 6px;border-radius:2px;color:var(--t3)">Search</span>
        <span style="font-size:10px;color:var(--t3)">0 highlights</span>
      </div>
    </div>

    <!-- Response pane -->
    <div class="pn">
      <div class="ph">
        <div class="pt">Response</div>
        <div style="display:flex;gap:2px;padding:0 6px">
          <div class="ib" style="color:#d4d4d4;background:var(--bg4)">
            <svg width="15" height="13" viewBox="0 0 15 13" fill="none" stroke="currentColor" stroke-width="1.2"><rect x="1" y="1.5" width="13" height="10" rx="1"/><line x1="7.5" y1="1.5" x2="7.5" y2="11.5"/></svg>
          </div>
          <div class="ib"><svg width="15" height="13" viewBox="0 0 15 13" fill="none" stroke="currentColor" stroke-width="1.2"><rect x="1" y="1.5" width="13" height="10" rx="1"/><line x1="1" y1="6.5" x2="14" y2="6.5"/></svg></div>
          <div class="ib"><svg width="15" height="13" viewBox="0 0 15 13" fill="none" stroke="currentColor" stroke-width="1.2"><rect x="1" y="1.5" width="13" height="10" rx="1"/></svg></div>
        </div>
      </div>
      <div class="sub">
        <div class="pk a">Pretty</div><div class="pk">Raw</div><div class="pk">Hex</div><div class="pk">Render</div>
        <div style="flex:1"></div>
      </div>
      <div class="code">{resp_html}</div>
      <div class="sb">
        <span>?</span>
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.2"><circle cx="4.5" cy="4.5" r="3.5"/><line x1="7.5" y1="7.5" x2="10" y2="10"/></svg>
        <span>&#8592;</span><span>&#8594;</span>
        <span style="flex:1;background:var(--bg2);border:1px solid var(--bd);padding:2px 6px;border-radius:2px;color:var(--t3)">Search</span>
        <span style="font-size:10px;color:var(--t3)">0 highlights</span>
      </div>
    </div>

    <!-- Inspector -->
    <div style="width:280px;flex-shrink:0;display:flex;flex-direction:column;background:var(--bg2);border-left:1px solid var(--bd)">
      <div class="ph" style="padding:0 10px;height:26px">
        <span class="pt" style="padding:0">Inspector</span>
        <div class="ib" style="padding:2px 4px"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.2"><rect x="1" y="1.5" width="10" height="9" rx="1"/><line x1="7" y1="1.5" x2="7" y2="10.5"/></svg></div>
        <div class="ib" style="padding:2px 4px"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"><path d="M1.5 3h9M1.5 6h9M1.5 9h9"/></svg></div>
        <div class="ib" style="padding:2px 4px"><svg width="10" height="10" viewBox="0 0 10 10" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"><path d="M1.5 1.5l7 7M8.5 1.5l-7 7"/></svg></div>
      </div>
      <div style="flex:1;overflow:hidden">{insp_html}</div>
    </div>

    <!-- Side tabs -->
    <div style="width:30px;background:var(--bg3);border-left:1px solid var(--bd);display:flex;flex-direction:column;align-items:center;flex-shrink:0;overflow:hidden">
      <div class="rt a">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M2 4h12M2 8h12M2 12h12"/><circle cx="5" cy="4" r="1.6" fill="#2d2d2d"/><circle cx="11" cy="8" r="1.6" fill="#2d2d2d"/><circle cx="7" cy="12" r="1.6" fill="#2d2d2d"/></svg>
        <span>Inspector</span>
      </div>
      <div class="rt">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 1.8h7.5L13 4.3V14.2H3z"/><path d="M5.5 7h5M5.5 9.5h5M5.5 12h3"/></svg>
        <span>Notes</span>
      </div>
      <div class="rt">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M6.2 6.3a1.9 1.9 0 113 1.5c-.7.5-1.2.9-1.2 1.7"/><circle cx="8" cy="11.5" r=".5" fill="currentColor"/></svg>
        <span>Explanations</span>
      </div>
      <div class="rt">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 1.5L3 9h4.5L7 14.5 13 7H8.5z"/></svg>
        <span>Custom actions</span>
      </div>
      <div style="flex:1"></div>
      <div class="rt" style="padding:6px 0">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l-.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33h0a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51h0a1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v0a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
      </div>
    </div>

  </div>

  <!-- Status bar -->
  <div style="background:var(--bg3);border-top:1px solid var(--bd);padding:3px 12px;display:flex;align-items:center;gap:8px;font-size:10px;color:var(--t2);flex-shrink:0">
    <span style="width:6px;height:6px;border-radius:50%;background:{sc_color};display:inline-block;flex-shrink:0"></span>
    <span style="color:#d4d4d4;font-weight:500">Done</span>
    <div style="flex:1"></div>
    <span>{_fmt(resp['bytes'])} | {resp['millis']} ms</span>
  </div>

  <!-- Footer -->
  <div style="background:#1a1a1a;border-top:1px solid #111;padding:3px 10px;display:flex;align-items:center;gap:6px;flex-shrink:0;height:26px">
    <div class="fb">Event log</div>
    <div class="fb">All issues</div>
    <div style="flex:1"></div>
    <div class="fb">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="8" cy="8" r="6.2"/><path d="M8 7.2v4.2"/><circle cx="8" cy="4.9" r=".4" fill="currentColor"/></svg>
    </div>
    <div class="fb">Memory: 0MB</div>
    <div style="width:60px;height:6px;background:#333;border-radius:2px;overflow:hidden"><div style="width:5%;height:100%;background:#4a9eff"></div></div>
    <div class="fb" style="gap:3px;color:#a78bfa">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M9 3l1.8 5.2L16 10l-5.2 1.8L9 17l-1.8-5.2L2 10l5.2-1.8z"/><path d="M18 11l.9 2.6 2.6.9-2.6.9L18 18l-.9-2.6-2.6-.9 2.6-.9z"/><path d="M17.5 2l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9-1.9-.6 1.9-.6z"/></svg>
      <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor"><path d="M1 2.5h6L4 6z"/></svg>
    </div>
  </div>

</div>
</body>
</html>"""


# ─── Screenshot via Playwright ─────────────────────────────────────────────────

def take_screenshot(html_content: str, output_path: str) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('[!] playwright not found. Install: pip install playwright && playwright install chromium',
              file=sys.stderr)
        return False

    with tempfile.NamedTemporaryFile(suffix='.html', mode='w', delete=False, encoding='utf-8') as f:
        f.write(html_content)
        tmp = f.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 840})
            page.goto(f'file://{os.path.abspath(tmp)}')
            page.wait_for_timeout(800)  # fonts + render settle
            page.screenshot(path=output_path, full_page=False)
            browser.close()
        return True
    finally:
        os.unlink(tmp)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='burpshot',
        description='Send raw HTTP request, render in Burp Suite UI, save screenshot.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python burpshot.py -r request.txt -t https://example.com
  python burpshot.py -r request.txt -t https://example.com -o result.png
  cat request.txt | python burpshot.py -t https://example.com
  python burpshot.py -r request.txt -t https://example.com --print-only
  python burpshot.py -r request.txt -t https://example.com --json""",
    )
    parser.add_argument('-r', '--request',
                        help='Raw HTTP request file (omit to read from stdin)')
    parser.add_argument('-t', '--target', required=True,
                        help='Target base URL, e.g. https://example.com')
    parser.add_argument('-o', '--output',
                        help='Output PNG path (default: screenshots/burpshot-<ts>.png)')
    parser.add_argument('--print-only', action='store_true',
                        help='Print raw response to stdout, skip screenshot')
    parser.add_argument('--json', action='store_true',
                        help='Output JSON {status_code, headers, body, bytes, millis} to stdout')
    parser.add_argument('--no-screenshot', action='store_true',
                        help='Save HTML only (no headless browser needed)')
    args = parser.parse_args()

    # ── read request ──────────────────────────────────────────────────────────
    if args.request:
        raw = Path(args.request).read_text(encoding='utf-8', errors='replace')
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        parser.error('Provide -r/--request FILE or pipe request via stdin.')

    raw = raw.strip()
    if not raw:
        sys.exit('[!] Empty request.')

    # ── send ──────────────────────────────────────────────────────────────────
    print(f'[*] {raw.splitlines()[0]}  →  {args.target}', file=sys.stderr)
    try:
        resp = send_http_request(args.target, raw)
    except Exception as exc:
        sys.exit(f'[!] Request failed: {exc}')

    status_line = f'{resp["status_code"]} {resp["status_message"]}'
    size_line   = (f'{resp["bytes"]} B' if resp["bytes"] < 1024
                   else f'{resp["bytes"]/1024:.1f} KB')
    print(f'[+] {status_line}  —  {size_line}  in {resp["millis"]} ms', file=sys.stderr)

    # ── output modes ──────────────────────────────────────────────────────────
    if args.print_only:
        print(resp['raw_response'])
        return

    if args.json:
        print(_json.dumps({
            'status_code':    resp['status_code'],
            'status_message': resp['status_message'],
            'headers':        resp['headers'],
            'body':           resp['body'],
            'bytes':          resp['bytes'],
            'millis':         resp['millis'],
            'redirect_url':   resp['redirect_url'],
        }, ensure_ascii=False))
        return

    # ── screenshot ────────────────────────────────────────────────────────────
    html = generate_html(raw, resp, args.target)

    if args.output:
        out = args.output
    else:
        os.makedirs('screenshots', exist_ok=True)
        ts  = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
        ext = '.html' if args.no_screenshot else '.png'
        out = f'screenshots/burpshot-{ts}{ext}'

    if args.no_screenshot:
        Path(out).write_text(html, encoding='utf-8')
        print(f'[+] HTML saved: {out}')
    else:
        print('[*] Rendering screenshot...', file=sys.stderr)
        if take_screenshot(html, out):
            print(f'[+] Screenshot saved: {out}')
        else:
            sys.exit(1)


if __name__ == '__main__':
    main()
