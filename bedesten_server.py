"""
UYAP Mevzuat ve İçtihat / Bedesten üzerinden Yargıtay karar araştırması.
FastMCP Cloud entrypoint: bedesten_server.py:mcp
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP

SEARCH_URL = "https://bedesten.adalet.gov.tr/emsal-karar"
PUBLIC_DECISION_URL = "https://mevzuat.adalet.gov.tr/ictihat"
MIN_INTERVAL = max(0.0, float(os.getenv("YARGITAY_MIN_INTERVAL", "2.0")))
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=UTF-8",
    "User-Agent": "UYAP-Yargitay-MCP/2.2 (decision research client)",
    "AdaletApplicationName": "UyapMevzuat",
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
}

mcp = FastMCP(
    name="UYAP Mevzuat ve İçtihat - Yargıtay Bedesten",
    instructions=(
        "Yalnızca Adalet Bakanlığı UYAP Mevzuat ve İçtihat Bedesten kaynağında "
        "arama yapar. Hukuki içtihat araştırmasında önce Hukuk Genel Kurulu ve "
        "Ceza Genel Kurulu, ardından ilgili Yargıtay dairesi araştırılmalıdır. "
        "Her bulunan kararın tam metnini getirmeyi dene. Tam metin alınamazsa "
        "bunu açıkça belirt; karar içeriği veya künye uydurma. Tarih ve esas/"
        "karar numaralarını kaynak yanıtından aktar."
    ),
)

UNIT_CODES: dict[str, int] = {
    "HGK": 1011520, "CGK": 1011518,
    "H1": 1010563, "H2": 1010564, "H3": 1010565, "H4": 1010566,
    "H5": 1010567, "H6": 1010568, "H7": 1010569, "H8": 1010570,
    "H9": 1010571, "H10": 1010572, "H11": 1010573, "H12": 1010574,
    "H13": 1010575, "H14": 1010576, "H15": 1010577, "H16": 1010578,
    "H17": 1010579, "H18": 1010580, "H19": 1010581, "H20": 1010582,
    "H21": 1010583, "H22": 1033913, "H23": 1033925,
    "C1": 1010584, "C2": 1010585, "C3": 1010586, "C4": 1010587,
    "C5": 1010588, "C6": 1010589, "C7": 1010590, "C8": 1010591,
    "C9": 1010592, "C10": 1010593, "C11": 1010594, "C12": 1033937,
    "C13": 1033949, "C14": 1033961, "C15": 1033973, "C16": 1042778,
    "C17": 1042779, "C18": 1042780, "C19": 1042781, "C20": 1042782,
    "C21": 1042783, "C22": 1042784, "C23": 1042785,
}
UNIT_LABELS = {"HGK": "Hukuk Genel Kurulu", "CGK": "Ceza Genel Kurulu"}
DAIRELER = [
    "Hukuk Genel Kurulu", "Ceza Genel Kurulu",
    *[f"{i}. Hukuk Dairesi" for i in range(1, 24)],
    *[f"{i}. Ceza Dairesi" for i in range(1, 24)],
]

# UYAP Mevzuat web arayüzünün searchDocuments itemTypeList değerleri.
KARAR_TURLERI: dict[str, str] = {
    "Yargıtay Kararı": "YARGITAYKARARI",
    "Danıştay Kararı": "DANISTAYKARAR",
    "Yerel Hukuk Mahkemesi Kararı": "YERELHUKUK",
    "İstinaf Hukuk Mahkemesi Kararı": "ISTINAFHUKUK",
    "Kanun Yararına Bozma Kararları": "KYB",
}

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()
_throttle_lock = asyncio.Lock()
_last_request = 0.0
_doc_cache: dict[str, str] = {}
_CACHE_MAX = 300


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        return _client


async def _pace() -> None:
    global _last_request
    async with _throttle_lock:
        now = asyncio.get_running_loop().time()
        wait = MIN_INTERVAL - (now - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = asyncio.get_running_loop().time()


async def _post(path: str, data: dict[str, Any], *, paging: bool = False,
                timeout: float | None = None) -> dict[str, Any]:
    await _pace()
    client = await _get_client()
    body: dict[str, Any] = {"data": data, "applicationName": "UyapMevzuat"}
    if paging:
        body["paging"] = True
    try:
        response = await client.post(
            f"{SEARCH_URL}{path}", json=body,
            timeout=timeout if timeout is not None else TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Bedesten {path} isteği zaman aşımına uğradı.") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Bedesten bağlantı hatası: {exc}") from exc

    if response.status_code == 429:
        raise RuntimeError("Bedesten erişim sınırı (HTTP 429). Bir süre bekleyip yeniden deneyin.")
    if not response.is_success:
        raise RuntimeError(f"Bedesten HTTP {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Bedesten JSON yerine farklı yanıt verdi: {response.text[:300]}") from exc
    metadata = payload.get("metadata") or {}
    if metadata.get("FMTY") == "ERROR":
        raise RuntimeError(f"Bedesten: {metadata.get('FMTE') or 'bilinmeyen hata'}")
    data_out = payload.get("data") or {}
    if not isinstance(data_out, dict):
        raise RuntimeError("Bedesten yanıtının data alanı beklenen biçimde değil.")
    return data_out


def _clean_phrase(value: str) -> str:
    phrase = str(value or "").strip()
    phrase = re.sub(r"[^\w\s\"'()+\-]", " ", phrase, flags=re.UNICODE)
    return " ".join(phrase.split())


def _unit_filter(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw:
        return "", ""
    if raw.casefold() == "hukuk genel kurulu".casefold():
        code = "HGK"
    elif raw.casefold() == "ceza genel kurulu".casefold():
        code = "CGK"
    else:
        hm = re.fullmatch(r"(\d+)\.\s*Hukuk Dairesi", raw, flags=re.IGNORECASE)
        cm = re.fullmatch(r"(\d+)\.\s*Ceza Dairesi", raw, flags=re.IGNORECASE)
        code = f"H{hm.group(1)}" if hm else f"C{cm.group(1)}" if cm else raw.upper()
    if code not in UNIT_CODES:
        raise ValueError("Tanınmayan daire/kurul. HGK, CGK, 1-23. Hukuk veya 1-23. Ceza Dairesi kullanın.")
    label = UNIT_LABELS.get(code)
    if not label:
        label = f"{int(code[1:])}. {'Hukuk' if code.startswith('H') else 'Ceza'} Dairesi"
    return code, label


def _number(value: str, label: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if not re.fullmatch(r"\d+", str(value).strip()):
        raise ValueError(f"{label} sayısal olmalıdır.")
    return int(value)


def _date_value(value: str, end: bool = False) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Tarihler YYYY-AA-GG biçiminde olmalıdır.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Geçersiz takvim tarihi. Tarih YYYY-AA-GG biçiminde ve gerçek bir tarih olmalıdır.") from exc
    return value + ("T23:59:59.999Z" if end else "T00:00:00.000Z")


def _base_query(args: dict[str, Any]) -> dict[str, Any]:
    phrase = _clean_phrase(args.get("aranan_kelime", ""))
    if not phrase:
        raise ValueError("Aranacak kelime/ifade boş olamaz.")
    karar_turu = str(args.get("karar_turu") or "Yargıtay Kararı").strip()
    item_type = KARAR_TURLERI.get(karar_turu)
    if item_type is None:
        raise ValueError(
            "Tanınmayan karar_turu. Şunlardan birini kullanın: "
            + ", ".join(KARAR_TURLERI)
        )
    if karar_turu != "Yargıtay Kararı" and str(args.get("daire") or "").strip():
        raise ValueError("daire filtresi yalnızca Yargıtay Kararı aramalarında kullanılabilir.")
    siralama = str(args.get("siralama") or "alaka").strip()
    if siralama not in {"alaka", "tarih_yeniden_eskiye"}:
        raise ValueError("siralama 'alaka' veya 'tarih_yeniden_eskiye' olmalıdır.")

    base: dict[str, Any] = {
        "phrase": phrase,
        "itemTypeList": [item_type],
    }
    # Bedesten'in varsayılan alaka sıralamasını koru; tarih seçilirse
    # sıralama alanlarını açıkça gönder.
    if siralama == "tarih_yeniden_eskiye":
        base["sortFields"] = ["KARAR_TARIHI"]
        base["sortDirection"] = "desc"

    _, unit_label = _unit_filter(str(args.get("daire") or ""))
    if unit_label:
        # Bedesten must narrow results before paging; local post-filtering can miss
        # sparse units such as the Criminal General Assembly.
        base["birimAdi"] = unit_label
    for key, field, label in (
        ("esas_yil", "esasNoYil", "Esas yılı"),
        ("karar_yil", "kararNoYil", "Karar yılı"),
    ):
        n = _number(args.get(key), label)
        if n is not None:
            base[field] = n

    ei = _number(args.get("esas_ilk_sira_no"), "Esas sıra numarası")
    es = _number(args.get("esas_son_sira_no"), "Esas son sıra numarası")
    ki = _number(args.get("karar_ilk_sira_no"), "Karar sıra numarası")
    ks = _number(args.get("karar_son_sira_no"), "Karar son sıra numarası")
    if ei is not None and es is not None and ei != es:
        raise ValueError("Bedesten uç noktasında esas sıra aralığı doğrulanamadı; tek sıra numarası filtrelenebilir.")
    if ki is not None and ks is not None and ki != ks:
        raise ValueError("Bedesten uç noktasında karar sıra aralığı doğrulanamadı; tek sıra numarası filtrelenebilir.")
    if ei is not None:
        base["esasNoSira"] = ei
    if ki is not None:
        base["kararNoSira"] = ki

    start = _date_value(args.get("baslangic_tarihi", ""))
    end = _date_value(args.get("bitis_tarihi", ""), end=True)
    if start:
        base["kararTarihiStart"] = start
    if end:
        base["kararTarihiEnd"] = end
    return base


def _decision_row(item: dict[str, Any]) -> dict[str, Any]:
    document_id = str(item.get("documentId") or "")
    return {
        "id": f"yargitay:{document_id}",
        "daire": item.get("birimAdi") or item.get("birimAd") or item.get("daire"),
        "esas_no": item.get("esasNo"),
        "karar_no": item.get("kararNo"),
        "karar_tarihi": item.get("kararTarihiStr") or item.get("kararTarihi"),
        "kaynak": f"{PUBLIC_DECISION_URL}/{document_id}" if document_id else None,
        "kesinlesme_durumu": item.get("kesinlesmeDurumu"),
    }


async def _search(args: dict[str, Any]) -> dict[str, Any]:
    base = _base_query(args)
    _, target = _unit_filter(str(args.get("daire") or ""))
    page_size = min(50, max(1, int(args.get("sayfa_boyutu") or args.get("sonuc_sayisi") or 10)))
    page = max(1, int(args.get("sayfa") or 1))

    result = await _post(
        "/searchDocuments",
        {**base, "pageSize": page_size, "pageNumber": page},
        paging=True,
    )
    items = result.get("emsalKararList")
    if not isinstance(items, list):
        raise RuntimeError("Bedesten arama yanıtında emsalKararList alanı bulunamadı.")

    response: dict[str, Any] = {
        "kaynak_sistemi": "Adalet Bakanlığı UYAP Mevzuat ve İçtihat / Bedesten",
        "karar_turu": str(args.get("karar_turu") or "Yargıtay Kararı"),
        "siralama": str(args.get("siralama") or "alaka"),
        "toplam_sonuc": int(result.get("total") or 0),
        "sayfa": page,
        "kararlar": [_decision_row(x) for x in items],
    }
    if target:
        response["filtreleme"] = {
            "daire": target,
            "sunucu_tarafinda": True,
            "bedesten_parametresi": "birimAdi",
        }
    return response


def _html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


async def _get_document(decision_id: str) -> str:
    raw_id = re.sub(r"^yargitay:", "", str(decision_id).strip(), flags=re.IGNORECASE)
    if not re.fullmatch(r"\d{1,30}", raw_id):
        raise ValueError("Karar kimliği sayısal olmalıdır; arama sonucundaki id değerini kullanın.")
    if raw_id in _doc_cache:
        return _doc_cache[raw_id]
    result = await _post(
        "/getDocumentContent", {"documentId": raw_id}, timeout=15.0
    )
    encoded = result.get("content")
    if not encoded:
        raise RuntimeError("Bedesten kararın tam metnini döndürmedi.")
    try:
        html = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError("Bedesten karar içeriği Base64/UTF-8 olarak çözümlenemedi.") from exc
    mime = str(result.get("mimeType") or "text/html").lower()
    if "html" not in mime:
        raise RuntimeError(f"Beklenmeyen karar içeriği türü: {mime}")
    text = _html_to_text(html)
    if len(text) < 100:
        raise RuntimeError("Karar metni boş veya eksik döndü.")
    if len(_doc_cache) >= _CACHE_MAX:
        _doc_cache.pop(next(iter(_doc_cache)))
    _doc_cache[raw_id] = text
    return text


@mcp.tool
async def yargitay_daire_listesi() -> dict[str, Any]:
    """UYAP Bedesten'de arama yapılabilen Yargıtay genel kurulları ve daireleri."""
    return {"daireler": DAIRELER, "kaynak": "UYAP Bedesten"}


