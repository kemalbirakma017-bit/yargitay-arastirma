# Yargıtay Karar Arama — MCP Sunucusu

karararama.yargitay.gov.tr (kamuya açık Yargıtay karar arama platformu) üzerinde
arama yapan ve karar tam metni getiren, **uzak (HTTP) MCP sunucusu**. Bir kez
bulutta barındırılır, claude.ai hesabınıza özel bağlayıcı (custom connector)
olarak eklenir ve **hesabınızı kullandığınız tüm bilgisayarlarda** çalışır.

## Araçlar

| Araç | İşlev |
|---|---|
| `yargitay_karar_ara` | Kelime/ifade, daire, esas–karar no aralığı ve karar tarihi aralığına göre arama. Sayfalama destekli. |
| `yargitay_karar_metni` | Arama sonucundaki `id` ile kararın tam metnini düz metin olarak getirir. |
| `yargitay_daire_listesi` | Geçerli daire/kurul adlarını listeler (Hukuk/Ceza Genel Kurulu, 1–23. Hukuk/Ceza Daireleri vb.). |

## Önemli teknik notlar (canlı testle doğrulandı — 16.07.2026)

- Sitenin resmî/dokümante API'si yoktur; sunucu, sitenin kendi arayüzünün
  kullandığı `POST /aramadetaylist` ve `GET /getDokuman` uç noktalarını kullanır.
  **Site arayüzü değişirse sunucunun güncellenmesi gerekebilir.**
- Site hız sınırı uygular (HTTP 429 "Erişim Sınırı Aşıldı"). Sunucu istekler
  arasına en az 3 sn koyar ve 429'da 20 sn bekleyip bir kez yeniden dener.
  Buna rağmen sınır aşılırsa kullanıcıya "1-2 dakika bekleyin" mesajı döner.
- Daire filtresinde `"ALL"` değeri sitede 0 sonuç döndürür; tüm daireler için
  alan hiç gönderilmez (sunucu bunu otomatik yönetir).
- Tarih formatı `GG.AA.YYYY`, kesin ifade araması çift tırnakla yapılır:
  `"elektrik piyasası"`.

## Kurulum — Seçenek A: FastMCP Cloud (önerilen, kişisel kullanımda ücretsiz)

1. github.com'da hesap açın (varsa geçin) ve bu klasörü **private repo** olarak
   yükleyin (`server.py`, `requirements.txt` yeterli).
2. https://fastmcp.cloud adresine GitHub ile giriş yapın.
3. "New Server" → repo'yu seçin → Entrypoint: `server.py:mcp` → Deploy.
4. ~1 dk içinde adres alırsınız: `https://<sunucu-adiniz>.fastmcp.app/mcp`

## Kurulum — Seçenek B: Kendi sunucunuz / Render / Railway (Docker)

```bash
docker build -t yargitay-mcp .
docker run -p 8000:8000 yargitay-mcp
# MCP adresi: http://<host>:8000/mcp  (HTTPS için ters proxy gerekir)
```

Ortam değişkenleri: `PORT` (varsayılan 8000), `MCP_PATH` (varsayılan `/mcp`),
`YARGITAY_MIN_INTERVAL` (istekler arası minimum sn, varsayılan 3).

## claude.ai'ye bağlama (tüm cihazlarda çalışması için)

1. claude.ai → **Ayarlar → Bağlayıcılar (Connectors)** → **Özel bağlayıcı ekle**.
2. Ad: `Yargıtay Karar Arama`, URL: `https://<sunucu-adiniz>.fastmcp.app/mcp`.
3. Kaydedin; her sohbette bağlayıcıyı etkinleştirerek kullanın.

> Not: Kimlik doğrulaması olmayan bir bağlayıcıda URL'yi bilen herkes sunucuyu
> kullanabilir. Veriler kamuya açık olduğundan risk düşüktür; yine de URL'yi
> paylaşmayın. İsterseniz FastMCP Cloud üzerinde OAuth açılabilir.

## Yerel test

```bash
pip install -r requirements.txt
python test_mock.py        # mock upstream ile MCP katmanı testleri
MCP_TRANSPORT=stdio python server.py   # stdio modunda (tek makine) çalıştırma
```

## Hukuki not

Kararlar Yargıtay'ın kamuya açık platformundan alınır; kişisel veri içerebilen
karar metinlerinin işlenmesinde KVKK ve platformun kullanım koşullarına uyum
kullanıcının sorumluluğundadır.


## Birleşik kaynaklar (geliştirme dalı)

- `tum_kaynaklarda_karar_ara`: Yargıtay sonuçlarını ve Bedesten sonucunu
  ayrı kaynak başlıkları altında döndürür.
- `karar_metni`: `kaynak="yargitay"` veya `kaynak="bedesten"` ile metin
  isteme yönlendirmesi yapar.
- Bedesten arama gövdesi, kamuya açık bir istemci uygulamasında görülen
  şemayla varsayılan olarak hazırlanır; bu resmî API dokümantasyonu veya
  canlı doğrulama değildir. Gerekirse `BEDSTEN_SEARCH_PAYLOAD_JSON` ile
  değiştirilebilir ve içinde `${query}` yer tutucusu bulunmalıdır.
  Belge içeriği uç noktasının gövdesi doğrulanmadığından
  `BEDSTEN_CONTENT_PAYLOAD_JSON` ortam değişkeni zorunludur; JSON şablonunda
  `${document_id}` yer tutucusu bulunmalıdır.
- Bu nedenle yalnızca mock testlerinin geçmesi Bedesten'den canlı karar
  alınabildiği anlamına gelmez. Canlı doğrulama ayrı yapılmalıdır.
