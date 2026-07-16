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