@mcp.tool
async def yargitay_karar_ara(
    aranan_kelime: str,
    karar_turu: str = "Yargıtay Kararı",
    siralama: str = "alaka",
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
) -> dict[str, Any]:
    """UYAP Bedesten'de seçilen karar türünde arar; künyeyi ve resmî bağlantıyı döndürür.

    karar_turu: Yargıtay Kararı (varsayılan), Danıştay Kararı,
    Yerel Hukuk Mahkemesi Kararı, İstinaf Hukuk Mahkemesi Kararı veya
    Kanun Yararına Bozma Kararları. siralama 'alaka' (varsayılan) veya
    'tarih_yeniden_eskiye' olabilir. Tarihler YYYY-AA-GG biçimindedir.
    Yalnız Yargıtay aramalarında HGK, CGK veya ör. 3. Hukuk Dairesi için
    daire parametresini kullanın. Tam metin için sonuçtaki id değerini
    yargitay_karar_metni aracına aktarın.
    """
    args = locals()
    return await _search(args)


@mcp.tool
async def yargitay_karar_metni(karar_id: str) -> dict[str, Any]:
    """UYAP Bedesten'den arama sonucundaki karar kimliğiyle tam karar metnini getirir."""
    text = await _get_document(karar_id)
    raw_id = re.sub(r"^yargitay:", "", karar_id.strip(), flags=re.IGNORECASE)
    return {
        "id": f"yargitay:{raw_id}",
        "kaynak": f"{PUBLIC_DECISION_URL}/{raw_id}",
        "uzunluk": len(text),
        "metin": text,
    }


