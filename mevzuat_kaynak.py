"""Adalet Bakanlığı Bedesten emsal karar istemcisi.

İstek/yanıt alanları kamuya açık bağımsız bir istemcinin kaynak kodunda
görülen Bedesten sözleşmesine göre eşlenmiştir. Resmî geliştirici API
dokümantasyonu değildir; canlı ortam doğrulaması ayrıca gereklidir.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, quote

import httpx
from bs4 import BeautifulSoup

BASE_URL = os.getenv("BEDSTEN_BASE_URL", "https://bedesten.adalet.gov.tr").rstrip("/")
SEARCH_PATH = os.getenv("BEDSTEN_SEARCH_PATH", "/emsal-karar/searchDocuments")
CONTENT_PATH = os.getenv("BEDSTEN_CONTENT_PATH", "/emsal-karar/getDocumentContent")
PUBLIC_DECISION_URL = os.getenv("BEDSTEN_PUBLIC_DECISION_URL", "https://mevzuat.adalet.gov.tr/ictihat").rstrip("/")
TIMEOUT = float(os.getenv("BEDSTEN_TIMEOUT", "30"))
MIN_INTERVAL = float(os.getenv("BEDSTEN_MIN_INTERVAL", "1.0"))
BLOCK_SECONDS = float(os.getenv("BEDSTEN_BLOCK_SECONDS", "120"))

_DEFAULT_SEARCH_TEMPLATE = {
    "data": {
        "phrase": "${query}",
        "itemTypeList": ["YARGITAYKARARI"],
        "pageSize": 20,
        "pageNumber": 1,
    },
    "paging": True,
}
_DEFAULT_CONTENT_TEMPLATE = {
    "data": {
        "documentId": "${document_id}",
    },
}


class BedestenConfigurationError(RuntimeError):
    """Eksik veya geçersiz Bedesten yapılandırması."""


class BedestenAccessBlocked(RuntimeError):
    """Bedesten erişimi reddetti veya hız sınırı uyguladı."""


class BedestenResponseError(RuntimeError):
    """Bedesten beklenmeyen veya çözümlenemeyen yanıt döndürdü."""


def _replace_placeholder(value: Any, placeholder: str, replacement: str) -> Any:
    if isinstance(value, dict):
        return {k: _replace_placeholder(v, placeholder, replacement) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_placeholder(v, placeholder, replacement) for v in value]
    if isinstance(value, str):
        return value.replace(placeholder, replacement)
    return value


def _load_template(
    env_name: str,
    placeholder: str,
    value: str,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = os.getenv(env_name, "").strip()
    if raw:
        try:
            template = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BedestenConfigurationError(f"{env_name} geçerli JSON değil.") from exc
    elif default is not None:
        template = default
    else:
        raise BedestenConfigurationError(f"{env_name} yapılandırılmamış.")
    if not isinstance(template, dict):
        raise BedestenConfigurationError(f"{env_name} bir JSON nesnesi olmalıdır.")
    if placeholder not in json.dumps(template, ensure_ascii=False):
        raise BedestenConfigurationError(f"{env_name} içinde {placeholder} yer tutucusu bulunmalıdır.")
    return _replace_placeholder(template, placeholder, value)


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Yalnızca Bedesten'in emsalKararList alanını karar listesi kabul eder."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("emsalKararList")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _extract_total(payload: Any, items: list[dict[str, Any]]) -> int:
    data = payload.get("data") if isinstance(payload, dict) else None
    total = data.get("total") if isinstance(data, dict) else None
    try:
        return int(total) if total is not None else len(items)
    except (TypeError, ValueError):
        return len(items)


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
        tag.append("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode_document_content(content: str, mime_type: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise BedestenResponseError("Bedesten belge yanıtında content alanı boş.")
    if "html" not in (mime_type or "text/html").lower() and "text/plain" not in (mime_type or "").lower():
        raise BedestenResponseError(f"Desteklenmeyen karar metni türü: {mime_type or 'bilinmiyor'}.")
    compact = re.sub(r"\s+", "", content)
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error):
        raise BedestenResponseError("Bedesten content alanı geçerli Base64 değil.") from None
    text = decoded.decode("utf-8", errors="replace")
    return _html_to_text(text) if "html" in (mime_type or "text/html").lower() else text.strip()


class BedestenClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float | None = None,
        base_url: str | None = None,
    ) -> None:
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.search_path = SEARCH_PATH
        self.content_path = CONTENT_PATH
        self._transport = transport
        self._min_interval = MIN_INTERVAL if min_interval is None else max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._blocked_until = 0.0
        self._blocked_message = ""

    async def _wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                remaining = max(1, int(self._blocked_until - now))
                raise BedestenAccessBlocked(
                    f"Bedesten erişimi geçici olarak durduruldu; yaklaşık {remaining} saniye bekleyin."
                )
            delay = self._last_request + self._min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    def _block(self, seconds: float, message: str) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + max(0.0, seconds))
        self._blocked_message = message

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        await self._wait_turn()
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "yargitay-arastirma-mcp/0.1 (+https://github.com/kemalbirakma017-bit/yargitay-arastirma)",
        }
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            try:
                response = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise BedestenResponseError(
                    f"Bedesten bağlantı hatası: {type(exc).__name__}."
                ) from None

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else BLOCK_SECONDS
            self._block(wait, "HTTP 429")
            raise BedestenAccessBlocked(
                f"Bedesten hız sınırı uyguladı (HTTP 429). {int(wait)} saniye sonra yeniden deneyin."
            )
        if response.status_code == 403:
            self._block(BLOCK_SECONDS, "HTTP 403")
            raise BedestenAccessBlocked(
                "Bedesten isteği reddetti (HTTP 403). Erişim engeli aşılmaya çalışılmadı."
            )
        if 300 <= response.status_code < 400:
            self._block(BLOCK_SECONDS, "redirect")
            raise BedestenAccessBlocked(
                f"Bedesten beklenen JSON yerine yönlendirme döndürdü (HTTP {response.status_code})."
            )
        if not response.is_success:
            raise BedestenResponseError(f"Bedesten HTTP {response.status_code} yanıtı verdi.")

        try:
            payload = response.json()
        except ValueError:
            raise BedestenResponseError("Bedesten JSON yerine farklı bir yanıt döndürdü.") from None
        if not isinstance(payload, dict):
            raise BedestenResponseError("Bedesten yanıtının kök değeri JSON nesnesi değil.")
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("FMTY") == "ERROR":
            message = metadata.get("FMTE") or "Bedesten isteği uygulama hatası döndürdü."
            raise BedestenResponseError(str(message))
        return payload

    async def search(
        self,
        query: str,
        page_size: int = 20,
        page_number: int = 1,
    ) -> dict[str, Any]:
        if not query or not query.strip():
            raise ValueError("Bedesten arama ifadesi boş olamaz.")
        page_size = min(50, max(1, int(page_size)))
        page_number = max(1, int(page_number))
        body = _load_template(
            "BEDSTEN_SEARCH_PAYLOAD_JSON",
            "${query}",
            query,
            default=_DEFAULT_SEARCH_TEMPLATE,
        )
        data = body.setdefault("data", {})
        if not isinstance(data, dict):
            raise BedestenConfigurationError("Bedesten arama şablonundaki data alanı nesne olmalıdır.")
        data["pageSize"] = page_size
        data["pageNumber"] = page_number

        url = urljoin(self.base_url + "/", self.search_path.lstrip("/"))
        payload = await self._post(self.search_path, body)
        items = _extract_items(payload)
        results = []
        for item in items:
            document_id = str(item.get("documentId") or "").strip()
            if not document_id:
                continue
            chamber = item.get("birimAdi") or ""
            docket = item.get("esasNo") or ""
            decision = item.get("kararNo") or ""
            date = item.get("kararTarihiStr") or ""
            results.append({
                "id": document_id,
                "document_id": document_id,
                "daire": chamber,
                "esas_no": docket,
                "karar_no": decision,
                "karar_tarihi": date,
                "kesinlesme_durumu": item.get("kesinlesmeDurumu"),
                "atif": f"{chamber}, E.{docket} K.{decision}, T.{date}".strip(", "),
                "kaynak": f"{PUBLIC_DECISION_URL}/{quote(document_id, safe='')}",
            })
        return {
            "kaynak": "Bedesten",
            "sorgu": query,
            "sayfa": page_number,
            "sayfa_boyutu": page_size,
            "toplam_sonuc": _extract_total(payload, items),
            "sonuc_sayisi": len(results),
            "sonuclar": results,
            "kaynak_url": url,
        }

    async def document(self, document_id: str) -> dict[str, Any]:
        document_id = str(document_id).strip()
        if not document_id:
            raise ValueError("Bedesten belge kimliği boş olamaz.")
        body = _load_template(
            "BEDSTEN_CONTENT_PAYLOAD_JSON",
            "${document_id}",
            document_id,
            default=_DEFAULT_CONTENT_TEMPLATE,
        )
        payload = await self._post(self.content_path, body)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BedestenResponseError("Bedesten tam metin yanıtında data nesnesi bulunamadı.")
        mime_type = str(data.get("mimeType") or "text/html")
        text = _decode_document_content(data.get("content") or "", mime_type)
        return {
            "kaynak": "Bedesten",
            "belge_id": document_id,
            "id": document_id,
            "mime_type": mime_type,
            "uzunluk": len(text),
            "metin": text,
            "kaynak_url": f"{PUBLIC_DECISION_URL}/{quote(document_id, safe='')}",
        }
}