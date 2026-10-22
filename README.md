# BurpShot

Kirim raw HTTP request, render response dalam tampilan Burp Suite Repeater, simpan sebagai screenshot PNG.

![BurpShot screenshot](screenshots/burpshot-2026-10-22T13-29-11.png)

---

## Cara Kerja

```
raw HTTP request  →  kirim ke target  →  render UI Burp Suite  →  simpan PNG
```

BurpShot tersedia dalam dua mode:

| Mode | File | Cocok untuk |
|---|---|---|
| **CLI / agentic** | `burpshot.py` | scripting, pipeline, AI agent |
| **Web UI** | `server.js` + `index.html` | interaktif di browser |

---

## Instalasi

### Kebutuhan

- Python 3.8+
- Node.js 16+ (hanya untuk mode web)

### Install dependensi Python

```bash
pip install playwright
playwright install chromium
```

> Tidak ada dependensi Python lain. HTTP request dikirim dengan stdlib (`http.client`).

---

## CLI — `burpshot.py`

### Penggunaan dasar

```bash
python burpshot.py -r request.txt -t https://target.com
```

Output ke stderr (progress) dan stdout (path PNG):

```
[*] GET /api/users HTTP/1.1  →  https://target.com
[+] 200 OK  —  4.2 KB  in 312 ms
[*] Rendering screenshot...
[+] Screenshot saved: screenshots/burpshot-2026-10-22T13-29-11.png
```

### Format request file

File teks biasa, format raw HTTP request:

```http
POST /api/login HTTP/1.1
Host: target.com
Content-Type: application/json
Authorization: Bearer eyJhbGci...
Cookie: session=abc123

{"username": "admin", "password": "pass"}
```

### Semua opsi

```
usage: burpshot [-h] [-r REQUEST] -t TARGET [-o OUTPUT]
                [--print-only] [--json] [--no-screenshot]

arguments:
  -r, --request FILE    File raw HTTP request (default: stdin)
  -t, --target URL      Target base URL, contoh: https://example.com
  -o, --output FILE     Path output PNG (default: screenshots/burpshot-<ts>.png)
  --print-only          Print raw response ke stdout, skip screenshot
  --json                Output JSON ke stdout (untuk di-parse agen lain)
  --no-screenshot       Simpan HTML saja, tanpa headless browser
```

### Contoh penggunaan

**Dari file:**
```bash
python burpshot.py -r request.txt -t https://example.com
```

**Dari stdin (pipe):**
```bash
cat request.txt | python burpshot.py -t https://example.com
```

**Output PNG ke path custom:**
```bash
python burpshot.py -r request.txt -t https://example.com -o hasil/login-test.png
```

**Hanya print response (tanpa screenshot):**
```bash
python burpshot.py -r request.txt -t https://target.com --print-only
```

**Output JSON untuk diproses agen/script lain:**
```bash
python burpshot.py -r request.txt -t https://target.com --json
```

Output JSON:
```json
{
  "status_code": 200,
  "status_message": "OK",
  "headers": {
    "content-type": "application/json",
    "set-cookie": "session=xyz; HttpOnly"
  },
  "body": "{\"token\": \"eyJ...\"}",
  "bytes": 312,
  "millis": 180,
  "redirect_url": null
}
```

**Tanpa playwright (simpan HTML saja):**
```bash
python burpshot.py -r request.txt -t https://target.com --no-screenshot
# → screenshots/burpshot-<ts>.html
```

### Integrasi dengan agentic tool

BurpShot dirancang agar mudah dipanggil dari AI agent atau script otomasi:

```python
import subprocess, json

result = subprocess.run(
    ['python', 'burpshot.py', '-r', 'req.txt', '-t', 'https://target.com', '--json'],
    capture_output=True, text=True
)

data = json.loads(result.stdout)
print(data['status_code'])   # 200
print(data['body'])          # response body
# screenshot disimpan otomatis di screenshots/
```

```bash
# Shell pipeline
STATUS=$(python burpshot.py -r req.txt -t https://target.com --json | jq .status_code)
echo "Status: $STATUS"
```

