"""Bedesten arama ve tam metin sözleşmesi için fixture/mock regresyon testleri.

Bu testler gerçek Adalet Bakanlığı servisine ağ isteği göndermez.
"""
import asyncio
import base64
import json

import httpx

from mevzuat_kaynak import BedestenClient, BedestenResponseError


SEARCH_PAYLOAD = {
    "data": {
        "emsalKararList": [{
            "documentId": "12345678901",
            "birimAdi": "Hukuk Genel Kurulu",
            "esasNo": "2024/4174",
            "kararNo": "2025/3748",
            "kararTarihiStr": "09.09.2025",
            "kesinlesmeDurumu": "Kesinleşti",
        }],
        "total": 1,
    },
    "metadata": {"FMTY": "SUCCESS"},
}
HTML = "<html><body><h2>T.C. YARGITAY</h2><p>Hukuk Genel Kurulu</p><p>Gerekçeli karar metni.</p></body></html>"
CONTENT_PAYLOAD = {
    "data": {
        "content": base64.b64encode(HTML.encode("utf-8")).decode("ascii"),
        "mimeType": "text/html",
    },
    "metadata": {"FMTY": "SUCCESS"},
}


def make_transport(captured):
    def handler(request: httpx.Request):
        body = request.content.decode("utf-8") if request.content else ""
        captured.append((request.method, request.url.path, json.loads(body), dict(request.headers)))
        if request.url.path.endswith("/searchDocuments"):
            return httpx.Response(200, json=SEARCH_PAYLOAD)
        if request.url.path.endswith("/getDocumentContent"):
            return httpx.Response(200, json=CONTENT_PAYLOAD)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def run():
    captured = []
    client = BedestenClient(transport=make_transport(captured), min_interval=0)

    search = await client.search("tazminat")
    assert search["toplam_sonuc"] == 1
    assert search["sonuc_sayisi"] == 1
    result = search["sonuclar"][0]
    assert result["id"] == "12345678901"
    assert result["daire"] == "Hukuk Genel Kurulu"
    assert result["esas_no"] == "2024/4174"
    assert result["karar_no"] == "2025/3748"
    assert result["karar_tarihi"] == "09.09.2025"
    method, path, body, headers = captured[0]
    assert method == "POST" and path.endswith("/searchDocuments")
    assert body["data"]["phrase"] == "tazminat"
    assert body["data"]["itemTypeList"] == ["YARGITAYKARARI"]
    assert body["data"]["pageSize"] == 20 and body["data"]["pageNumber"] == 1
    assert body["paging"] is True
    assert "applicationName" not in body
    assert "Mozilla" not in headers.get("user-agent", "")

    document = await client.document("12345678901")
    assert document["id"] == "12345678901"
    assert "T.C. YARGITAY" in document["metin"]
    assert "Gerekçeli karar metni." in document["metin"]
    method, path, body, _ = captured[1]
    assert method == "POST" and path.endswith("/getDocumentContent")
    assert body == {"data": {"documentId": "12345678901"}}

    print("PASS: Bedesten arama payload ve endpoint")
    print("PASS: Bedesten künye normalizasyonu")
    print("PASS: Bedesten tam metin payload ve Base64 HTML çözümü")
    print("PASS: Karar/belge kimliği ve esas/karar numaraları korunuyor")


async def error_cases():
    def error_handler(request):
        return httpx.Response(200, json={
            "metadata": {"FMTY": "ERROR", "FMTE": "Invalid document id"},
            "data": {},
        })
    client = BedestenClient(transport=httpx.MockTransport(error_handler), min_interval=0)
    try:
        await client.search("tazminat")
    except BedestenResponseError as exc:
        assert "Invalid document id" in str(exc)
    else:
        raise AssertionError("Bedesten uygulama hatası yakalanmadı")

    def bad_content(request):
        return httpx.Response(200, json={"data": {"content": "not-base64", "mimeType": "text/html"}})
    client = BedestenClient(transport=httpx.MockTransport(bad_content), min_interval=0)
    try:
        await client.document("x")
    except BedestenResponseError:
        pass
    else:
        raise AssertionError("Geçersiz Base64 yanıtı yakalanmadı")

    print("PASS: Bedesten uygulama hatası ve bozuk tam metin yanıtı")


if __name__ == "__main__":
    asyncio.run(run())
    asyncio.run(error_cases())
    print("ALL BEDSTEN CONTRACT TESTS PASSED")
