#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bedesten canlı tanılama aracı.

Güvenlik sınırları:
- Mod açıkça seçilmelidir: --canli veya --test-sunucu.
- En fazla iki POST isteği gönderilir: arama ve bulunan kararın içeriği.
- Cookie, Authorization veya oturum bilgisi gönderilmez/kaydedilmez.
- HTTP yönlendirmeleri izlenmez.
- 403, 429, 5xx ve bağlantı hatalarında durur; tekrar denemez.
- Ham yanıtlar varsayılan olarak kaydedilmez.
- TEST modu yalnızca localhost/loopback adreslerine izin verir.

Kullanım:
  python bedesten_tanilama.py --canli
  python bedesten_tanilama.py --test-sunucu http://127.0.0.1:8000
  python bedesten_tanilama.py --canli --ham-kaydet
"""
import argparse
import base64
import datetime
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CANLI_BASE = "https://bedesten.adalet.gov.tr"
USER_AGENT = "bedesten-tanilama/1.0"
TIMEOUT = 25
DURDURAN_KODLAR = {403, 429}
MAX_ISTEK = 2
ISTEK_SAYISI = 0


class YonlendirmeEngelle(urllib.request.HTTPRedirectHandler):
    """Yönlendirmeyi takip etmek yerine HTTPError üretir."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        guvenli_hedef = urllib.parse.urlsplit(newurl)
        hedef = f"{guvenli_hedef.scheme}://{guvenli_hedef.netloc}"
        raise urllib.error.HTTPError(
            req.full_url, code, f"Yönlendirme engellendi (hedef: {hedef})", headers, fp
        )


OPENER = urllib.request.build_opener(YonlendirmeEngelle)


