"""
Yargıtay Karar Arama MCP Sunucusu
==================================
karararama.yargitay.gov.tr (kamuya açık Yargıtay karar arama platformu)
üzerinde arama ve karar tam metni çekme araçları sunar.

Doğrulanmış API davranışı (16.07.2026 canlı test):
  - POST /aramadetaylist  -> {"data": {...arama alanları...}}
    * "aranan" ve "arananKelime" alanlarının İKİSİ de gönderilmelidir.
    * Daire filtresi "birimYrgKurulDaire" gerçek daire adı ister
      ("ALL" gönderilirse 0 sonuç döner — tüm daireler için alan hiç gönderilmez).
    * Tarihler GG.AA.YYYY formatındadır.
    * Yanıt: {"data": {"recordsTotal": N, "data": [{id, daire, esasNo,
      kararNo, kararTarihi, arananKelime, index, siraNo}]}}
  - GET /getDokuman?id=<id> -> {"data": "<html>"} (karar tam metni)
  - Hız sınırı: art arda hızlı isteklerde HTTP 429 ("Erişim Sınırı Aşıldı").
    Bu sunucu istekler arasına en az MIN_INTERVAL saniye koyar ve 429'da
    bekleyip bir kez yeniden dener.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP

BASE_URL = "https://karararama.yargitay.gov.tr"
MIN_INTERVAL = float(os.environ.get("YARGITAY_MIN_INTERVAL", "3.0"))
RETRY_WAIT = float(os.environ.get("YARGITAY_RETRY_WAIT", "20.0"))
TIMEOUT = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}

# Geçerli daire adları (birimYrgKurulDaire alanı için)
DAIRELER: list[str] = (
    ["Büyük Genel Kurulu", "Hukuk Genel Kurulu", "Ceza Genel Kurulu",
     "Hukuk Daireleri Başkanlar Kurulu", "Ceza Daireleri Başkanlar Kurulu"]
    + [f"{i}. Hukuk Dairesi" for i in range(1, 24)]
    + [f"{i}. Ceza Dairesi" for i in range(1, 24)]
)

mcp = FastMCP(
    name="Yargıtay Karar Arama",
    instructions=(
        "karararama.yargitay.gov.tr üzerinde Yargıtay kararı arar ve karar "
        "tam metnini getirir. Önce yargitay_karar_ara ile arama yapın, "
        "sonuçtaki 'id' değerini yargitay_karar_metni aracına verin. "
        "Kaynak sitede hız sınırı vardır; araçlar istekleri otomatik "
        "yavaşlatır — art arda çok sayıda çağrıdan kaçının."
    ),
)


class _Throttle:
    """Kaynak siteye giden istekleri küresel olarak aralıklandırır."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = time.monotonic()


_throttle = _Throttle(MIN_INTERVAL)
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Karar metni önbelleği (id -> düz metin). Aynı oturumda tekrar istenen
# kararlar siteye gidilmeden döner.
_CACHE_MAX = int(os.environ.get("YARGITAY_CACHE_MAX", "500"))
_doc_cache: dict[str, str] = {}


def _cache_put(karar_id: str, text: str) -> None:
    if len(_doc_cache) >= _CACHE_MAX:
        _doc_cache.pop(next(iter(_doc_cache)))
    _doc_cache[karar_id] = text


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                base_url=BASE_URL, headers=HEADERS, timeout=TIMEOUT,
                follow_redirects=True,
            )
            try:
                # Çerez almak için ana sayfaya bir kez dokun.
                await _client.get("/")
            except httpx.HTTPError:
                pass  # Çerez alınamazsa da API çoğu zaman çalışır.
        return _client


class RateLimitedError(Exception):
    pass


