
import os
import html
import secrets

from fastapi import FastAPI, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from server import _do_search, _fetch_doc_text

app = FastAPI(title="Yargıtay Karar Araştırma")
security = HTTPBasic()

def yetki(credentials: HTTPBasicCredentials = Depends(security)):
    username = os.getenv("APP_USERNAME", "")
    password = os.getenv("APP_PASSWORD", "")
    if not username or not password:
        raise HTTPException(status_code=503, detail="Uygulama giriş bilgileri ayarlanmamış.")
    if not (
        secrets.compare_digest(credentials.username, username)
        and secrets.compare_digest(credentials.password, password)
    ):
        raise HTTPException(
            status_code=401,
            detail="Giriş bilgileri hatalı",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

@app.get("/", response_class=HTMLResponse)
async def ana_sayfa(ok: bool = Depends(yetki)):
    return """
    <!doctype html><html lang="tr"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Yargıtay Karar Araştırma</title>
    <style>
    body{font:16px Arial;max-width:950px;margin:35px auto;padding:0 16px;color:#222}
    input,button{font:inherit;padding:11px;margin:5px 0}
    input{width:95%;max-width:600px}button{cursor:pointer}
    .karar{border:1px solid #ddd;padding:14px;margin:12px 0;border-radius:8px}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}
    </style>
    <h1>Yargıtay Karar Araştırma</h1>
    <p>Aranacak kelime veya hukuki ifadeyi girin.</p>
    <form id="form">
      <input name="kelime" placeholder="Örn. vasiyetnamenin iptali" required>
      <br><input name="daire" placeholder="Daire (isteğe bağlı, örn. 3. Hukuk Dairesi)">
      <br><button>Karar Ara</button>
    </form>
    <p id="durum"></p><div id="sonuc"></div>
    <script>
    const form=document.getElementById('form');
    const durum=document.getElementById('durum');
    const sonuc=document.getElementById('sonuc');

    form.onsubmit=async e=>{
      e.preventDefault(); sonuc.replaceChildren();
      durum.textContent='Yargıtay sitesinde aranıyor; lütfen bekleyin...';
      const fd=new FormData(form);
      try{
        const r=await fetch('/ara',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            aranan_kelime:fd.get('kelime'),
            daire:fd.get('daire'),
            sayfa_boyutu:20
          })
        });
        const data=await r.json();
        if(!r.ok) throw Error(data.detail||'Arama başarısız');
        durum.textContent='Toplam sonuç: '+data.toplam_sonuc;
        if(!data.kararlar.length){sonuc.textContent=data.not||'Sonuç bulunamadı.';return}
        data.kararlar.forEach(k=>{
          const card=document.createElement('div');card.className='karar';
          const title=document.createElement('strong');
          title.textContent=(k.daire||'Daire belirtilmemiş')+' | Esas: '+(k.esas_no||'-')+' | Karar: '+(k.karar_no||'-');
          const date=document.createElement('p');date.textContent='Tarih: '+(k.karar_tarihi||'-');
          const btn=document.createElement('button');btn.textContent='Karar metnini aç';
          const body=document.createElement('pre');
          btn.onclick=async()=>{
            btn.disabled=true;body.textContent='Karar metni getiriliyor...';
            try{
              const rr=await fetch('/karar/'+encodeURIComponent(k.id));
              const d=await rr.json();
              if(!rr.ok) throw Error(d.detail||'Metin alınamadı');
              body.textContent=d.metin||d.uyari||'Metin bulunamadı.';
            }catch(err){body.textContent=err.message}
            finally{btn.disabled=false}
          };
          card.append(title,date,btn,body);sonuc.append(card);
        });
      }catch(err){durum.textContent='Hata: '+err.message}
    };
    </script></html>
    """

@app.post("/ara")
async def ara(data: dict, ok: bool = Depends(yetki)):
    try:
        return await _do_search(
            aranan_kelime=str(data.get("aranan_kelime", "")),
            daire=str(data.get("daire", "")),
            sayfa_boyutu=int(data.get("sayfa_boyutu", 20)),
        )
  except Exception as e:
    import logging
    logging.exception("Yargitay arama hatasi")
    raise HTTPException(
        status_code=502,
        detail=f"{type(e).__name__}: {e!r}"
    )
@app.get("/karar/{karar_id}")
async def karar(karar_id: str, ok: bool = Depends(yetki)):
    if not karar_id.isdigit():
        raise HTTPException(status_code=400, detail="Geçersiz karar numarası.")
    try:
        metin = await _fetch_doc_text(karar_id)
        return {"id": karar_id, "metin": metin}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
