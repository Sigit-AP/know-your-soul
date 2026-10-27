<div align="center">

[🇬🇧 English](README.md) | **🇮🇩 Bahasa Indonesia**

</div>

# Know Your Soul

<p align="center">
  <a href="https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml"><img src="https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"/></a>
  <a href="https://pypi.org/project/know-your-soul/"><img src="https://img.shields.io/pypi/v/know-your-soul" alt="PyPI"/></a>
  <img src="https://img.shields.io/badge/python-3.14-blue" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/cakupan-97.7%25-brightgreen" alt="Cakupan 97.7%"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/Lisensi-Apache%202.0-blue" alt="Lisensi: Apache-2.0"/></a>
</p>

Know Your Soul adalah graph pengetahuan dan pembelajaran yang deterministik
untuk [Hermes Agent](https://hermes-agent.nousresearch.com/docs). Ia menyimpan
apa yang dipelajari agent sebagai graph event-sourced yang teraudit, menilai
setiap node dengan aritmetika Decimal yang eksak, dan hanya berubah lewat
proposal yang kamu setujui. Tidak ada model, embedding, atau panggilan provider
di mana pun dalam runtime. AI-nya tetap di sisi Hermes; ini memori yang bisa
dipercaya.

<p align="center">
  <img src="docs/assets/screenshots/graph-viewer.png" alt="Graph viewer hasil ekspor sistem yang berjalan: 36 node dan 35 edge bertipe dari korpus asli, berwarna per domain P1–P7" width="900"/>
</p>
<p align="center">
  <em>Graph viewer, diekspor oleh sistem yang sedang berjalan. 36 node, 35 edge
  bertipe, berwarna per domain. Data asli, bukan mockup.</em>
</p>

## Kenapa

Agent membaca ulang dokumen yang sama di setiap sesi dan membayar harga token
penuh untuk itu. Know Your Soul memberi agent satu memori lokal sebagai
gantinya:

- Query, bukan baca ulang. Retrieval berbatas token, terukur hemat ~94%
  dibanding membaca korpus mentah (24/24 kasus benchmark beku).
- Setiap panggilan teraudit. Termasuk bacaan. Setiap operasi tercatat sebagai
  satu event dalam store SQLite append-only berantai-hash.
- Belajar itu dinilai, bukan kira-kira. Setiap node membawa skor P/M/E
  terkalibrasi di bawah kontrak matematika beku.
- Tidak ada yang menulis ulang dirinya sendiri. Perbaikan berupa proposal diff
  yang kamu setujui; rollback byte-exact.

## Mulai cepat

Butuh Python 3.14.

```bash
pip install know-your-soul
```

Dari source, dengan environment terkunci:

```bash
git clone https://github.com/Sigit-AP/know-your-soul.git
cd know-your-soul
uv sync --frozen
```

Catat dua node lalu query:

```bash
know-your-soul --store demo.sqlite3 --output-root graph log-event \
  '{"node_id":"p1-root","kind":"primary","domain":"P1",
    "summary":"P1 root: disiplin verifikasi",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

know-your-soul --store demo.sqlite3 --output-root graph log-event \
  '{"node_id":"n1","parent_id":"p1-root","domain":"P1",
    "summary":"Selalu verifikasi sebelum mengklaim selesai",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

know-your-soul --store demo.sqlite3 --output-root graph rebuild
know-your-soul --store demo.sqlite3 --output-root graph query \
  '{"purpose":"verify before claiming done","token_budget":400}'
```

Jawaban nyata dari server yang berjalan, lengkap dengan jejak auditnya:

<p align="center">
  <img src="docs/assets/screenshots/live-api-query.png" alt="Respons query langsung dari server HTTP yang berjalan: hasil, provenance, akuntansi token, dan id event audit" width="900"/>
</p>

API persisten menyajikan JSON `/v1` berversi lewat Unix socket (HTTP loopback
opsional, API key wajib di luar loopback):

```bash
know-your-soul --store demo.sqlite3 --output-root graph serve --socket demo.sock

curl -s --unix-socket demo.sock -X POST http://localhost/v1/knowledge/query \
  -H 'content-type: application/json' \
  -d '{"purpose":"verify before claiming done","token_budget":400}'
```

Hermes mendaftarkan sistem ini sebagai MCP server:

```bash
hermes mcp add know-your-soul \
  --command /root/know-your-soul/.venv/bin/python \
  --args -m know_your_soul.mcp_server \
         --graph-root /root/know-your-soul/.local/graph \
         --db /root/know-your-soul/.local/know-your-soul.sqlite3

hermes mcp test know-your-soul
```

Setup lengkap: [docs/integration/hermes.md](docs/integration/hermes.md).

## Graph viewer

`know-your-soul export --format all` menulis `graph.html` mandiri (tanpa CDN,
tanpa dependensi JavaScript, jalan offline) plus DOT, Mermaid, CSV, GraphML,
SVG, dan buku markdown per domain. Setiap artifact ter-hash ke dalam manifest
generasi dan tervalidasi round-trip.

Klik sebuah node untuk melihat skor, versi, dan provenance-nya:

<p align="center">
  <img src="docs/assets/screenshots/node-detail.png" alt="Panel detail node pada graph viewer hasil ekspor: label, id, domain, skor P/M/E, dan provenance node korpus asli" width="900"/>
</p>

Tema gelap tersedia:

<p align="center">
  <img src="docs/assets/screenshots/dark-theme.png" alt="Graph viewer yang sama dalam tema gelap" width="900"/>
</p>

## Empat belas operasi

Identik di CLI, HTTP, dan MCP. Sepuluh operasi v1 beku; kontrak v2
([docs/contracts/retrieval-verbs-v2.md](docs/contracts/retrieval-verbs-v2.md))
menambah empat verb retrieval.

| # | Operasi | Fungsi |
|---|---------|--------|
| 1 | `query` | Retrieval berbatas token dengan provenance, versi, dan rekomendasi |
| 2 | `get_node` | Satu node dengan evidence dan metadata proyeksi |
| 3 | `get_subtree` | Traversal subtree terbatas |
| 4 | `log_event` | Menambah event domain yang tak bisa diubah |
| 5 | `record_outcome` | Menempelkan hasil ke evidence |
| 6 | `evaluate` | Skor P/M/E, uncertainty, level, kelayakan promosi |
| 7 | `review` | Mesin status review: evidence minimum, cooldown, hysteresis |
| 8 | `propose_improvement` | Proposal diff terstruktur |
| 9 | `apply_improvement` | Apply operator-gated dengan gerbang regresi |
| 10 | `rollback` | Pembalikan byte-exact |
| 11 | `path` | Jalur terpendek terarah antara dua node |
| 12 | `explain` | Lingkungan relasional: link masuk/keluar plus hitungan per relasi |
| 13 | `affected` | Penelusuran dependensi terbalik, kedalaman terbatas |
| 14 | `god_nodes` | Node hub teratas berdasarkan degree terukur |

## Benchmark

`bench/` mengunci snapshot korpus dengan sha256 (28 file, 13.151 estimasi
token) dan mengajukan 24 pertanyaan beku. Tanpa model. Ledger terbaru: 24/24
diterima, rata-rata hemat token 94,2%, median latensi p50 7,9 ms.

```bash
uv run python bench/run.py check
uv run python bench/run.py run
```

Metodologi: [bench/README.md](bench/README.md). Bukti release:
[docs/verification.md](docs/verification.md).

## Keamanan

- Batas no-AI ditegakkan saat runtime: paket provider, vector, dan model
  gagal-tertutup; socket keluar ditolak; MCP server menolak start bila menemukan
  kredensial provider di environment-nya.
- Host allowlist di setiap request. API key dibandingkan dalam waktu konstan.
- Body request dibatasi. Bentuk secret dan PII email teredaksi saat tulis dan
  di setiap permukaan baca. Path terkurung ke root tetap.

## Relearn

Saat korpus berubah, sistem menerbitkan `RelearnOffer` lalu berhenti. Ia tidak
pernah memilih mode sendiri:

```bash
know-your-soul relearn-scan      # deteksi drift, catat offer
know-your-soul relearn-status    # periksa offer yang tertunda
know-your-soul relearn-preview   # dry-run rencananya
know-your-soul relearn-apply     # apply, setelah kamu setujui (RESET atau ACCUMULATE)
```

Setiap apply mengambil backup atomik terverifikasi dulu. Rollback memulihkan
hash dan skor persis seperti sebelum apply.

## Development

```bash
uv sync --frozen
uv run pytest -q                 # 2.025 test
uv run pytest -q --cov=src/know_your_soul
uv run pyright                   # 0 error
uv run ruff check
uv run python bench/run.py run   # 24/24 diterima
```

CI menjalankan gate yang sama di Ubuntu dan macOS, plus bandit, pip-audit, dan
penjaga atribusi lisensi.

## Kontribusi

Issue dan pull request diterima. Tiga aturan: jalankan seluruh gate dulu,
jangan pernah menambah dependensi model atau provider, dan jangan mengubah
perilaku kontrak beku tanpa versi kontrak baru.

## Lisensi

[Apache 2.0](LICENSE). Ide desain yang diadaptasi dari
[Graphify](https://github.com/Graphlet-AI/graphify) (Apache-2.0 ATAU MIT)
diimplementasikan ulang dari nol di bawah arsitektur proyek ini. Tidak ada
kode sumber Graphify yang disalin. Atribusi lengkap di [NOTICE](NOTICE).
