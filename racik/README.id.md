# Racik

Perencana menu berbasis AKG dengan batasan biaya untuk program **Makan Bergizi
Gratis** (MBG) Indonesia.

Racik mengambil 18.133 resep daerah, mengubah baris bahan dalam ukuran rumah
tangga menjadi gram, menghitung gizi dan biayanya terhadap tabel komposisi
pangan resmi, lalu memilih menu beberapa hari dengan pemrograman bilangan bulat
di bawah batas bawah AKG 2019, pagu per porsi MBG, dan komposisi nampan
"Isi Piringku".

> 🇬🇧 English version: [README.md](README.md)

```bash
pip install -r requirements.txt
python scripts/build_db.py                      # ~30 detik, membuat data/racik.db
export SEALION_API_KEY=...                      # opsional; mengaktifkan orkestrator
python -m uvicorn racik.api:app --port 8000     # buka http://127.0.0.1:8000
```

Antarmuka tersedia dalam Bahasa Indonesia dan Inggris — gunakan tombol ID/EN di
kanan atas. Build ini default ke **Bahasa Indonesia**.

---

## Arsitektur

![Arsitektur Racik](docs/architecture.svg)

Disajikan di `/architecture.svg` saat aplikasi berjalan; sumber di
[`docs/architecture.svg`](docs/architecture.svg).

## Alur kerja

```
Indonesian_Recipes_HF_Regional.xlsx   TKPI_2020_Table_4_All_Data.csv   prices_id.json
        18.133 resep                      1.146 pangan + BDD              Rp/kg
              │                                   │                         │
              ▼                                   │                         │
      racik/urt.py                                │                         │
      URT → gram                                  │                         │
      "2 ons ikan" → 200 g                        │                         │
      "1 papan tempe" → 200 g                     │                         │
      "secukupnya garam" → 2 g (ditandai)         │                         │
              │                                   │                         │
              ▼                                   ▼                         │
      racik/tkpi.py ── pencocokan token ──→ komposisi + BDD                 │
              │                                                             │
              ▼                                                             ▼
      racik/nutrition.py — gram bersih × per 100 g │ gram kotor × Rp/kg ────┘
      faktor retensi · penyerapan minyak · perkiraan jumlah porsi
              │
              ▼
      data/racik.db     18.008 hidangan berbiaya, 258rb baris bahan teraudit
              │
              ▼
      racik/optimizer.py — CP-SAT
      batas AKG (lunak) · pagu (lunak) · komposisi nampan · variasi
              │
              ▼
      racik/api.py + ui/index.html — menu, kepatuhan, belanja, CSV
```

## Orkestrator SEA-LION

