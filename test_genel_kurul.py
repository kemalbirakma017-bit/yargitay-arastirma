"""HGK, CGK ve Büyük Genel Kurul filtrelerinin regresyon testleri.

Dış ağa çıkmaz; _do_search katmanına sahte yanıt verir ve gönderilen
Yargıtay daire filtresini doğrular.
"""
import asyncio

import server


async def run_general_council_tests():
    expected = (
        "Hukuk Genel Kurulu",
        "Ceza Genel Kurulu",
        "Büyük Genel Kurulu",
    )
    listed = await server.yargitay_daire_listesi()
    for name in expected:
        assert name in listed["daireler"], f"Kurul listede yok: {name}"

    original_request = server._request
    captured = []

    async def fake_request(method, path, **kwargs):
        captured.append((method, path, kwargs))
        return {
            "data": {
                "recordsTotal": 1,
                "data": [{
                    "id": "123456",
                    "daire": kwargs["json"]["data"]["birimYrgKurulDaire"],
                    "esasNo": "2025/1",
                    "kararNo": "2026/1",
                    "kararTarihi": "01.02.2026",
                }],
            }
        }

    server._request = fake_request
    try:
        for name in expected:
            result = await server._do_search(aranan_kelime="test", daire=name)
            assert result["toplam_sonuc"] == 1
            assert result["kararlar"][0]["daire"] == name
            method, path, kwargs = captured[-1]
            assert method == "POST" and path == "/aramadetaylist"
            assert kwargs["json"]["data"]["birimYrgKurulDaire"] == name
    finally:
        server._request = original_request

    print("HGK / CGK / Büyük Genel Kurul tests passed")


if __name__ == "__main__":
    asyncio.run(run_general_council_tests())