---

## Web UI — `server.js`

### Jalankan server

```bash
node server.js
```

Buka `http://localhost:3000` di browser.

### Fitur

**Repeater (`/`)**

| Fitur | Detail |
|---|---|
| Request editor | Textarea editable dengan line numbers |
| Syntax highlighting | Method, header, cookie, body — tiap warna berbeda |
| Tab Pretty / Raw / Hex / Render | Render tab: render HTML response di iframe |
| Inspector | Hitung headers, cookies, query params, body params otomatis |
| Send (`Cmd+Enter`) | Kirim request nyata ke target host |
| Follow redirection | Auto-follow 3xx redirect |
| History | Navigasi request sebelumnya dengan tombol `◀` / `▶` |
| Screenshot (tombol SS) | Simpan tampilan ke `screenshots/burpshot-<ts>.png` |
| Status bar | Tampilkan `Done \| 577 B \| 1623 ms` |

**Intruder Positions (`/intruder-positions`)**

| Fitur | Detail |
|---|---|
| §-marker editor | Tambah, hapus, auto-detect payload positions |
| Add § / Clear § / Auto § | Wrap selection, strip all, atau auto-mark form values |
| Payload list | Tambah/hapus/paste/load payloads dari file |
| Attack type | Sniper, Battering ram, Pitchfork, Cluster bomb |
| Start attack | Navigasi ke halaman Intruder Attack |

**Intruder Attack (`/intruder-attack`)**

| Fitur | Detail |
|---|---|
| Results table | 20 baris sample, klik baris untuk lihat request/response |
| Column sort | Klik header kolom untuk sort ascending/descending |
| Layout toggle | Side-by-side / Stacked / Single-response pane |
| Inspector panel | Collapse/expand sections, update per-row |
| Resizable dividers | Drag untuk resize table height dan req/resp split |

### Endpoint server

| Method | Path | Fungsi |
|---|---|---|
| `GET` | `/` | Serve Repeater UI |
| `GET` | `/intruder-positions` | Serve Intruder Positions UI |
| `GET` | `/intruder-attack` | Serve Intruder Attack results UI |
| `POST` | `/send` | Proxy raw HTTP request ke target |
| `POST` | `/save-screenshot` | Terima PNG base64 dari browser, simpan ke disk |

---

## Syntax Highlighting

| Elemen | Warna |
|---|---|
| `GET` | biru `#61afef` |
| `POST` | kuning `#e5c07b` |
| `PUT` | ungu `#c678dd` |
| `DELETE` | merah `#e06c75` |
| `PATCH` | hijau `#98c379` |
| Header key | cyan `#56b6c2` |
| Cookie / body | hijau `#4ec94e` |
| Location / URL | biru link `#4a9eff` |
| Content-Type | oranye `#d19a66` |
| Status 2xx | hijau `#98c379` |
| Status 3xx | oranye `#d19a66` |
| Status 4xx | kuning `#e5c07b` |
| Status 5xx | merah `#e06c75` |

---

## Struktur Project

```
burpshot/
├── burpshot.py              # CLI tool (Python, standalone)
├── server.js                # Web server (Node.js)
├── index.html               # Web UI: Burp Repeater clone
├── intruder-positions.html  # Web UI: Burp Intruder Positions clone
├── intruder-attack.html     # Web UI: Burp Intruder Attack results clone
├── screenshots/             # Output PNG tersimpan di sini
└── design_ref/              # File desain referensi asli (tidak dicommit)
```

---

## Troubleshooting

**`playwright not found`**
```bash
pip install playwright && playwright install chromium
```

**Request timeout**
- Default timeout 30 detik
- Cek koneksi ke target dan pastikan host di `-t` benar

**Response tidak terbaca / encoding error**
- BurpShot otomatis decompress gzip, deflate
- Untuk binary response, gunakan `--no-screenshot` dan buka HTML-nya manual

**Screenshot kosong / font tidak render**
- Butuh koneksi internet untuk load Google Fonts (JetBrains Mono)
- Tanpa internet, fallback ke Consolas/monospace — tampilan tetap berfungsi
