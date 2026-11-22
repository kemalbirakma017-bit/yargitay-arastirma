"""Birleşik arama ve karar_metni yönlendirmesi için regresyon testleri.

Tüm testler kaynak servislere gerçek ağ isteği göndermez.
"""
import asyncio

import server
from mevzuat_kaynak import BedestenConfigurationError


async def test_both_sources_success():
    original_search = server._do_search
    original_bedesten = server._bedesten.search

    async def yargitay_search(**kwargs):
        return {"kararlar": [{"id": "y-1"}]}

    async def bedesten_search(query):
        return {"sonuclar": [{"documentId": "b-1"}]}

    server._do_search = yargitay_search
    server._bedesten.search = bedesten_search
    try:
        result = await server.tum_kaynaklarda_karar_ara("tazminat")
        assert result["kaynaklar"]["yargitay"]["durum"] == "başarılı"
        assert result["kaynaklar"]["yargitay"]["sonuc"]["kararlar"][0]["id"] == "y-1"
        assert result["kaynaklar"]["bedesten"]["durum"] == "başarılı"
        assert result["kaynaklar"]["bedesten"]["sonuc"]["sonuclar"][0]["documentId"] == "b-1"
    finally:
        server._do_search = original_search
        server._bedesten.search = original_bedesten


async def test_bedesten_unconfigured_does_not_hide_yargitay():
    original_search = server._do_search
    original_bedesten = server._bedesten.search

    async def yargitay_search(**kwargs):
        return {"kararlar": [{"id": "y-2"}]}

    async def bedesten_search(query):
        raise BedestenConfigurationError("şablon eksik")

    server._do_search = yargitay_search
    server._bedesten.search = bedesten_search
    try:
        result = await server.tum_kaynaklarda_karar_ara("tazminat")
        assert result["kaynaklar"]["yargitay"]["durum"] == "başarılı"
        assert result["kaynaklar"]["bedesten"]["durum"] == "yapılandırılmadı"
    finally:
        server._do_search = original_search
        server._bedesten.search = original_bedesten


async def test_bedesten_http_error_does_not_hide_yargitay():
    original_search = server._do_search
    original_bedesten = server._bedesten.search

    async def yargitay_search(**kwargs):
        return {"kararlar": [{"id": "y-3"}]}

    async def bedesten_search(query):
        raise RuntimeError("HTTP 500")

    server._do_search = yargitay_search
    server._bedesten.search = bedesten_search
    try:
        result = await server.tum_kaynaklarda_karar_ara("tazminat")
        assert result["kaynaklar"]["yargitay"]["durum"] == "başarılı"
        assert result["kaynaklar"]["bedesten"]["durum"] == "hata"
        assert result["kaynaklar"]["bedesten"]["mesaj"] == "HTTP 500"
    finally:
        server._do_search = original_search
        server._bedesten.search = original_bedesten


async def test_yargitay_error_does_not_prevent_bedesten_search():
    original_search = server._do_search
    original_bedesten = server._bedesten.search
    bedesten_called = False

    async def yargitay_search(**kwargs):
        raise RuntimeError("Yargitay HTTP 500")

    async def bedesten_search(query):
        nonlocal bedesten_called
        bedesten_called = True
        return {"sonuclar": [{"documentId": "b-4"}]}

    server._do_search = yargitay_search
    server._bedesten.search = bedesten_search
    try:
        result = await server.tum_kaynaklarda_karar_ara("tazminat")
        assert bedesten_called, "Yargıtay hatası Bedesten çağrısını engelledi"
        assert result["kaynaklar"]["yargitay"]["durum"] == "hata"
        assert result["kaynaklar"]["yargitay"]["mesaj"] == "Yargitay HTTP 500"
        assert result["kaynaklar"]["bedesten"]["durum"] == "başarılı"
        assert result["kaynaklar"]["bedesten"]["sonuc"]["sonuclar"][0]["documentId"] == "b-4"
    finally:
        server._do_search = original_search
        server._bedesten.search = original_bedesten


async def test_karar_metni_routes_by_source():
    original_yargitay = server.yargitay_karar_metni
    original_bedesten = server._bedesten.document

    async def yargitay_document(document_id):
        return {"source": "yargitay", "id": document_id}

    async def bedesten_document(document_id):
        return {"source": "bedesten", "id": document_id}

    server.yargitay_karar_metni = yargitay_document
    server._bedesten.document = bedesten_document
    try:
        y = await server.karar_metni("yargitay", "101")
        b = await server.karar_metni("bedesten", "202")
        assert y == {"source": "yargitay", "id": "101"}
        assert b == {"source": "bedesten", "id": "202"}
        try:
            await server.karar_metni("unknown", "303")
        except ValueError:
            pass
        else:
            raise AssertionError("Geçersiz kaynak reddedilmedi")
    finally:
        server.yargitay_karar_metni = original_yargitay
        server._bedesten.document = original_bedesten


async def main():
    await test_both_sources_success()
    await test_bedesten_unconfigured_does_not_hide_yargitay()
    await test_bedesten_http_error_does_not_hide_yargitay()
    await test_yargitay_error_does_not_prevent_bedesten_search()
    await test_karar_metni_routes_by_source()
    print("Birleşik arama ve karar_metni regresyon testleri geçti.")


if __name__ == "__main__":
    asyncio.run(main())
