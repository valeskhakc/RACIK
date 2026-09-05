"""Bilingual strings for the server-generated prose.

Racik ships in Indonesian and English from one source. Most labels already
travel bilingually — `NUTRIENT_META`, `SLOT_LABELS_*` and `AgeBand` each carry
both forms, and the API returns both so a client picks what it needs. What lives
here is the prose the server writes itself: the methodology notes attached to
every plan, and the text of the rule-based fallback answer.

Adding a language means adding a key block below; nothing else in the codebase
branches on language.
"""
from __future__ import annotations

from typing import Any

LANGS = ("id", "en")
# scripts/package.py rewrites this line to build the per-language packages.
DEFAULT_LANG = "id"  # BUILD:DEFAULT_LANG

LANG_NAMES = {"id": "Bahasa Indonesia", "en": "English"}


def norm_lang(value: Any) -> str:
    """Coerce anything into a supported language code."""
    code = str(value or "").strip().lower()[:2]
    return code if code in LANGS else DEFAULT_LANG


STRINGS: dict[str, dict[str, str]] = {
    "id": {
        # ---- methodology notes attached to every plan
        "note.compliance_method": (
            "Satu kali makan MBG dirancang memenuhi sekitar 1/3 AKG 2019 "
            "harian ({fraction}). Hari dinilai 'memenuhi standar' bila total "
            "5 zat gizi makro terikat (energi, protein, lemak, karbohidrat, "
            "serat) mencapai 100% dari target masing-masing; kekurangan pada "
            "salah satu batas ditampilkan sebagai pelonggaran, bukan "
            "disembunyikan. 6 zat gizi mikro (kalsium, besi, seng, vitamin A, "
            "vitamin C, natrium) dihitung dan ditampilkan sebagai informasi "
            "saja, bukan batasan, karena nilai AKG 2019 untuk zat ini "
            "berbeda antar sumber sekunder."),
        "note.method": (
            "Nilai gizi berasal dari TKPI 2020 atas dasar berat mentah, "
            "disesuaikan dengan BDD (berat dapat dimakan) dan faktor retensi "
            "kategori FAO/INFOODS sesuai metode masak yang terdeteksi. Faktor "
            "retensi bersifat perkiraan."),
        "note.servings": (
            "Jumlah porsi tiap resep korpus diperkirakan dari massa bahan "
            "terhadap porsi rujukan Isi Piringku; resep sumber tidak "
            "mencantumkan hasil jadi."),
        "note.prices": (
            "Harga bahan memakai snapshot nasional bertanggal. Endpoint harian "
            "Bapanas dan BI tidak terdokumentasi dan dibatasi wilayah, sehingga "
            "pembaruan langsung bersifat best-effort dan kembali ke snapshot."),
        "note.micro": (
            "Angka mikronutrien (Ca, Fe, Zn, vitamin A, vitamin C) ditampilkan "
            "sebagai informasi saja. Nilai mikronutrien AKG 2019 berbeda antar "
            "sumber sekunder, sehingga belum dijadikan batasan optimasi sampai "
            "diverifikasi terhadap dokumen Permenkes 28/2019 asli."),
        "note.overrun": (
            "Menu melampaui pagu bahan baku sebesar Rp{amount} untuk seluruh "
            "rencana; tidak ada kombinasi lebih murah yang memenuhi batas bawah "
            "gizi."),
        "note.relaxed": (
            "{count} batas bawah gizi dilonggarkan. Selisih terbesar adalah "
            "{nutrient} pada hari {day}, kurang {share}."),
        # ---- rule-based fallback answer
        "plan.header": (
            "Menu {days} hari untuk {stage}, {portions} porsi/hari{province}."),
        "plan.province": ", preferensi {province}",
        "plan.cost": (
            "Biaya bahan rata-rata Rp{cost} per porsi (pagu bahan Rp{budget}). "
            "Pemenuhan AKG rata-rata {adequacy}."),
        "plan.day": (
            "Hari {day}: Rp{cost} · {kcal} kkal · {protein} g protein · AKG {adequacy}"),
        "plan.relax_header": "Batas gizi yang dilonggarkan:",
        "plan.relax_row": (
            "  · Hari {day} {nutrient}: kurang {short} dari {target} ({share})"),
        "fallback.empty": (
            "Tidak ada jawaban yang dihasilkan. Coba ulangi pertanyaan dengan "
            "lebih spesifik."),
        "fallback.stopped": (
            "[Orkestrator berhenti: {reason}. Menampilkan hasil alat terakhir: "
            "{tool}.]"),
        "error.sealion": (
            "[SEA-LION tidak dapat dihubungi: {error}. Menjawab dengan "
            "perencana deterministik.]"),
        # ---- Agen Gizi / Agen Biaya / Validator (generate pipeline)
        "agent.gizi.rationale_default": (
            "Tidak ada catatan alergi/preferensi khusus; menggunakan batas "
            "gizi standar untuk jenjang ini."),
        "agent.gizi.rationale_rule_terms": (
            "Jenjang {stage}. Catatan operator menyebut: {terms}, dikecualikan "
            "dari resep kandidat."),
        "agent.gizi.rationale_rule_plain": (
            "Jenjang {stage}. Tidak ada istilah pengecualian yang terdeteksi "
            "pada catatan; menggunakan batas gizi standar."),
        "agent.gizi.rationale_feedback": (
            "{count} hidangan yang pernah ditolak operator dikecualikan dari "
            "resep kandidat (alasan: {reasons})."),
        "agent.biaya.rationale_default": (
            "Seluruh anggaran yang dimasukkan operator dipakai untuk bahan baku."),
        "agent.biaya.rationale_rule": (
            "Pagu per porsi Rp{budget}, seluruhnya untuk bahan baku. Tanpa "
            "data harga daerah langsung, memakai indeks harga nasional (1,0x) "
            "untuk provinsi {province}."),
        "agent.biaya.rationale_rule_market": (
            "Pagu per porsi Rp{budget}, seluruhnya untuk bahan baku; indeks "
            "kemahalan {province} sebesar {index}x berasal "
            "dari data survei harga eceran WFP, bukan asumsi."),
        "validator.pass": (
            "Tervalidasi: seluruh {days} hari memenuhi batas gizi AKG dan pagu "
            "bahan baku tanpa pelonggaran."),
        "validator.fail_nutrition": (
            "{count} batas gizi dilonggarkan. Selisih terbesar: {nutrient} pada "
            "hari {day}, kurang {share} dari target."),
        "validator.fail_budget": (
            "Rencana melampaui pagu bahan baku sebesar Rp{amount} untuk "
            "seluruh periode."),
    },
    "en": {
        "note.compliance_method": (
            "One MBG meal is designed to cover about 1/3 of daily AKG 2019 "
            "({fraction}). A day is scored as 'meeting the standard' when the "
            "total of all 5 binding macro nutrients (energy, protein, fat, "
            "carbohydrate, fibre) reaches 100% of its target; a shortfall on "
            "any one is reported as a relaxation, never hidden. 6 micronutrients "
            "(calcium, iron, zinc, vitamin A, vitamin C, sodium) are computed "
            "and shown for information only, not enforced, because "
            "secondary sources disagree on their AKG 2019 values."),
        "note.method": (
            "Nutrient values come from TKPI 2020 on a raw-weight basis, adjusted "
            "by BDD (edible portion) and by FAO/INFOODS category retention "
            "factors for the inferred cooking method. Retention factors are "
            "approximations."),
        "note.servings": (
            "Serving counts for corpus recipes are estimated from ingredient "
            "mass against Isi Piringku reference portions; the source recipes "
            "do not state a yield."),
        "note.prices": (
            "Ingredient prices use a dated national snapshot. The daily Bapanas "
            "and BI endpoints are undocumented and geo-restricted, so live "
            "refresh is best-effort and falls back to the snapshot."),
        "note.micro": (
            "Micronutrient figures (Ca, Fe, Zn, vitamin A, vitamin C) are shown "
            "for information only. AKG 2019 micronutrient values disagree across "
            "secondary sources, so they are not enforced as constraints until "
            "verified against the original Permenkes 28/2019 document."),
        "note.overrun": (
            "The menu exceeds the ingredient budget by Rp{amount} across the "
            "plan; no cheaper combination met the nutrient floors."),
        "note.relaxed": (
            "{count} nutrient floor(s) were relaxed. The largest gap is "
            "{nutrient} on day {day}, short by {share}."),
        "plan.header": (
            "A {days}-day menu for {stage}, {portions} portions/day{province}."),
        "plan.province": ", regional preference {province}",
        "plan.cost": (
            "Mean ingredient cost Rp{cost} per portion (ingredient budget "
            "Rp{budget}). Mean AKG adequacy {adequacy}."),
        "plan.day": (
            "Day {day}: Rp{cost} · {kcal} kcal · {protein} g protein · AKG {adequacy}"),
        "plan.relax_header": "Relaxed nutrient floors:",
        "plan.relax_row": (
            "  · Day {day} {nutrient}: short {short} of {target} ({share})"),
        "fallback.empty": (
            "No answer was produced. Try asking again with more detail."),
        "fallback.stopped": (
            "[Orchestrator stopped: {reason}. Showing the last tool result: "
            "{tool}.]"),
        "error.sealion": (
            "[SEA-LION could not be reached: {error}. Answering with the "
            "deterministic planner.]"),
        # ---- Agen Gizi / Agen Biaya / Validator (generate pipeline)
        "agent.gizi.rationale_default": (
            "No allergy/preference notes given; using the standard nutrient "
            "floors for this stage."),
        "agent.gizi.rationale_rule_terms": (
            "Stage {stage}. The operator's notes mention: {terms}, excluded "
            "from candidate recipes."),
        "agent.gizi.rationale_rule_plain": (
            "Stage {stage}. No exclusion terms detected in the notes; using "
            "the standard nutrient floors."),
        "agent.gizi.rationale_feedback": (
            "{count} previously-rejected dish(es) excluded from candidate "
            "recipes (reasons: {reasons})."),
        "agent.biaya.rationale_default": (
            "The operator's full budget input is used for raw ingredients."),
        "agent.biaya.rationale_rule": (
            "Per-portion pagu Rp{budget}, all of it for raw ingredients. With "
            "no live regional price feed, using the national price index "
            "(1.0x) for {province}."),
        "agent.biaya.rationale_rule_market": (
            "Per-portion pagu Rp{budget}, all of it for raw ingredients; "
            "{province}'s {index}x regional cost index comes from the "
            "WFP retail price survey, not an assumption."),
        "validator.pass": (
            "Validated: all {days} day(s) meet the AKG nutrient floors and the "
            "ingredient budget with no relaxation."),
        "validator.fail_nutrition": (
            "{count} nutrient floor(s) were relaxed. Largest gap: {nutrient} on "
            "day {day}, short {share} of target."),
        "validator.fail_budget": (
            "The plan exceeds the ingredient budget by Rp{amount} across the "
            "whole period."),
    },
}


# Measurement units are language-neutral except for the energy unit, which is
# "kkal" in Indonesian and "kcal" in English.
_UNIT_OVERRIDES = {"en": {"kkal": "kcal"}}


def unit(lang: str, symbol: str) -> str:
    """Localise a unit symbol; returns it unchanged when no override exists."""
    return _UNIT_OVERRIDES.get(norm_lang(lang), {}).get(symbol, symbol)


def t(lang: str, key: str, **kwargs: Any) -> str:
    """Look up a string, falling back to the default language then the key."""
    lang = norm_lang(lang)
    text = STRINGS.get(lang, {}).get(key) \
        or STRINGS[DEFAULT_LANG].get(key) \
        or key
    try:
        return text.format(**kwargs) if kwargs else text
    except (KeyError, IndexError):
        return text
