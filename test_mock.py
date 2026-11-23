"""Mock Bedesten contract and backwards-compatibility tests."""
import asyncio
import base64
import inspect
import json

import httpx
import server


HTML = "<html><body><h2>T.C. YARGITAY</h2><p>Hukuk Genel Kurulu</p><p>Karar metni...</p></body></html>"
HTML_B64 = base64.b64encode(HTML.encode("utf-8")).decode("ascii")
captured = []


def mock_transport(request: httpx.Request) -> httpx.Response:
    body = request.content.decode("utf-8") if request.content else ""
    captured.append((request.method, request.url.path, body))
    if request.url.path == "/emsal-karar/searchDocuments":
        return httpx.Response(200, json={"data": {
            "total": 12206,
            "emsalKararList": [{
                "documentId": "1024709700",
                "birimAdi": "Hukuk Genel Kurulu",
                "esasNo": "2023/456",
                "kararNo": "2026/123",
                "kararTarihiStr": "20.05.2026",
                "kesinlesmeDurumu": "Kesinleşti",
            }],
        }})
    if request.url.path == "/emsal-karar/getDocumentContent":
        return httpx.Response(200, json={"data": {
            "content": HTML_B64, "mimeType": "text/html",
        }})
    return httpx.Response(404, json={"error": "unexpected endpoint"})


async def main() -> None:
    server._pacer.min_interval = 0
    server._doc_cache.clear()
    server._client = httpx.AsyncClient(
        base_url=server.BEDESTEN_BASE, headers=server.HEADERS,
        transport=httpx.MockTransport(mock_transport),
    )

    result = await server._do_search(
        aranan_kelime="tazminat", daire="Hukuk Genel Kurulu",
        baslangic_tarihi="01.01.2024", bitis_tarihi="31.12.2024",
        sayfa=2, sayfa_boyutu=20, esas_yil="2023", esas_sira_no="456",
        karar_yil="2026", karar_sira_no="123",
    )
    assert result["toplam_sonuc"] == 12206
    record = result["kararlar"][0]
    assert record["id"] == "1024709700"
    assert record["esas_no"] == "2023/456"
    assert record["karar_no"] == "2026/123"
    assert record["karar_tarihi"] == "20.05.2026"
    assert record["kaynak"].endswith("/1024709700")
    sent = json.loads(captured[-1][2])
    data = sent["data"]
    assert sent["paging"] is True
    assert data["phrase"] == "tazminat"
    assert data["birimAdi"] == "Hukuk Genel Kurulu"
    assert data["kararTarihiStart"] == "2024-01-01T00:00:00.000Z"
    assert data["kararTarihiEnd"] == "2024-12-31T23:59:59.000Z"
    assert data["esasNoYil"] == "2023" and data["esasNoSira"] == "456"
    assert data["kararNoYil"] == "2026" and data["kararNoSira"] == "123"
    assert data["pageNumber"] == 2 and data["pageSize"] == 20

    text = await server._fetch_doc_text("1024709700")
    assert "YARGITAY" in text and "Karar metni" in text
    content_request = json.loads(captured[-1][2])
    assert content_request["data"]["documentId"] == "1024709700"
    decision = await server.yargitay_karar_metni("1024709700")
    assert decision["id"] == "1024709700"
    assert decision["metin"] == text and decision["kaynak"].endswith("/1024709700")

    # Existing public tool names and argument names remain available.
    params = inspect.signature(server.yargitay_karar_ara).parameters
    for name in (
        "esas_yil", "esas_ilk_sira_no", "esas_son_sira_no",
        "karar_yil", "karar_ilk_sira_no", "karar_son_sira_no",
    ):
        assert name in params, name
    assert callable(server.yargitay_karar_ara)
    assert callable(server.yargitay_karar_metni)

    # Old ALL and kurul spelling are accepted; end-of-range args stay accepted.
    await server._do_search(daire="ALL")
    assert "birimAdi" not in json.loads(captured[-1][2])["data"]
    await server._do_search(daire="Büyük Genel Kurulu")
    assert json.loads(captured[-1][2])["data"]["birimAdi"] == "Büyük Genel Kurul"

    # Null totals remain safe and preserve the established output keys.
    async def null_search(*args, **kwargs):
        return {"toplam_sonuc": None, "kararlar": []}
    original = server._do_search
    server._do_search = null_search
    try:
        bulk = await server._toplu_impl("tazminat", metin_sayisi=0)
        assert bulk["toplam_sonuc"] is None
        assert bulk["metinler"] == [] and bulk["metin_bicimi"] == "pencere"
    finally:
        server._do_search = original

    await server._client.aclose()
    print("ALL TESTS PASSED")


asyncio.run(main())