[SEA-LION v4.5](https://sea-lion.ai/) (AI Singapore, Mei 2026) adalah lapisan
bahasa alami. Dipilih di atas model umum karena dua alasan: model ini dilatih
untuk bahasa-bahasa Asia Tenggara, sehingga operator SPPG dapat menulis dalam
Bahasa Indonesia biasa, dan model ini disetel untuk *function-calling* presisi
serta penggunaan alat secara agentik.

**Model mengorkestrasi; model tidak pernah menghitung.** Setiap angka gram,
rupiah, kalori, dan persentase pemenuhan dihasilkan oleh mesin deterministik
lalu diserahkan ke model sebagai keluaran alat. Prompt sistem melarang model
mengarang angka, dan setiap jawaban membawa jejak alat yang menghasilkannya,
sehingga tiap klaim dapat diperiksa. Model bahasa yang diam-diam membulatkan
angka protein akan menghancurkan satu-satunya sifat yang membuat Racik layak
dipakai.

| Alat | Mengembalikan |
|---|---|
| `plan_menu` | Menu teroptimasi, biaya & pemenuhan per hari, batas yang dilonggarkan |
| `budget_sensitivity` | Pemenuhan dan biaya pada beberapa pagu bahan |
| `search_dishes` | Kandidat hidangan menurut slot, provinsi, biaya, protein |
| `get_dish` | Gizi per porsi, biaya, bahan lengkap dengan asal angka gram |
| `procurement_list` | Daftar belanja teragregasi sesuai jumlah porsi |
| `nutrition_reference` | Target AKG per sekali makan; wajib vs informasi |
| `translate_dishes` | Nama hidangan diterjemahkan ke Inggris, dengan cache |
| `explain_candidates` | Hidangan mana yang memenuhi syarat, dan gerbang mana yang menyingkirkan sisanya |

**Dibatasi di kode, bukan di prompt.** Batas langkah keras (6) ditambah deteksi
pemanggilan berulang mengakhiri loop; kondisi berhenti yang hanya hidup di
prompt adalah kondisi yang suatu saat tidak berhenti. Kegagalan — alat tidak
dikenal, argumen salah, pengulangan liar — dicatat di jejak, bukan ditelan.

**Menurun, bukan gagal.** Tanpa `SEALION_API_KEY`, atau bila API tidak dapat
dihubungi, parser maksud berbasis aturan menjalankan alat yang sama dan
mengembalikan angka yang sama. Lebih sempit, tetap tepercaya, tidak pernah
halaman kosong.

```bash
curl -s localhost:8000/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"Menu 5 hari untuk SMP di Jawa Tengah, 150 porsi, tanpa udang","lang":"id"}'
```

## Mengapa sebuah hidangan dipilih atau tidak

Penyaringan awal terdiri dari dua tahap, dan `POST /api/candidates`
mengembalikan keduanya beserta dampaknya — pertanyaan "mengapa hidangan ini
tidak pernah dipertimbangkan?" punya jawaban.

**Gerbang keras.** Hidangan yang gagal di salah satunya tidak memenuhi syarat:

| Gerbang | Aturan |
|---|---|
| G1 slot | harus mengisi slot nampan yang sedang diisi |
| G2 sumber | `staple` dan `buah` hanya menerima komponen sajian, bukan resep korpus |
| G3 pantangan | istilah dicocokkan ke nama hidangan **dan** seluruh indeks bahan |
| G4 id hidangan | veto eksplisit operator |
| G5 plafon biaya | biaya per porsi <= 85% pagu bahan |

**Skor.** Yang lolos diberi peringkat, dideduplikasi menurut nama, lalu 220
teratas per slot masuk ke solver:

```
nilai     = min(protein/target, 1,5) + min(energi/target, 1,5)
efisiensi = nilai / (biaya/pagu + 0,15)
skor      = efisiensi + 3,0 x kecocokan_daerah + 2,0 x mutu_data
```

Kecocokan daerah: 1,0 provinsi tepat, 0,6 sepulau, 0,5 nasional, 0,2 lainnya.
`rank_breakdown()` mengembalikan tiap suku sehingga totalnya dapat diperiksa.

Corong ini membuat cerita pagu terlihat. Untuk SD di Jawa Tengah, **plafon biaya
saja menyingkirkan 9.084 dari 13.525 hidangan lauk hewani** — dua pertiga korpus
tersingkir karena harga sebelum solver berjalan.

## Laporan kepatuhan & kebutuhan bahan

`POST /api/report` mengembalikan satu halaman siap cetak yang dapat diserahkan
SPPG kepada penyelia atau auditor: menu, kepatuhan AKG per hari, setiap batas
yang dilonggarkan, daftar belanja teragregasi dalam kilogram kotor, catatan
metodologi, dan kolom tanda tangan. Berkas HTML mandiri dengan gaya cetak —
dapat dicetak ke PDF dari peramban mana pun, tanpa dependensi tambahan.
Dwibahasa, dan mikronutrien informatif ditandai `INFO` di setiap tabel.

## Terjemahan nama hidangan

`POST /api/translate` menerjemahkan nama hidangan Indonesia ke Inggris dengan
SEA-LION — dikelompokkan 40 sekaligus terhadap batas 10 permintaan/menit, dan
disimpan di cache sehingga pemanggilan kedua gratis. **Terjemahan tidak pernah
menggantikan nama aslinya**: dapur memasak "Semur Ikan Bandeng", teks Inggris
mendampinginya. Tiap hasil melaporkan asalnya: model (`sealion`/`cache`) atau
glosarium luring (`rule`).

## Menjalankan SEA-LION di Amazon Bedrock

SEA-LION bukan model fondasi bawaan Bedrock; ia masuk melalui **Custom Model
Import**. `make_client()` memilih penyedia dari lingkungan:

```bash
RACIK_LLM_PROVIDER=bedrock  BEDROCK_MODEL_ARN=arn:aws:bedrock:us-east-1:...:imported-model/...
RACIK_LLM_PROVIDER=gateway  SEALION_BASE_URL=http://localhost:8000/api/v1   # Bedrock Access Gateway
RACIK_LLM_PROVIDER=sealion  SEALION_API_KEY=...                             # hosted (bawaan)
```

Kendala, diverifikasi terhadap dokumentasi AWS, bukan diasumsikan:

- **Pemanggilan alat tidak dijamin.** AWS mendokumentasikan tool calling untuk
  model impor dalam konteks GPT-OSS, dan Converse API secara eksplisit tidak
  didukung untuk Qwen. Orkestrator bergantung pada function calling, jadi
  jalankan `BedrockSeaLionClient.verify_deployment()` sebelum mengandalkannya —
  fungsi itu menguji chat *dan* tool calling lalu melaporkan keduanya.
- **Konteks harus di bawah 128K.** SEA-LION v4.5 27B membawa 262K, sehingga
  `max_position_embeddings` perlu diturunkan sebelum impor, atau pakai varian
  lebih kecil.
- **Tidak ada ap-southeast-1.** Custom Model Import hanya tersedia di us-east-1,
  us-east-2, us-west-2, dan eu-central-1 — perlu diperiksa terhadap aturan
  residensi data untuk penempatan di Indonesia.
- **Cold start itu nyata.** Bedrock mengeluarkan model impor yang menganggur dan
  memunculkan `ModelNotReadyException`; klien sudah mengatur retry boto3.

`pip install boto3` hanya diperlukan untuk penyedia Bedrock.

## Temuan yang penting

Menu lima hari untuk Jawa Tengah, menurut jenjang usia dan pagu bahan:

| Jenjang | Pagu bahan | Rata-rata energi | Target AKG | Pemenuhan | Biaya nyata |
|---|---|---|---|---|---|
| SD (7–12) | Rp7.000 | 552 kkal | 617 | **0,87** | Rp6.970 |
| SD (7–12) | Rp10.000 | 691 kkal | 617 | **1,00** | Rp7.724 |
| SMP (13–15) | Rp7.000 | 558 kkal | 742 | **0,80** | Rp6.990 |
| SMP (13–15) | Rp10.000 | 833 kkal | 742 | **1,00** | Rp8.559 |
| SMA (16–18) | Rp7.000 | 555 kkal | 792 | **0,78** | Rp6.981 |
| SMA (16–18) | Rp10.000 | 857 kkal | 792 | **1,00** | Rp8.617 |

Pada pagu bahan Rp7.000 (70% dari pagu Rp10.000 per porsi, sesuai pembagian
70/20/10 SPPG oleh BGN), **tidak ada kombinasi dalam korpus 18.000 hidangan yang
memenuhi sepertiga AKG untuk anak usia sekolah** — pagu menjadi batas pengikat,
dan pemenuhan berhenti di kisaran 0,78–0,87. Kepatuhan penuh muncul di kisaran
Rp7.700–Rp8.600 belanja bahan. Racik melaporkannya sebagai pelonggaran yang
disebut dan terukur, bukan diam-diam menyajikan nampan yang kurang.

---

## Keputusan desain yang dapat dipertanggungjawabkan

**Ekstraksi gram berbasis aturan, bukan LLM.** Setiap gram dapat ditelusuri ke
aturan bernama (`mass:ons`, `piece:siung`, `volume:sdm×0,92`, `nominal`) dan
himpunan aturannya diuji. Reproduksibilitas lebih penting daripada cakupan di
sini: mesin gizi yang angkanya berubah tiap dijalankan tidak dapat diaudit.
Cakupan 99,98% dari 260.734 baris bahan, dalam 8,6 detik.

**Pencocokan berbasis token, tidak pernah substring.** TKPI menulis daging sapi
sebagai `Sapi, daging, lemak sedang, segar`, bukan "daging sapi", sehingga
pencocokan naif gagal pada nama terbalik. Lebih buruk lagi, pencocokan substring
memetakan `ayam` ke `Bayam`. Penjaga identitas juga memblokir salah-padan
sebagian kata: `daun jeruk` tidak boleh jatuh ke `Jeruk manis`, dan
`kerupuk udang` bukan udang segar. Lebih dari 200 alias kurasi dibaca langsung
dari tabel yang dimuat, bukan disimpulkan — percobaan awal berbasis dugaan
menempatkan `kentang` pada *Ganyong* dan `tepung terigu` pada *Tapai ketan*.

| Resolusi bahan | Bagian dari 245rb kemunculan |
|---|---|
| Alias kurasi | 78,9% |
| Pengganti terdokumentasi | 1,6% |
| Pencocokan token samar | 1,1% |
| Bumbu aromatik (dibuang, tetap dibiayai) | 12,9% |
| Tidak terpadankan (rempah, non-pangan) | 5,5% |

**Biaya dihitung atas berat kotor, gizi atas berat bersih.**
`gram_beli = gram_bersih / (BDD/100)`. Ayam ber-BDD 58%, sehingga 75 g di nampan
berarti membeli 129 g ayam bertulang. Mengabaikan ini membuat perkiraan belanja
meleset hampir setengahnya untuk bahan bertulang.

**Minyak goreng diserap, bukan dimakan seluruhnya.** Resep yang menulis
"500 ml minyak goreng" akan melaporkan 460 g minyak sebagai dikonsumsi. Minyak
dibatasi pada 10% massa bahan untuk perhitungan gizi, namun tetap dibiayai
penuh karena dapur memang membelinya.

**Batas bawah gizi bersifat lunak.** Tiap batas membawa variabel kelonggaran
berdenda, sehingga solver selalu mengembalikan menu beserta daftar batas yang
terpaksa dilonggarkan. Pesan "infeasible" tidak menolong juru masak.

**Nasi dan buah adalah komponen sajian, bukan resep.** Keduanya dibangun dari
TKPI pada porsi rujukan Isi Piringku dalam beberapa ukuran, sehingga optimasi
dapat menyesuaikan nampan dengan jenjang usia. Ini sekaligus menghapus satu
kelas salah klasifikasi: tempe mendoan didominasi tepung terigu secara massa dan
akan menyamar sebagai "makanan pokok".

**Variasi ditegakkan pada keluarga bahan, bukan id hidangan.** Korpus memuat
belasan unggahan berbeda bernama varian "Tempe Mendoan"; membatasi pada
`dish_id` menghasilkan satu minggu penuh tempe dengan lima nama berbeda.

---

## Keterbatasan yang diketahui

- **Mikronutrien belum ditegakkan.** Sumber sekunder berbeda soal zat besi dan
  seng AKG 2019 (laki-laki 10–12 dilaporkan 8 mg dan juga 13 mg). Ca, Fe, Zn,
  vitamin A, dan vitamin C dihitung dan ditampilkan, ditandai `INFO`, serta
  dikeluarkan dari batasan optimasi sampai disalin dari dokumen Permenkes
  28/2019 asli. *Ini pekerjaan terbuka bernilai tertinggi.*
- **Jumlah porsi diperkirakan**, tidak dinyatakan resep sumber. Diturunkan dari
  massa bahan penentu slot terhadap porsi rujukan. Ini sumber galat per porsi
  terbesar.
- **Faktor retensi adalah perkiraan**, diterapkan pada tingkat kategori menurut
  metode masak yang terdeteksi — FAO/INFOODS menyatakan hal yang sama tentang
  tabelnya sendiri.
- **Harga adalah snapshot bertanggal.** Endpoint harian Bapanas dan BI tidak
  terdokumentasi dan dibatasi wilayah; pembaruan langsung bersifat best-effort.
- **Korpus adalah masakan rumahan**, sehingga takaran dan biaya condong ke skala
  rumah tangga, bukan pengadaan SPPG grosir.
- **Penetapan daerah diwarisi** dari pengklasifikasi kata kunci pada berkas
  sumber (11.715 keyakinan tinggi / 6.418 sedang).
- **Jalur SEA-LION langsung belum diverifikasi terhadap API sungguhan.**
  Transport, autentikasi, pembatasan laju, dan penguraian tool-call mengikuti
  kontrak terdokumentasi, dan loop diuji penuh dengan pengganti, tetapi belum
  ada panggilan dengan kunci asli. Set `SEALION_API_KEY` lalu periksa
  `/api/orchestrator` melaporkan `mode: sealion`.

---

## Struktur

| Berkas | Fungsi |
|---|---|
| `racik/config.py` | Pagu MBG, komposisi nampan, porsi rujukan |
| `racik/akg.py` | Kelompok umur AKG 2019; makro terverifikasi vs mikro informatif |
| `racik/i18n.py` | Teks dwibahasa untuk prosa yang ditulis server |
| `racik/translate.py` | Terjemahan nama hidangan via SEA-LION, dengan cadangan luring |
| `racik/report.py` | Dokumen kepatuhan + belanja siap cetak |
| `racik/bedrock.py` | Penyedia Bedrock Custom Model Import + pemeriksaan pra-pakai |
| `racik/urt.py` | Ukuran rumah tangga → gram, dengan asal-usul tiap aturan |
| `racik/tkpi.py` | Pemuat TKPI 2020, BDD, alias, pencocokan token |
| `racik/nutrition.py` | Resep → hidangan berbiaya per porsi |
| `racik/prices.py` | Tabel Rp/kg, tangga cadangan, adaptor harga langsung |
| `racik/optimizer.py` | Pemilihan menu CP-SAT dengan batas lunak |
| `racik/procurement.py` | Menu → daftar belanja teragregasi |
| `racik/store.py` | Lapisan baca SQLite |
| `racik/sealion.py` | Klien SEA-LION: auth, pembatasan laju, tool-call |
| `racik/orchestrator.py` | Definisi alat, loop agen terbatas, cadangan aturan |
| `racik/api.py` | Endpoint FastAPI + host UI statis |
| `ui/index.html` | Antarmuka perencana (ID/EN) |
| `scripts/build_db.py` | ETL |
| `scripts/package.py` | Membuat paket rilis ID dan EN |
| `docs/CODE_MAP.md` | Urutan membaca kode secara terpandu |
| `tests/` | 100 pengujian |

## API

| Endpoint | Fungsi |
|---|---|
| `GET /api/meta` | Jenjang, target AKG, provinsi, zat gizi, bahasa, statistik korpus |
| `POST /api/plan` | Menyusun menu; mengembalikan hari, kepatuhan, pelonggaran, catatan |
| `GET /api/dish/{id}` | Bahan dengan asal gram, kode TKPI, langkah |
| `POST /api/procurement` | Daftar belanja teragregasi sesuai jumlah porsi |
| `POST /api/ask` | Perencanaan bahasa alami via SEA-LION, dengan jejak alat |
| `POST /api/candidates` | Rubrik penyaring awal dan corongnya, per slot nampan |
| `POST /api/translate` | Nama hidangan Indonesia diterjemahkan ke Inggris |
| `POST /api/report` | Dokumen kepatuhan + belanja siap cetak |
| `GET /api/orchestrator` | Model yang aktif dan status keterjangkauannya |

Semua endpoint `POST` menerima `"lang": "id"` atau `"lang": "en"`.

## Pengujian

```bash
python -m pytest tests -q
```

137 pengujian: penguraian angka dan satuan, konvensi `1 ons = 100 g`, jebakan
substring ayam/bayam, integritas kode alias, berat beli BDD, penyerapan minyak,
klasifikasi slot, perilaku batas lunak pada pagu mustahil, batasan variasi,
loop orkestrator dengan pengganti SEA-LION, dan kelengkapan katalog dwibahasa.

## Sumber

AKG 2019 (Permenkes 28/2019) · TKPI 2020 (Kemenkes) · Isi Piringku (Permenkes
41/2014) · Kemenkes *Pedoman Konversi Berat Matang-Mentah, BDD* (2014) ·
panduan retensi dan rendemen FAO/INFOODS · pernyataan pagu per porsi BGN
2024–2026 · Bapanas Panel Harga / BI PIHPS · SEA-LION v4.5 (AI Singapore).