async def _request(method: str, path: str, **kw: Any) -> Any:
    """Hız sınırına saygılı istek; 429'da bir kez bekleyip yeniden dener."""
    client = await _get_client()
    for attempt in (1, 2):
        await _throttle.wait()
        resp = await client.request(method, path, **kw)
        if resp.status_code == 429 or (
            resp.status_code == 200
            and resp.headers.get("content-type", "").startswith("text/html")
            and "Erişim Sınırı" in resp.text[:2000]
        ):
            if attempt == 1:
                await asyncio.sleep(RETRY_WAIT)
                continue
            raise RateLimitedError(
                "Yargıtay karar arama sitesi hız sınırı uyguladı (HTTP 429). "
                "Lütfen 1-2 dakika bekleyip yeniden deneyin."
            )
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def _html_to_text(html: str) -> str:
    """Karar HTML'ini okunur düz metne çevirir."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # <br> ve blok öğeleri satır sonuna çevir
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _keyword_windows(text: str, keyword: str, window: int = 700, max_hits: int = 4) -> str:
    """Metinde aranan ifadenin geçtiği bölgeleri (±window karakter) döndürür."""
    if not keyword:
        return text[: 2 * window]
    core = keyword.replace('"', "").strip()
    terms = [t for t in re.split(r"\s+", core) if len(t) > 2] or [core]
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
    parts = [("…" if s > 0 else "") + text[s:e] + ("…" if e < len(text) else "") for s, e in spans]
    return "\n[...]\n".join(parts)


async def _fetch_doc_text(karar_id: str) -> str:
    """Karar metnini önbellekten veya siteden getirir."""
    if karar_id in _doc_cache:
        return _doc_cache[karar_id]
    payload = await _request("GET", "/getDokuman", params={"id": karar_id})
    html = (payload or {}).get("data") or ""
    text = _html_to_text(html) if html else ""
    if text:
        _cache_put(karar_id, text)
    return text


def _validate_date(value: str, name: str) -> None:
    if value and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        raise ValueError(f"{name} GG.AA.YYYY formatında olmalıdır (örn. 01.01.2024).")


@mcp.tool
async def yargitay_daire_listesi() -> dict:
    """Yargıtay karar aramasında kullanılabilecek geçerli daire/kurul adlarını döndürür.

    yargitay_karar_ara aracının 'daire' parametresine bu listeden bir değer verin.
    """
    return {"daireler": DAIRELER}


async def _do_search(
    aranan_kelime: str = "",
    daire: str = "",
    esas_yil: str = "",
    esas_ilk_sira_no: str = "",
    esas_son_sira_no: str = "",
    karar_yil: str = "",
    karar_ilk_sira_no: str = "",
    karar_son_sira_no: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    sayfa: int = 1,
    sayfa_boyutu: int = 10,
) -> dict:
    """Yargıtay kararlarında detaylı arama yapar (karararama.yargitay.gov.tr).

    Args:
        aranan_kelime: Aranacak kelime/ifade. Kesin ifade için çift tırnak
            kullanın: "elektrik piyasası". Boş bırakılıp yalnızca
            daire/esas/karar/tarih filtreleriyle de arama yapılabilir.
        daire: Daire/kurul adı (örn. "3. Hukuk Dairesi", "Hukuk Genel Kurulu").
            Boş bırakılırsa tüm dairelerde arar. Geçerli adlar için
            yargitay_daire_listesi aracını kullanın.
        esas_yil: Esas yılı (YYYY).
        esas_ilk_sira_no: Esas ilk sıra no (aralık başlangıcı).
        esas_son_sira_no: Esas son sıra no (aralık sonu).
        karar_yil: Karar yılı (YYYY).
        karar_ilk_sira_no: Karar ilk sıra no.
        karar_son_sira_no: Karar son sıra no.
        baslangic_tarihi: Karar tarihi aralığı başlangıcı, GG.AA.YYYY.
        bitis_tarihi: Karar tarihi aralığı sonu, GG.AA.YYYY.
        sayfa: Sonuç sayfası (1'den başlar).
        sayfa_boyutu: Sayfa başına sonuç (1-100).

    Returns:
        toplam_sonuc ve kararlar listesi (id, daire, esas_no, karar_no,
        karar_tarihi). Tam metin için id'yi yargitay_karar_metni aracına verin.
    """
    _validate_date(baslangic_tarihi, "baslangic_tarihi")
    _validate_date(bitis_tarihi, "bitis_tarihi")
    if daire:
        if daire.strip().upper() == "ALL":
            daire = ""
        elif daire not in DAIRELER:
            raise ValueError(
                f"Geçersiz daire adı: {daire!r}. "
                "Geçerli adlar için yargitay_daire_listesi aracını çağırın."
            )
    sayfa = max(1, int(sayfa))
    sayfa_boyutu = min(100, max(1, int(sayfa_boyutu)))

    data: dict[str, Any] = {
        # Site her iki alanı da bekliyor (canlı doğrulandı).
        "aranan": aranan_kelime or "",
        "arananKelime": aranan_kelime or "",
        "pageSize": sayfa_boyutu,
        "pageNumber": sayfa,
    }
    # Boş filtre alanlarını hiç gönderme; "ALL" değeri 0 sonuç döndürüyor.
    if daire:
        data["birimYrgKurulDaire"] = daire
    for key, val in {
        "esasYil": esas_yil, "esasIlkSiraNo": esas_ilk_sira_no,
        "esasSonSiraNo": esas_son_sira_no, "kararYil": karar_yil,
        "kararIlkSiraNo": karar_ilk_sira_no, "kararSonSiraNo": karar_son_sira_no,
        "baslangicTarihi": baslangic_tarihi, "bitisTarihi": bitis_tarihi,
    }.items():
        if val:
            data[key] = str(val)

    payload = await _request("POST", "/aramadetaylist", json={"data": data})
    inner = (payload or {}).get("data") or {}
    rows = inner.get("data") or []
    kararlar = [
        {
            "id": str(r.get("id", "")),
            "daire": r.get("daire"),
            "esas_no": r.get("esasNo"),
            "karar_no": r.get("kararNo"),
            "karar_tarihi": r.get("kararTarihi"),
        }
        for r in rows
    ]
    return {
        "toplam_sonuc": inner.get("recordsTotal", 0),
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


@mcp.tool
async def yargitay_karar_ara(
    aranan_kelime: str = "",
    daire: str = "",
    esas_yil: str = "",
    esas_ilk_sira_no: str = "",
    esas_son_sira_no: str = "",
    karar_yil: str = "",
    karar_ilk_sira_no: str = "",
    karar_son_sira_no: str = "",
    baslangic_tarihi: str = "",
    bitis_tarihi: str = "",
    sayfa: int = 1,
    sayfa_boyutu: int = 10,
) -> dict:
    """Yargıtay kararlarında detaylı arama yapar (yalnızca künye listesi döner).

    Metinlerle birlikte TOPLU araştırma için yargitay_toplu_arastirma aracını
    tercih edin — çok daha hızlıdır (tek çağrıda arama + metinler).

    Args:
        aranan_kelime: Aranacak kelime/ifade. Kesin ifade için çift tırnak:
            "elektrik piyasası".
        daire: Daire/kurul adı (örn. "3. Hukuk Dairesi", "Hukuk Genel Kurulu").
            Boş bırakılırsa tüm daireler. Geçerli adlar: yargitay_daire_listesi.
        esas_yil: Esas yılı (YYYY).
        esas_ilk_sira_no: Esas ilk sıra no.
        esas_son_sira_no: Esas son sıra no.
        karar_yil: Karar yılı (YYYY).
        karar_ilk_sira_no: Karar ilk sıra no.
        karar_son_sira_no: Karar son sıra no.
        baslangic_tarihi: Karar tarihi aralığı başlangıcı, GG.AA.YYYY.
        bitis_tarihi: Karar tarihi aralığı sonu, GG.AA.YYYY.
        sayfa: Sonuç sayfası (1'den başlar).
        sayfa_boyutu: Sayfa başına sonuç (1-100).
    """
    return await _do_search(
        aranan_kelime, daire, esas_yil, esas_ilk_sira_no, esas_son_sira_no,
        karar_yil, karar_ilk_sira_no, karar_son_sira_no,
        baslangic_tarihi, bitis_tarihi, sayfa, sayfa_boyutu,
    )


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
    """TEK ÇAĞRIDA emsal araştırması: arama + ilk N kararın metni birlikte döner.

    Karar karar ayrı çağrı yapmak yerine bunu kullanın — sunucu metinleri
    kendi içinde (hız sınırına uyarak) toplar ve tek pakette döndürür.
    Önbellek sayesinde tekrarlanan kararlar anında gelir.

    Args:
        aranan_kelime: Aranacak kelime/ifade. Kesin ifade için çift tırnak:
            "elektrik piyasası". Zorunlu.
        daire: Daire/kurul adı (boş = tüm daireler).
        baslangic_tarihi: Karar tarihi aralığı başlangıcı, GG.AA.YYYY.
        bitis_tarihi: Karar tarihi aralığı sonu, GG.AA.YYYY.
        sonuc_sayisi: Künyesi listelenecek karar sayısı (1-100, varsayılan 30).
        metin_sayisi: Metni getirilecek karar sayısı (0-10, varsayılan 5).
            Not: her metin kaynak siteden ~3 sn sürer; 10 metin ≈ 30 sn.
        metin_bicimi: "pencere" = aranan ifadenin geçtiği bölümler (varsayılan,
            önerilen — pakete daha çok karar sığar) | "tam" = kararın tamamı.

    Returns:
        toplam_sonuc, kararlar (künye listesi) ve metinler (id + metin).
    """
    if not aranan_kelime.strip():
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
    metinler = []
    hatalar = []
    for k in arama["kararlar"][:metin_sayisi]:
        try:
            text = await _fetch_doc_text(k["id"])
        except RateLimitedError as e:
            hatalar.append(f"{k['id']}: {e}")
            break
        except Exception as e:  # tek karar hatası araştırmayı durdurmasın
            hatalar.append(f"{k['id']}: {type(e).__name__}")
            continue
        metinler.append({
            "id": k["id"],
            "daire": k["daire"],
            "esas_no": k["esas_no"],
            "karar_no": k["karar_no"],
            "karar_tarihi": k["karar_tarihi"],
            "metin": _keyword_windows(text, aranan_kelime) if metin_bicimi == "pencere" else text,
        })
    out = {
        "toplam_sonuc": arama["toplam_sonuc"],
        "kararlar": arama["kararlar"],
        "metinler": metinler,
        "metin_bicimi": metin_bicimi,
    }
    if hatalar:
        out["hatalar"] = hatalar
    if arama["toplam_sonuc"] > sonuc_sayisi:
        out["not"] = (
            f"Toplam {arama['toplam_sonuc']} sonuçtan ilk {sonuc_sayisi} künye "
            "listelendi. Daraltmak için daire/tarih filtresi veya daha spesifik "
            "ifade kullanın."
        )
    return out


@mcp.tool
async def yargitay_karar_metni(karar_id: str) -> dict:
    """Bir Yargıtay kararının tam metnini getirir.

    Args:
        karar_id: yargitay_karar_ara sonucundaki 'id' değeri.

    Returns:
        Kararın düz metin hâli ve kaynak URL'si.
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
        "kaynak": f"{BASE_URL}/getDokuman?id={karar_id}",
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
