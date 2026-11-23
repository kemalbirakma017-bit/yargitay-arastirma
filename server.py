"""
Yargıtay Karar Arama MCP Sunucusu
==================================
Adalet Bakanlığı Bedesten emsal karar API'si üzerinden Yargıtay kararı arar
ve tam metin getirir.

Neden Bedesten: eski karararama.yargitay.gov.tr arayüzü filtreleri
kararsız uyguluyor, toplam sayıyı bazen null döndürüyor. Bedesten daire
(birimAdi) ve tarih filtrelerini sunucu tarafında uygular ve toplamı
"total" alanında verir.

Kamuya açık istemci kaynak kodundan türetilen API beklentisi (canlı doğrulanmadı):
  POST /emsal-karar/searchDocuments
      {"data": {...}, "applicationName": "UyapMevzuat", "paging": true}
      yanıt: {"data": {"emsalKararList": [...], "total": N, "start": S}}
  POST /emsal-karar/getDocumentContent
      {"applicationName": "UyapMevzuat", "data": {"documentId": ID}}
      yanıt: {"data": {"content": "<base64 html>", "mimeType": "..."}}
  Hız sınırı: HTTP 429 (Retry-After başlığı olabilir).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import time
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP

BEDESTEN_BASE = "https://bedesten.adalet.gov.tr"
SOURCE_URL = "https://mevzuat.adalet.gov.tr/ictihat/{doc_id}"
APPLICATION_NAME = "UyapMevzuat"
ITEM_TYPE_YARGITAY = "YARGITAYKARARI"

MIN_INTERVAL = float(os.environ.get("YARGITAY_MIN_INTERVAL", "3.0"))
TIMEOUT = 30.0
MAX_RETRIES = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
}

# Geçerli daire adları (birimAdi filtresi ile birebir aynı yazılır)
DAIRELER: list[str] = (
    ["Büyük Genel Kurul", "Hukuk Genel Kurulu", "Ceza Genel Kurulu",
     "Hukuk Daireleri Başkanlar Kurulu", "Ceza Daireleri Başkanlar Kurulu"]
    + [f"{i}. Hukuk Dairesi" for i in range(1, 24)]
    + [f"{i}. Ceza Dairesi" for i in range(1, 24)]
)
_DAIRE_SET = set(DAIRELER)

mcp = FastMCP(
    name="Yargıtay Karar Arama",
    instructions=(
        "Yargıtay kararlarını arar ve tam metnini getirir. Önce "
        "yargitay_karar_ara ile arama yapın, sonuçtaki 'id' değerini "
        "yargitay_karar_metni aracına verin. Metinle birlikte toplu araştırma "
        "için yargitay_toplu_arastirma kullanın."
    ),
)


class UpstreamError(Exception):
    """Kaynak servisten beklenmeyen yanıt."""


class RateLimitedError(UpstreamError):
    pass


class _Pacer:
    """İstekler arasında minimum aralık bırakır ve 429'da bekler."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._pause_until = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._pause_until:
                await asyncio.sleep(self._pause_until - now)
                now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = time.monotonic()

    def pause(self, seconds: float) -> None:
        self._pause_until = max(self._pause_until, time.monotonic() + seconds)


log = logging.getLogger("yargitay")
logging.basicConfig(level=os.environ.get("YARGITAY_LOG_LEVEL", "INFO"))

_pacer = _Pacer(MIN_INTERVAL)
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Karar metni önbelleği (id -> düz metin)
_CACHE_MAX = int(os.environ.get("YARGITAY_CACHE_MAX", "500"))
_doc_cache: dict[str, str] = {}