def test_adresi_dogrula(url):
    """TEST modunun yanlışlıkla gerçek/uzak sunucuya istek atmasını engeller."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "http" or not p.hostname:
        raise ValueError("TEST sunucusu http://127.0.0.1:PORT biçiminde olmalıdır.")
    try:
        ip = ipaddress.ip_address(p.hostname)
        yerel = ip.is_loopback
    except ValueError:
        yerel = p.hostname.lower() == "localhost"
    if not yerel:
        raise ValueError("Güvenlik nedeniyle TEST modu yalnızca localhost/loopback adreslerine izin verir.")
    if p.username or p.password:
        raise ValueError("TEST URL'sinde kullanıcı adı/parola bulunamaz.")
    return url.rstrip("/")


def istek(url, govde):
    """Kimlik bilgisi eklemeden tek POST isteği gönderir."""
    global ISTEK_SAYISI
    if ISTEK_SAYISI >= MAX_ISTEK:
        raise RuntimeError("İstek sınırı (2) aşılamaz.")
    ISTEK_SAYISI += 1
    veri = json.dumps(govde, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=veri, method="POST", headers={
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with OPENER.open(req, timeout=TIMEOUT) as response:
            return {"kod": response.status, "govde": response.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(300).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"kod": e.code, "govde": body, "not": str(e.reason)}
    except Exception as e:
        return {"kod": None, "hata": f"{type(e).__name__}: {e}"}


def yapi(obj, derinlik=0):
    """JSON anahtar yapısını, içerik değerlerini göstermeden özetler."""
    if isinstance(obj, dict):
        return ({k: yapi(v, derinlik + 1) for k, v in obj.items()}
                if derinlik < 3 else "{...}")
    if isinstance(obj, list):
        if not obj:
            return "liste(0)"
        return ([yapi(obj[0], derinlik + 1), f"... ({len(obj)} öğe)"]
                if derinlik < 3 else f"liste({len(obj)})")
    return type(obj).__name__


def durdurm(kod):
    return kod is None or kod in DURDURAN_KODLAR or kod >= 500 or 300 <= kod < 400


def rapor_yaz(klasor, satirlar):
    rapor = "\n".join(satirlar)
    yol = os.path.join(klasor, "rapor.txt")
    with open(yol, "w", encoding="utf-8") as f:
        f.write(rapor)
    print(rapor)
    print(f"\nRapor kaydedildi: {yol}")


def main():
    parser = argparse.ArgumentParser(description="Bedesten bağlantı/tam metin tanılaması")
    mod = parser.add_mutually_exclusive_group(required=True)
    mod.add_argument("--canli", action="store_true", help="Gerçek Bedesten'e bağlan")
    mod.add_argument("--test-sunucu", metavar="URL", help="Yalnızca localhost sahte sunucu")
    parser.add_argument("--ham-kaydet", action="store_true", help="Ham JSON yanıtlarını da kaydet")
    args = parser.parse_args()

    try:
        base = test_adresi_dogrula(args.test_sunucu) if args.test_sunucu else CANLI_BASE
    except ValueError as e:
        parser.error(str(e))
    mod_adi = "TEST" if args.test_sunucu else "CANLI"
    arama_url = base + "/emsal-karar/searchDocuments"
    icerik_url = base + "/emsal-karar/getDocumentContent"

    tarih = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    klasor = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"rapor_{tarih}")
    os.makedirs(klasor, exist_ok=True)
    s = [f"MOD: {mod_adi}", f"Tarih: {tarih}", f"User-Agent: {USER_AGENT}",
         "Cookie/Authorization gönderilmedi. Yönlendirme takip edilmedi.",
         "Ham yanıtlar kaydedildi mi: " + ("EVET" if args.ham_kaydet else "HAYIR"), ""]
    if mod_adi == "TEST":
        s += ["UYARI: TEST modu yerel sahte sunucudur; gerçek Bedesten kanıtı değildir.", ""]

    # 1) Arama
    s.append(f"== 1. ARAMA == {arama_url}")
    r1 = istek(arama_url, {"data": {"phrase": "tazminat", "itemTypeList": ["YARGITAYKARARI"],
                                      "pageSize": 3, "pageNumber": 1}, "paging": True})
    s.append(f"HTTP kodu: {r1.get('kod')}" + (f" | hata: {r1['hata']}" if "hata" in r1 else "")
             + (f" | {r1['not']}" if r1.get("not") else ""))
    ilk_id, dur = None, False
    karar_listesi_bulundu = False
    tam_metin_cozuldu = False
    if durdurm(r1.get("kod")):
        s.append(f"DURDU: {r1.get('kod')}. Tekrar denenmedi; yönlendirme/engel aşılmadı.")
        dur = True
    else:
        try:
            j = json.loads(r1["govde"])
            if args.ham_kaydet:
                with open(os.path.join(klasor, "arama_yanit.json"), "w", encoding="utf-8") as f:
                    f.write(r1["govde"])
            s += ["JSON: evet. Yapı (değerler yok):", json.dumps(yapi(j), ensure_ascii=False, indent=1)]
            data = j.get("data") if isinstance(j, dict) else None
            kararlar = data.get("emsalKararList") if isinstance(data, dict) else None
            if isinstance(kararlar, list) and kararlar:
                k = kararlar[0]
                s.append(f"Karar listesi: BULUNDU ({len(kararlar)} kayıt)")
                karar_listesi_bulundu = True
                s.append(f"İlk kayıt alanları: {sorted(k.keys())}")
                s.append("İlk künye: " + " | ".join([
                    f"documentId={k.get('documentId')}", f"birimAdi={k.get('birimAdi')}",
                    f"esasNo={k.get('esasNo')}", f"kararNo={k.get('kararNo')}",
                    f"kararTarihiStr={k.get('kararTarihiStr')}"
                ]))
                ilk_id = str(k.get("documentId") or "") or None
            else:
                s.append("Karar listesi: BULUNAMADI (emsalKararList yok veya boş).")
        except (ValueError, TypeError, KeyError) as e:
            s.append(f"JSON ayrıştırılamadı: {type(e).__name__}. Yanıt rapora yazılmadı.")
            dur = True

    # 2) Tam metin
    s += ["", f"== 2. TAM METİN == {icerik_url}"]
    if dur or not ilk_id:
        s.append("ATLANDI: belge kimliği alınamadı veya ilk aşamada duruldu.")
    else:
        r2 = istek(icerik_url, {"data": {"documentId": ilk_id}})
        s.append(f"documentId: {ilk_id}")
        s.append(f"HTTP kodu: {r2.get('kod')}" + (f" | hata: {r2['hata']}" if "hata" in r2 else "")
                 + (f" | {r2['not']}" if r2.get("not") else ""))
        if durdurm(r2.get("kod")):
            s.append(f"DURDU: {r2.get('kod')}. Tekrar denenmedi.")
        else:
            try:
                j = json.loads(r2["govde"])
                if args.ham_kaydet:
                    with open(os.path.join(klasor, "metin_yanit.json"), "w", encoding="utf-8") as f:
                        f.write(r2["govde"])
                data = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
                content = data.get("content") if isinstance(data, dict) else None
                s.append(f"JSON: evet. Üst düzey anahtarlar: {sorted(j.keys()) if isinstance(j, dict) else type(j).__name__}")
                s.append(f"mimeType: {data.get('mimeType') if isinstance(data, dict) else None}")
                if isinstance(content, str) and content.strip():
                    try:
                        compact = re.sub(r"\s+", "", content)
                        decoded = base64.b64decode(compact, validate=True).decode("utf-8", "replace")
                        if decoded.strip():
                            tam_metin_cozuldu = True
                            s.append(f"Tam metin BASE64 çözüldü: {len(decoded)} karakter. Metin rapora yazılmadı.")
                        else:
                            s.append("Tam metin BASE64 çözüldü ancak içerik boş.")
                    except Exception as e:
                        s.append(f"Base64 çözülemedi: {type(e).__name__}: {str(e)[:120]}")
                else:
                    s.append("content alanı bulunamadı veya boş.")
            except (ValueError, TypeError, KeyError) as e:
                s.append(f"JSON ayrıştırılamadı: {type(e).__name__}. Yanıt rapora yazılmadı.")

    s += ["", f"Gönderilen istek sayısı: {ISTEK_SAYISI} (üst sınır {MAX_ISTEK})"]
    basarili = mod_adi == "CANLI" and karar_listesi_bulundu and tam_metin_cozuldu
    s.append("SONUÇ: BAŞARILI" if basarili else "SONUÇ: BAŞARISIZ")
    rapor_yaz(klasor, s)
    return 0 if basarili else 2


if __name__ == "__main__":
    sys.exit(main())
