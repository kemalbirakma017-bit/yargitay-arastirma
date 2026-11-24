# Yargıtay Karar Arama — UYAP Bedesten MCP

Bu depo, Adalet Bakanlığı UYAP Mevzuat ve İçtihat / Bedesten üzerinden Yargıtay kararlarını aramak ve karar metnini almak için FastMCP sunucusu içerir.

## FastMCP sunucusu

- **Entrypoint:** `main.py:mcp` (alternatif: `bedesten_server.py:mcp`)
- **Bağımlılıklar:** `requirements.txt`
- **Taşıma:** FastMCP Cloud/Gateway tarafından yönetilen uzak MCP (Streamable HTTP)
- **MCP yolu:** `/mcp`

## Araçlar

| Araç | İşlev |
|---|---|
| `yargitay_daire_listesi` | HGK, CGK ve hukuk/ceza dairelerini listeler. |
| `yargitay_karar_ara` | Bedesten'de kelime, daire, esas/karar numarası ve tarih ölçütleriyle arama yapar. |
| `yargitay_karar_metni` | Arama sonucundaki karar kimliğiyle tam metni getirir. |
| `yargitay_toplu_arastirma` | Arama yapar ve en fazla iki kararın metnini aynı çağrıda getirir. |

## Ücretsiz kişisel dağıtım

FastMCP Gateway kişisel projeler için ücretsiz plan sunduğunu resmî hızlı başlangıç belgesinde belirtiyor. Dağıtım için:

1. GitHub hesabıyla [FastMCP Gateway](https://horizon.prefect.io/) hesabına giriş yapın.
2. **Create project / New project** bölümünden bu depoyu seçin.
3. Server entrypoint alanına `main.py:mcp` yazın.
4. Build/deploy işlemini başlatın.
5. Dağıtım tamamlandığında Gateway'in verdiği `https://....fastmcp.app/mcp` adresini kopyalayın.
6. Bu adresi MCP destekleyen istemcinin uzak sunucu/connector ayarına ekleyin.

FastMCP Gateway, GitHub deposundaki dosyaları alıp sunucuyu kendi yönetilen ortamında çalıştırır. Bu yöntem için Railway veya ücretli bir VPS kullanılmaz.

## Yerel çalıştırma

```bash
pip install -r requirements.txt
python bedesten_server.py
```

Yerel MCP adresi: `http://127.0.0.1:8000/mcp`

## Parametreler

- `YARGITAY_MIN_INTERVAL`: Bedesten'e gönderilen istekler arasındaki asgari bekleme süresi (saniye); varsayılan 2.
- Arama tarihleri `YYYY-AA-GG` biçiminde verilmelidir.
- Bedesten'in erişim sınırlaması (HTTP 429) uygulaması hâlinde araç hata döndürür; otomatik olarak sınırsız tekrar yapmaz.
- Daire filtresi Bedesten aramasına `birimAdi` ile sunucu tarafında gönderilir. Varsayılan sıralama Bedesten'in alaka sıralamasıdır; `siralama="tarih_yeniden_eskiye"` ile tarih sıralaması seçilebilir.

## Doğrulama ilkesi

Bu sunucu bir arama ve belge getirme aracıdır; hukuki içtihat doğrulamasının yerine geçmez. Karar künyesi, tarihi ve tam metni Bedesten yanıtından alınmalıdır. Tam metin alınamadığında bu açıkça belirtilmeli; eksik metin tam metinmiş gibi sunulmamalıdır. UYAP'ta açılan kararın kendisiyle son kontrol yapılmalıdır.

**Dağıtım notu:** Kaynak kodunun FastMCP Gateway üzerinde başarıyla deploy edilmesi, Bedesten uç noktalarının o andaki erişilebilirliğini tek başına kanıtlamaz. İlk canlı testte HGK/CGK araması ve bir kararın tam metin çağrısı ayrıca doğrulanmalıdır.
