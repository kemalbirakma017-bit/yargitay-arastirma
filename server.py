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


def _validate_date(value: str, name: str) -> None:
    if value and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        raise ValueError(f"{name} GG.AA.YYYY formatında olmalıdır (örn. 01.01.2024).")


@mcp.tool
async def yargitay_daire_listesi() -> dict:
    """Yargıtay karar aramasında kullanılabilecek geçerli daire/kurul adlarını döndürür.

    yargitay_karar_ara aracının 'daire' parametresine bu listeden bir değer verin.
    """
    return {"daireler": DAIRELER}


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
    payload = await _request("GET", "/getDokuman", params={"id": karar_id})
    html = (payload or {}).get("data") or ""
    if not html:
        return {
            "id": karar_id,
            "metin": "",
            "uyari": "Belge boş döndü; id geçersiz veya belge kaldırılmış olabilir.",
        }
    return {
        "id": karar_id,
        "kaynak": f"{BASE_URL}/getDokuman?id={karar_id}",
        "metin": _html_to_text(html),
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
