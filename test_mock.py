"""MCP katmanı testi — kaynak site bu konteynerden erişilemediği için
canlı doğrulanmış (16.07.2026) yanıt şemalarıyla mock upstream kullanır."""
import asyncio, json
import httpx
import server
from fastmcp import Client

SEARCH_RESP = {
    "data": {
        "recordsTotal": 12206,
        "recordsFiltered": 12206,
        "data": [
            {"id": "1024709700", "daire": "Hukuk Genel Kurulu",
             "esasNo": "2023/456", "kararNo": "2026/123",
             "kararTarihi": "20.05.2026", "arananKelime": "tazminat",
             "index": 1, "siraNo": 1},
        ],
    },
    "metadata": {"FMTY": "SUCCESS"},
}
DOC_RESP = {"data": "<html><body><h2>T.C. YARGITAY</h2><p>Hukuk Genel Kurulu</p>"
                    "<br><p>MAHKEMESİ: Ticaret Mahkemesi</p><p>Karar metni...</p>"
                    "</body></html>", "metadata": {"FMTY": "SUCCESS"}}

captured = []

def mock_transport(request: httpx.Request) -> httpx.Response:
    captured.append((request.method, request.url.path, request.content.decode() if request.content else ""))
    if request.url.path == "/aramadetaylist":
        return httpx.Response(200, json=SEARCH_RESP)
    if request.url.path == "/getDokuman":
        return httpx.Response(200, json=DOC_RESP)
    return httpx.Response(200, text="<html>ana sayfa</html>")

async def main():
    server.MIN_INTERVAL = 0.0
    server._throttle.min_interval = 0.0
    server._client = httpx.AsyncClient(
        base_url=server.BASE_URL, headers=server.HEADERS,
        transport=httpx.MockTransport(mock_transport))

    async with Client(server.mcp) as c:
        tools = await c.list_tools()
        print("TOOLS:", [t.name for t in tools])

        r = await c.call_tool("yargitay_karar_ara", {
            "aranan_kelime": "tazminat", "daire": "Hukuk Genel Kurulu",
            "baslangic_tarihi": "01.01.2024", "bitis_tarihi": "31.12.2024"})
        out = r.data
        assert out["toplam_sonuc"] == 12206, out
        assert out["kararlar"][0]["id"] == "1024709700"
        sent = json.loads([c for c in captured if c[1] == "/aramadetaylist"][0][2])
        assert sent["data"]["aranan"] == "tazminat" and sent["data"]["arananKelime"] == "tazminat"
        assert sent["data"]["birimYrgKurulDaire"] == "Hukuk Genel Kurulu"
        assert "esasYil" not in sent["data"], "boş alan gönderilmemeli"
        print("SEARCH OK:", out["kararlar"][0])

        r2 = await c.call_tool("yargitay_karar_metni", {"karar_id": "1024709700"})
        assert "YARGITAY" in r2.data["metin"] and "MAHKEMESİ" in r2.data["metin"]
        print("DOC OK:", repr(r2.data["metin"][:80]))

        # hata durumları
        try:
            await c.call_tool("yargitay_karar_ara", {"daire": "99. Uydurma Dairesi"})
            raise SystemExit("daire validasyonu çalışmadı")
        except Exception as e:
            print("DAIRE VALIDATION OK")
        try:
            await c.call_tool("yargitay_karar_ara", {"baslangic_tarihi": "2024-01-01"})
            raise SystemExit("tarih validasyonu çalışmadı")
        except Exception:
            print("DATE VALIDATION OK")

        # ALL -> filtre gönderilmez
        captured.clear()
        await c.call_tool("yargitay_karar_ara", {"aranan_kelime": "x", "daire": "ALL"})
        sent = json.loads(captured[-1][2])
        assert "birimYrgKurulDaire" not in sent["data"]
        print("ALL-DAIRE OK")

    print("ALL TESTS PASSED")

asyncio.run(main())


# Bedesten adaptörünün şema yokken tahmini istek göndermediğini doğrula.
async def test_bedesten_requires_verified_templates():
    import os
    from mevzuat_kaynak import BedestenClient, BedestenConfigurationError
    old = os.environ.pop("BEDSTEN_CONTENT_PAYLOAD_JSON", None)
    try:
        try:
            await BedestenClient().document("test-id")
            raise AssertionError("Belge isteği şema olmadan gönderilmemeli")
        except BedestenConfigurationError:
            pass
    finally:
        if old is not None:
            os.environ["BEDSTEN_CONTENT_PAYLOAD_JSON"] = old



# Bedesten payload ve sonuç normalizasyonu: dış ağa çıkmadan test edilir.
def test_bedesten_payload_and_result_normalization():
    import os
    from mevzuat_kaynak import _DEFAULT_SEARCH_TEMPLATE, _extract_items, _extract_total, _load_template
    old = os.environ.pop("BEDSTEN_SEARCH_PAYLOAD_JSON", None)
    try:
        body = _load_template(
            "BEDSTEN_SEARCH_PAYLOAD_JSON", "${query}", ' "içtihat" ',
            default=_DEFAULT_SEARCH_TEMPLATE,
        )
        assert body["data"]["phrase"] == ' "içtihat" '
        assert body["data"]["itemTypeList"] == ["YARGITAYKARARI"]
        payload = {"data": {"total": 17, "emsalKararList": [{"documentId": "abc"}]}}
        items = _extract_items(payload)
        assert len(items) == 1 and items[0]["documentId"] == "abc"
        assert _extract_total(payload, items) == 17
    finally:
        if old is not None:
            os.environ["BEDSTEN_SEARCH_PAYLOAD_JSON"] = old



if __name__ == "__main__":
    asyncio.run(test_bedesten_requires_verified_templates())
    test_bedesten_payload_and_result_normalization()
    test_bedesten_extractor_ignores_unrecognized_arrays()
    print("BEDSTEN MOCK TESTS PASSED")


def test_bedesten_extractor_ignores_unrecognized_arrays():
    from mevzuat_kaynak import _extract_items
    # Bir metadata listesi, karar listesiymiş gibi sunulmamalı.
    payload = {"data": {"facets": [{"name": "daire", "count": 5}]}}
    assert _extract_items(payload) == []
    # Bilinen alanlar varsa sabit öncelikle karar kayıtları alınır.
    payload = {
        "data": {
            "facets": [{"name": "daire"}],
            "emsalKararList": [{"documentId": "decision-1"}],
        }
    }
    assert _extract_items(payload) == [{"documentId": "decision-1"}]


