"""Bedesten (Adalet Bakanlığı) emsal karar arama adaptörü.

Arama payload şeması, kamuya açık istemci uygulamalarında görülen
Bedesten sözleşmesine dayanır; resmî API dokümantasyonu değildir.
İstek yanıtı canlı ortamda doğrulanmadan sonuçlar hukuki kaynak olarak
kabul edilmemelidir. Belge içeriği isteği ortam değişkeniyle verilir.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urljoin

import httpx

BASE_URL = os.getenv("BEDSTEN_BASE_URL", "https://bedesten.adalet.gov.tr").rstrip("/")
SEARCH_PATH = os.getenv("BEDSTEN_SEARCH_PATH", "/emsal-karar/searchDocuments")
CONTENT_PATH = os.getenv("BEDSTEN_CONTENT_PATH", "/emsal-karar/getDocumentContent")
TIMEOUT = float(os.getenv("BEDSTEN_TIMEOUT", "30"))

# Kamuya açık istemci örneklerinde görülen arama gövdesi. Bu, Adalet
# Bakanlığınca yayımlanmış resmî bir API sözleşmesi değildir.
_DEFAULT_SEARCH_TEMPLATE = {
    "data": {
        "pageSize": 20,
        "pageNumber": 1,
        "itemTypeList": ["YARGITAYKARARI"],
        "phrase": "${query}",
        "sortFields": ["KARAR_TARIHI"],
        "sortDirection": "desc",
    },
    "applicationName": "UyapMevzuat",
    "paging": True,
}


class BedestenConfigurationError(RuntimeError):
    pass


def _replace_placeholder(value: Any, placeholder: str, replacement: str) -> Any:
    """JSON nesnesi içinde yer tutucuyu güvenli biçimde değiştirir."""
    if isinstance(value, dict):
        return {k: _replace_placeholder(v, placeholder, replacement) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_placeholder(v, placeholder, replacement) for v in value]
    if isinstance(value, str):
        return value.replace(placeholder, replacement)
    return value


def _load_template(env_name: str, placeholder: str, value: str,
                   default: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = os.getenv(env_name, "").strip()
    if raw:
        try:
            template = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BedestenConfigurationError(f"{env_name} geçerli JSON değil: {exc}") from exc
    elif default is not None:
        template = default
    else:
        raise BedestenConfigurationError(
            f"{env_name} ayarlanmamış. Belge içeriği isteğinin JSON gövdesi "
            "tarayıcı Network/HAR kaydından doğrulanarak tanımlanmalıdır."
        )
    if not isinstance(template, dict):
        raise BedestenConfigurationError(f"{env_name} bir JSON nesnesi olmalıdır.")
    serialized = json.dumps(template, ensure_ascii=False)
    if placeholder not in serialized:
        raise BedestenConfigurationError(
            f"{env_name} içinde {placeholder} yer tutucusu bulunmalıdır."
        )
    return _replace_placeholder(template, placeholder, value)


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Yalnızca bilinen karar koleksiyonu alanlarını, sabit öncelikle okur.

    Genel amaçlı 'content', 'data' veya rastgele ilk listeyi sonuç saymak,
    yanıtın başka bir bölümünü karar listesi gibi göstermeye yol açabilir.
    Gerçek Bedesten yanıtı görülene kadar belirsiz yapılar boş sonuç döndürür.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    collection_keys = ("emsalKararList", "documents", "items", "records", "results")

    # Bazı cevaplarda koleksiyon kökte, bazılarında data/result nesnesindedir.
    containers = [payload]
    for wrapper in ("data", "result", "results"):
        nested = payload.get(wrapper)
        if isinstance(nested, dict):
            containers.append(nested)

    for container in containers:
        for key in collection_keys:
            value = container.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_total(payload: Any, items: list[dict[str, Any]]) -> int:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("total", "totalElements", "recordsTotal", "totalCount"):
                value = data.get(key)
                if isinstance(value, int):
                    return value
    return len(items)


class BedestenClient:
    def __init__(self) -> None:
        self.base_url = BASE_URL
        self.search_path = SEARCH_PATH
        self.content_path = CONTENT_PATH

    async def search(self, query: str) -> dict[str, Any]:
        body = _load_template(
            "BEDSTEN_SEARCH_PAYLOAD_JSON", "${query}", query,
            default=_DEFAULT_SEARCH_TEMPLATE,
        )
        url = urljoin(self.base_url + "/", self.search_path.lstrip("/"))
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.post(url, json=body, headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Bedesten arama yanıtı JSON değil (HTTP {response.status_code})."
                ) from exc
        items = _extract_items(payload)
        return {
            "kaynak": "Bedesten",
            "sorgu": query,
            "toplam_sonuc": _extract_total(payload, items),
            "sonuc_sayisi": len(items),
            "sonuclar": items,
            "ham_yanit": payload,
            "kaynak_url": url,
        }

    async def document(self, document_id: str) -> dict[str, Any]:
        body = _load_template(
            "BEDSTEN_CONTENT_PAYLOAD_JSON", "${document_id}", document_id
        )
        url = urljoin(self.base_url + "/", self.content_path.lstrip("/"))
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.post(url, json=body, headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {"content": response.text}
        return {
            "kaynak": "Bedesten",
            "belge_id": document_id,
            "kaynak_url": url,
            "yanit": payload,
        }