@mcp.tool
async def yargitay_toplu_arastirma(
    aranan_kelime: str,
    karar_turu: str = "Yargıtay Kararı",
    siralama: str = "alaka",
    daire: str = "",
    sonuc_sayisi: int = 20,
    metin_sayisi: int = 1,
    metin_bicimi: str = "pencere",
) -> dict[str, Any]:
    """Bedesten'de arama yapar ve en fazla iki kararın tam metnini aynı çağrıda getirir.

    siralama 'alaka' (varsayılan) veya 'tarih_yeniden_eskiye' seçilebilir.
    metin_bicimi='tam' tam metin; 'pencere' ilk 1800 karakteri döndürür.
    Kaynak hız sınırları nedeniyle metin_sayisi en fazla 2 olabilir.
    """
    if metin_bicimi not in ("tam", "pencere"):
        raise ValueError("metin_bicimi 'tam' veya 'pencere' olmalıdır.")
    count = min(2, max(0, int(metin_sayisi)))
    results = await _search({
        "aranan_kelime": aranan_kelime,
        "karar_turu": karar_turu,
        "siralama": siralama,
        "daire": daire,
        "sayfa_boyutu": min(50, max(1, int(sonuc_sayisi))),
        "sayfa": 1,
    })
    texts: list[dict[str, Any]] = []
    errors: list[str] = []
    for decision in results["kararlar"][:count]:
        try:
            full_text = await _get_document(decision["id"])
            texts.append({
                **decision,
                "metin": full_text if metin_bicimi == "tam" else full_text[:1800],
                "metin_tam_mi": True,
                "tam_metin_karakter_sayisi": len(full_text),
            })
        except Exception as exc:
            errors.append(f"{decision['id']}: {type(exc).__name__}: {exc}")
    results["metinler"] = texts
    results["metin_bicimi"] = metin_bicimi
    if errors:
        results["hatalar"] = errors
    return results


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
