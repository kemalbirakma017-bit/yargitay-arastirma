"""Bedesten/UYAP içtihat kaynağı için yapılandırılabilir HTTP adaptörü.

ÖNEMLİ: Bedesten'in arama ve belge uç noktalarının istek gövdeleri
resmî/dokümante bir API şeması olarak doğrulanmamıştır. Bu nedenle
istek gövdeleri tahmin edilmez; HAR/Network kaydından doğrulanan JSON
şablonları ortam değişkenleriyle verilmelidir.

Gerekli:
  BEDSTEN_SEARCH_PAYLOAD_JSON='{"...":"${query}"}'
  BEDSTEN_CONTENT_PAYLOAD_JSON='{"...":"${document_id}"}'

Şablonlarda ${query} ve ${document_id} yer tutucuları kullanılır.
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


class BedestenConfigurationError(RuntimeError):
    pass


def _load_template(env_name: str, placeholder: str, value: str) -> dict[str, Any]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        raise BedestenConfigurationError(
            f"{env_name} ayarlanmamış. Bedesten tarayıcı Network/HAR kaydından "
            "doğrulanmış JSON istek gövdesi yapılandırılmalıdır; varsayımsal "
            "parametre gönderilmeyecektir."
        )
    try:
        template = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BedestenConfigurationError(f"{env_name} geçerli JSON değil: {exc}") from exc
    if not isinstance(template, dict):
        raise BedestenConfigurationError(f"{env_name} bir JSON nesnesi olmalıdır.")
    serialized = json.dumps(template, ensure_ascii=False)
    if placeholder not in serialized:
        raise BedestenConfigurationError(
            f"{env_name} içinde {placeholder} yer tutucusu bulunmalıdır."
        )
    return json.loads(serialized.replace(placeholder, value))


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Bedesten yanıtında yaygın koleksiyon sarmalayıcılarını bulur."""
    candidates = [payload]
    if isinstance(payload, dict):
        candidates.extend(payload.get(k) for k in ("data", "result", "results", "content"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [x for x in candidate if isinstance(x, dict)]
        if isinstance(candidate, dict):
            for key in ("documents", "items", "records", "content", "data", "results"):
                value = candidate.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
    return []


class BedestenClient:
    def __init__(self) -> None:
        self.base_url = BASE_URL
        self.search_path = SEARCH_PATH
        self.content_path = CONTENT_PATH

    async def search(self, query: str) -> dict[str, Any]:
        body = _load_template("BEDSTEN_SEARCH_PAYLOAD_JSON", "${query}", query)
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
        return {
            "kaynak": "Bedesten",
            "sorgu": query,
            "sonuc_sayisi": len(_extract_items(payload)),
            "sonuclar": _extract_items(payload),
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