def _cache_put(doc_id: str, text: str) -> None:
    if len(_doc_cache) >= _CACHE_MAX:
        _doc_cache.pop(next(iter(_doc_cache)))
    _doc_cache[doc_id] = text


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                base_url=BEDESTEN_BASE, headers=HEADERS, timeout=TIMEOUT
            )
        return _client


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Hız sınırına saygılı POST; 429'da Retry-After kadar bekleyip yeniden dener."""
    client = await _get_client()
    for attempt in range(MAX_RETRIES + 1):
        await _pacer.wait()
        log.info("bedesten POST start path=%s attempt=%d", path, attempt + 1)
        t0 = time.monotonic()
        try:
            resp = await client.post(path, json=body)
        except httpx.HTTPError as exc:
            log.error(
                "bedesten POST error path=%s after=%.1fs err=%s",
                path, time.monotonic() - t0, type(exc).__name__,
            )
            raise UpstreamError(
                f"Bedesten bağlantı hatası: {type(exc).__name__}"
            ) from exc
        log.info(
            "bedesten POST done path=%s status=%d after=%.1fs",
            path, resp.status_code, time.monotonic() - t0,
        )
        if resp.status_code == 429:
            retry_after = _retry_after_seconds(resp.headers.get("retry-after"))
            _pacer.pause(retry_after)
            if attempt < MAX_RETRIES:
                continue
            raise RateLimitedError(
                "Yargıtay karar servisi hız sınırı uyguladı (HTTP 429). "
                "Lütfen bir-iki dakika bekleyip yeniden deneyin."
            )
        if resp.status_code >= 500 and attempt < MAX_RETRIES:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise UpstreamError(f"Kaynak servis HTTP {resp.status_code} döndürdü.")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamError("Kaynak servis JSON dışında yanıt döndürdü.") from exc
        if not isinstance(payload, dict):
            raise UpstreamError("Kaynak servis beklenmeyen bir yanıt biçimi döndürdü.")
        return payload
    raise UpstreamError("Kaynak servise ulaşılamadı.")


def _retry_after_seconds(value: Optional[str]) -> float:
    try:
        seconds = float(value) if value else 20.0
    except ValueError:
        seconds = 20.0
    return min(max(seconds, 1.0), 60.0)


def _as_int(value: Any) -> Optional[int]:
    """total/start gibi sayısal alanları güvenle tamsayıya çevirir; yoksa None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _keyword_windows(
    text: str, keyword: str, window: int = 700, max_hits: int = 4
) -> str:
    """Aranan ifadenin geçtiği bölgeleri (±window karakter) döndürür."""
    core = keyword.replace('"', "").strip()
    terms = [t for t in re.split(r"\s+", core) if len(t) > 2] or ([core] if core else [])
    if not terms:
        return text[: 2 * window]
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    spans: list[tuple[int, int]] = []
    for m in pattern.finditer(text):
        start, end = max(0, m.start() - window), min(len(text), m.end() + window)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
        if len(spans) >= max_hits:
            break
    if not spans:
        return text[: 2 * window]
    parts = [
        ("…" if s > 0 else "") + text[s:e] + ("…" if e < len(text) else "")
        for s, e in spans
    ]
    return "\n[...]\n".join(parts)


def _validate_date(value: str, name: str) -> None:
    if value and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        raise ValueError(f"{name} GG.AA.YYYY formatında olmalıdır (örn. 01.01.2024).")


def _gg_aa_yyyy_to_iso_start(value: str) -> str:
    d, m, y = value.split(".")
    return f"{y}-{m}-{d}T00:00:00.000Z"


def _gg_aa_yyyy_to_iso_end(value: str) -> str:
    d, m, y = value.split(".")
    return f"{y}-{m}-{d}T23:59:59.000Z"


def _validate_daire(daire: str) -> str:
    daire = (daire or "").strip()
    if not daire or daire.upper() == "ALL":
        return ""
    if daire == "Büyük Genel Kurulu":
        daire = "Büyük Genel Kurul"
    if daire not in _DAIRE_SET:
        raise ValueError(
            f"Geçersiz daire adı: {daire!r}. Geçerli adlar için "
            "yargitay_daire_listesi aracını çağırın."
        )
    return daire


def _record_to_kunye(r: dict[str, Any]) -> dict[str, Any]:
    esas = r.get("esasNo") or (
        f"{r.get('esasNoYil')}/{r.get('esasNoSira')}"
        if r.get("esasNoYil") and r.get("esasNoSira") else None
    )
    karar = r.get("kararNo") or (
        f"{r.get('kararNoYil')}/{r.get('kararNoSira')}"
        if r.get("kararNoYil") and r.get("kararNoSira") else None
    )
    doc_id = r.get("documentId") or r.get("id") or r.get("emsalKararId") or ""
    return {
        "id": str(doc_id),
        "daire": r.get("birimAdi"),
        "esas_no": esas,
        "karar_no": karar,
        "karar_tarihi": r.get("kararTarihi") or r.get("kararTarihiStr"),
        "kesinlesme_durumu": r.get("kesinlesmeDurumu"),
        "kaynak": SOURCE_URL.format(doc_id=str(doc_id)),
    }


async def _do_search(
    aranan_kelime: str = "",
    daire: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    sayfa: int = 1,
    sayfa_boyutu: int = 10,
    esas_yil: str = "",
    esas_sira_no: str = "",
    karar_yil: str = "",
    karar_sira_no: str = "",
) -> dict[str, Any]:
    """Yargıtay kararlarında arama yapar; künye listesi ve toplam sayı döner."""
    _validate_date(baslangic_tarihi, "baslangic_tarihi")
    _validate_date(bitis_tarihi, "bitis_tarihi")
    daire = _validate_daire(daire)
    sayfa = max(1, int(sayfa))
    sayfa_boyutu = min(100, max(1, int(sayfa_boyutu)))
    if bool(esas_yil) != bool(esas_sira_no):
        raise ValueError("esas_yil ile esas_sira_no birlikte verilmelidir.")
    if bool(karar_yil) != bool(karar_sira_no):
        raise ValueError("karar_yil ile karar_sira_no birlikte verilmelidir.")

    data: dict[str, Any] = {
        "pageSize": sayfa_boyutu,
        "pageNumber": sayfa,
        "itemTypeList": [ITEM_TYPE_YARGITAY],
        "sortFields": ["KARAR_TARIHI"],
        "sortDirection": "desc",
    }
    phrase = (aranan_kelime or "").strip()
    if phrase:
        data["phrase"] = phrase
    if daire:
        data["birimAdi"] = daire
    if baslangic_tarihi:
        data["kararTarihiStart"] = _gg_aa_yyyy_to_iso_start(baslangic_tarihi)
    if bitis_tarihi:
        data["kararTarihiEnd"] = _gg_aa_yyyy_to_iso_end(bitis_tarihi)
    if esas_yil:
        data["esasNoYil"] = str(esas_yil)
        data["esasNoSira"] = str(esas_sira_no)
    if karar_yil:
        data["kararNoYil"] = str(karar_yil)
        data["kararNoSira"] = str(karar_sira_no)

    payload = await _post(
        "/emsal-karar/searchDocuments",
        {"data": data, "applicationName": APPLICATION_NAME, "paging": True},
    )
    inner = payload.get("data")
    if not isinstance(inner, dict):
        raise UpstreamError("Arama yanıtında data alanı yok.")
    rows = inner.get("emsalKararList") or []
    if not isinstance(rows, list):
        raise UpstreamError("Arama yanıtında emsalKararList geçersiz.")
    total = _as_int(inner.get("total"))
    kararlar = [_record_to_kunye(r) for r in rows if isinstance(r, dict)]
    return {
        "toplam_sonuc": total,
        "sayfa": sayfa,
        "sayfa_boyutu": sayfa_boyutu,
        "kararlar": kararlar,
        "not": (
            "Karar tam metni için yargitay_karar_metni aracını 'id' ile çağırın."
            if kararlar else
            "Sonuç yok. Arama kelimesini sadeleştirmeyi veya filtreleri "
            "gevşetmeyi deneyin."
        ),
    }


async def _fetch_doc_text(doc_id: str) -> str:
    """Karar metnini önbellekten veya kaynaktan getirir (düz metin)."""
    if doc_id in _doc_cache:
        return _doc_cache[doc_id]
    payload = await _post(
        "/emsal-karar/getDocumentContent",
        {"applicationName": APPLICATION_NAME, "data": {"documentId": doc_id}},
    )
    data = payload.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, str) or not content:
        return ""
    try:
        html = base64.b64decode(content, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError) as exc:
        raise UpstreamError("Belge içeriği çözülemedi.") from exc
    text = _html_to_text(html)
    if text:
        _cache_put(doc_id, text)
    return text


async def _toplu_impl(
    aranan_kelime: str,
    daire: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    sonuc_sayisi: int = 30,
    metin_sayisi: int = 5,
    metin_bicimi: str = "pencere",
) -> dict[str, Any]:
    if not (aranan_kelime or "").strip():
        raise ValueError("aranan_kelime boş olamaz.")
    sonuc_sayisi = min(100, max(1, int(sonuc_sayisi)))
    metin_sayisi = min(10, max(0, int(metin_sayisi)))
    if metin_bicimi not in ("pencere", "tam"):
        raise ValueError('metin_bicimi "pencere" veya "tam" olmalıdır.')

    arama = await _do_search(
        aranan_kelime=aranan_kelime, daire=daire,
        baslangic_tarihi=baslangic_tarihi, bitis_tarihi=bitis_tarihi,
        sayfa=1, sayfa_boyutu=sonuc_sayisi,
    )
    metinler: list[dict[str, Any]] = []
    hatalar: list[str] = []
    for k in arama["kararlar"][:metin_sayisi]:
        try:
            text = await _fetch_doc_text(k["id"])
        except RateLimitedError as e:
            hatalar.append(f"{k['id']}: {e}")
            break
        except UpstreamError as e:
            hatalar.append(f"{k['id']}: {e}")
            continue
        metinler.append({
            "id": k["id"],
            "daire": k["daire"],
            "esas_no": k["esas_no"],
            "karar_no": k["karar_no"],
            "karar_tarihi": k["karar_tarihi"],
            "metin": (
                _keyword_windows(text, aranan_kelime)
                if metin_bicimi == "pencere" else text
            ),
        })

    out: dict[str, Any] = {
        "toplam_sonuc": arama["toplam_sonuc"],
        "kararlar": arama["kararlar"],
        "metinler": metinler,
        "metin_bicimi": metin_bicimi,
    }
    if hatalar:
        out["hatalar"] = hatalar
    toplam = arama["toplam_sonuc"]
    if toplam is not None and toplam > sonuc_sayisi:
        out["not"] = (
            f"Toplam {toplam} sonuçtan ilk {sonuc_sayisi} künye listelendi. "
            "Daraltmak için daire/tarih filtresi veya daha spesifik ifade kullanın."
        )
    return out


@mcp.tool
async def yargitay_daire_listesi() -> dict:
    """Yargıtay karar aramasında kullanılabilecek geçerli daire/kurul adlarını döndürür.

    yargitay_karar_ara ve yargitay_toplu_arastirma aracının 'daire' parametresine
    bu listeden bir değer verin.
    """
    return {"daireler": DAIRELER}


@mcp.tool
async def yargitay_karar_ara(
    aranan_kelime: str = "",
    daire: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    sayfa: int = 1,
    sayfa_boyutu: int = 10,
    esas_yil: str = "",
    esas_ilk_sira_no: str = "",
    esas_son_sira_no: str = "",
    karar_yil: str = "",
    karar_ilk_sira_no: str = "",
    karar_son_sira_no: str = "",
) -> dict:
    """Yargıtay kararlarında arama yapar (yalnızca künye listesi döner).

    Metinle birlikte araştırma için yargitay_toplu_arastirma aracını tercih edin.

    Args:
        aranan_kelime: Aranacak kelime/ifade. Kesin ifade için çift tırnak:
            "elektrik piyasası".
        daire: Daire/kurul adı (örn. "8. Hukuk Dairesi"). Boş = tüm daireler.
            Geçerli adlar: yargitay_daire_listesi.
        baslangic_tarihi: Karar tarihi aralığı başlangıcı, GG.AA.YYYY.
        bitis_tarihi: Karar tarihi aralığı sonu, GG.AA.YYYY.
        sayfa: Sonuç sayfası (1'den başlar).
        sayfa_boyutu: Sayfa başına sonuç (1-100).
        esas_yil: Esas yılı (YYYY). esas_ilk_sira_no ile birlikte kullanılır.
        esas_ilk_sira_no: Esas sıra no (tek numara). esas_yil ile birlikte.
        esas_son_sira_no: Kullanılmaz; geriye dönük uyumluluk için kabul edilir.
        karar_yil: Karar yılı (YYYY). karar_ilk_sira_no ile birlikte kullanılır.
        karar_ilk_sira_no: Karar sıra no (tek numara). karar_yil ile birlikte.
        karar_son_sira_no: Kullanılmaz; geriye dönük uyumluluk için kabul edilir.
    """
    log.info("tool yargitay_karar_ara start")
    result = await _do_search(
        aranan_kelime=aranan_kelime, daire=daire,
        baslangic_tarihi=baslangic_tarihi, bitis_tarihi=bitis_tarihi,
        sayfa=sayfa, sayfa_boyutu=sayfa_boyutu,
        esas_yil=esas_yil, esas_sira_no=esas_ilk_sira_no,
        karar_yil=karar_yil, karar_sira_no=karar_ilk_sira_no,
    )
    ignored_ranges = []
    if esas_son_sira_no:
        ignored_ranges.append("esas_son_sira_no")
    if karar_son_sira_no:
        ignored_ranges.append("karar_son_sira_no")
    if ignored_ranges:
        result["uyari"] = (
            ", ".join(ignored_ranges)
            + " Bedesten API'sinde desteklenmediği için uygulanmadı; sonuçlar "
            "istenen sıra aralığından daha geniş olabilir."
        )
    log.info(
        "tool yargitay_karar_ara done result_count=%d total=%s",
        len(result.get("kararlar", [])), result.get("toplam_sonuc"),
    )
    return result


@mcp.tool
async def yargitay_toplu_arastirma(
    aranan_kelime: str,
    daire: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    sonuc_sayisi: int = 30,
    metin_sayisi: int = 5,
    metin_bicimi: str = "pencere",
) -> dict:
    """Tek çağrıda emsal araştırması: arama + ilk N kararın metni.

    Args:
        aranan_kelime: Aranacak kelime/ifade. Zorunlu.
        daire: Daire/kurul adı (boş = tüm daireler).
        baslangic_tarihi: GG.AA.YYYY.
        bitis_tarihi: GG.AA.YYYY.
        sonuc_sayisi: Künyesi listelenecek karar sayısı (1-100).
        metin_sayisi: Metni getirilecek karar sayısı (0-10).
        metin_bicimi: "pencere" (aranan ifadenin geçtiği bölümler) | "tam".
    """
    return await _toplu_impl(
        aranan_kelime, daire, baslangic_tarihi, bitis_tarihi,
        sonuc_sayisi, metin_sayisi, metin_bicimi,
    )


@mcp.tool
async def yargitay_karar_metni(karar_id: str) -> dict:
    """Bir Yargıtay kararının tam metnini getirir.

    Args:
        karar_id: yargitay_karar_ara sonucundaki 'id' değeri.
    """
    karar_id = str(karar_id).strip()
    if not re.fullmatch(r"\d{1,20}", karar_id):
        raise ValueError("karar_id sayısal bir kimlik olmalıdır (arama sonucundaki 'id').")
    text = await _fetch_doc_text(karar_id)
    if not text:
        return {
            "id": karar_id,
            "metin": "",
            "uyari": "Belge boş döndü; id geçersiz veya belge kaldırılmış olabilir.",
        }
    return {
        "id": karar_id,
        "kaynak": SOURCE_URL.format(doc_id=karar_id),
        "metin": text,
    }


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="http",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
            path=os.environ.get("MCP_PATH", "/mcp"),
        )

