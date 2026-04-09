import os, json, asyncio, threading, time, hashlib, secrets
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from flask import Flask, render_template_string, jsonify, request, session as fs, redirect, url_for

# ── Ayarlar ────────────────────────────────────────────────────
API_ID        = 30834437
API_HASH      = "d0b475bf9b8485b002650c5aeb85e134"
ACCOUNTS_DIR  = "accounts"
USERS_FILE    = "users.json"
NUMBERS_FILE  = "numbers.json"
MESSAGES_FILE = "messages.json"
SECRET_KEY    = "numarium-2025-secret"
ADMIN_USER    = "mustafapro"
ADMIN_PASS    = "mustafapro"

# Google OAuth ayarları (7: Google ile giriş)
GOOGLE_CLIENT_ID     = "YOUR_GOOGLE_CLIENT_ID"
GOOGLE_CLIENT_SECRET = "YOUR_GOOGLE_CLIENT_SECRET"
GOOGLE_REDIRECT_URI  = "http://localhost:5000/auth/google/callback"

os.makedirs(ACCOUNTS_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ── Global async loop (Telethon için) ──────────────────────────
_loop = asyncio.new_event_loop()
threading.Thread(
    target=lambda: (asyncio.set_event_loop(_loop), _loop.run_forever()),
    daemon=True
).start()

def run_async(coro, timeout=30):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)

pending_add     = {}   # phone -> {client, phone_hash, coin_cost}
monitor_clients = {}   # phone -> TelegramClient

# ── Veri I/O ───────────────────────────────────────────────────
def _ld(f, d):
    try:
        if os.path.exists(f):
            with open(f, "r", encoding="utf-8") as fp:
                return json.load(fp)
    except: pass
    return d

def _sv(f, data):
    with open(f, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

load_users   = lambda: _ld(USERS_FILE, {})
load_numbers = lambda: _ld(NUMBERS_FILE, [])
load_msgs    = lambda: _ld(MESSAGES_FILE, {})
save_users   = lambda d: _sv(USERS_FILE, d)
save_numbers = lambda d: _sv(NUMBERS_FILE, d)
save_msgs    = lambda d: _sv(MESSAGES_FILE, d)

# ── Ülke haritası ──────────────────────────────────────────────
CMAP = {
    "+90":("🇹🇷","Türkiye"),   "+1":("🇺🇸","ABD"),        "+44":("🇬🇧","İngiltere"),
    "+49":("🇩🇪","Almanya"),   "+33":("🇫🇷","Fransa"),     "+39":("🇮🇹","İtalya"),
    "+34":("🇪🇸","İspanya"),   "+7":("🇷🇺","Rusya"),       "+380":("🇺🇦","Ukrayna"),
    "+31":("🇳🇱","Hollanda"),  "+46":("🇸🇪","İsveç"),      "+91":("🇮🇳","Hindistan"),
    "+86":("🇨🇳","Çin"),       "+81":("🇯🇵","Japonya"),    "+55":("🇧🇷","Brezilya"),
    "+971":("🇦🇪","BAE"),      "+966":("🇸🇦","S.Arabistan"),"+82":("🇰🇷","G.Kore"),
    "+48":("🇵🇱","Polonya"),   "+20":("🇪🇬","Mısır"),      "+47":("🇳🇴","Norveç"),
    "+45":("🇩🇰","Danimarka"), "+358":("🇫🇮","Finlandiya"), "+32":("🇧🇪","Belçika"),
    "+41":("🇨🇭","İsviçre"),   "+43":("🇦🇹","Avusturya"),  "+30":("🇬🇷","Yunanistan"),
}

def ci(phone):
    for k in sorted(CMAP, key=len, reverse=True):
        if phone.startswith(k): return CMAP[k]
    return ("🌍", "Uluslararası")

# ── FIX 2: Satın alınan numaralara süre ve iade takibi ─────────
# numbers.json'daki her numaraya "purchased_at" ve "code_received" alanları ekleniyor.
# 10 dakika içinde kod gelmezse arka plan thread'i numarayı serbest bırakıp coin iade eder.
# 5 dakika içinde iptal edilebilir.

def _check_expired_purchases():
    """Her 60 saniyede bir çalışır, süresi dolan satın alımları iade eder."""
    while True:
        time.sleep(60)
        try:
            numbers = load_numbers()
            users   = load_users()
            changed = False
            now     = time.time()
            for n in numbers:
                if not n.get("purchased_by"):
                    continue
                # code_received = True ise artık iade yok
                if n.get("code_received"):
                    continue
                purchased_at = n.get("purchased_at", 0)
                if purchased_at and (now - purchased_at) > 600:  # 10 dakika
                    buyer = n["purchased_by"]
                    cost  = n.get("coin_cost", 50)
                    # Coin iade
                    if buyer in users:
                        users[buyer]["coins"] = users[buyer].get("coins", 0) + cost
                    # Numarayı serbest bırak
                    n["purchased_by"] = ""
                    n["purchased_at"] = 0
                    changed = True
            if changed:
                save_numbers(numbers)
                save_users(users)
        except Exception as e:
            print(f"[expired-check] hata: {e}")

threading.Thread(target=_check_expired_purchases, daemon=True).start()

# ── Telethon izleme ────────────────────────────────────────────
def _attach_handler(client, phone):
    @client.on(events.NewMessage(incoming=True))
    async def _h(ev):
        m = load_msgs()
        if phone not in m: m[phone] = []
        try:
            s  = await ev.get_sender()
            nm = " ".join(filter(None, [
                getattr(s, "first_name", "") or "",
                getattr(s, "last_name",  "") or ""
            ])).strip() or getattr(s, "username", "") or "?"
        except:
            nm = "?"
        text = ev.message.message or ""

        print(f"[MSG] phone={phone} | sender={nm!r} | text={text!r}")

        # Mesajı kaydet — gönderen Telegram servisi ise veya 5-6 haneli kod içeriyorsa
        extracted_code = _extract_code_strict(text)
        is_tg_auth = (
            "telegram" in nm.lower() or
            nm == "777000" or
            "login code" in text.lower() or
            "verification code" in text.lower() or
            "doğrulama kodu" in text.lower() or
            "giriş kodu" in text.lower() or
            "your code" in text.lower() or
            extracted_code is not None  # herhangi bir 5-6 haneli kod varsa
        )
        print(f"[MSG] extracted_code={extracted_code!r} | is_tg_auth={is_tg_auth}")
        if not is_tg_auth:
            print(f"[MSG] Filtreden geçemedi, yoksayıldı.")
            return  # Alakasız mesajları yoksay

        # FIX 5: Tek seferlik kod – daha önce kod aldıysak yeni mesaj ekleme
        nums = load_numbers()
        num_entry = next((n for n in nums if n["phone"] == phone), None)
        if num_entry and num_entry.get("code_received"):
            return  # Zaten bir kod gösterildi, ikincisini kaydetme

        # Kodu kaydet ve code_received işaretle
        new_msg = {
            "sender": nm,
            "text":   text,
            "time":   datetime.now().strftime("%d.%m %H:%M"),
            "code":   extracted_code or _extract_code_strict(text)  # Kodu direkt sakla
        }
        m[phone] = [new_msg]  # Sadece en son mesajı tut (tek seferlik)
        save_msgs(m)

        # Numarayı "kod alındı" olarak işaretle
        if num_entry:
            num_entry["code_received"] = True
            save_numbers(nums)

async def _start_monitor(phone):
    print(f"[MONITOR] _start_monitor: {phone}")
    if phone in monitor_clients:
        print(f"[MONITOR] Zaten izleniyor: {phone}")
        return True
    sp = os.path.join(ACCOUNTS_DIR, phone)
    if not os.path.exists(sp + ".session"):
        print(f"[MONITOR] Session YOK: {sp}.session")
        return False
    print(f"[MONITOR] Session bulundu: {sp}.session")
    c = TelegramClient(sp, API_ID, API_HASH)
    await c.connect()
    authorized = await c.is_user_authorized()
    print(f"[MONITOR] is_user_authorized={authorized}")
    if not authorized:
        await c.disconnect()
        print(f"[MONITOR] Yetkisiz, kesildi: {phone}")
        return False
    _attach_handler(c, phone)
    monitor_clients[phone] = c
    print(f"[MONITOR] Handler baglandi: {phone}")
    return True

async def _init_monitors():
    nums = load_numbers()
    print(f"[MONITOR] _init_monitors: {len(nums)} numara")
    for n in nums:
        await _start_monitor(n["phone"])

# ── FIX 1 & 6: Sıkı Telegram kod çıkarıcı ─────────────────────
def _extract_code_strict(text):
    """Telegram doğrulama kodunu metinden çıkarır."""
    import re
    # Önce spesifik formatları dene
    specific = [
        r'(?:login code|your code)[:\s]+(\d{5,6})',
        r'(?:verification code)[:\s]+(\d{5,6})',
        r'(?:doğrulama kodu|giriş kodu|kod)[:\s]+(\d{5,6})',
        r'(?:code is|code:)\s*(\d{5,6})',
        r'(\d{5,6})\s*(?:is your|kodunuz)',
    ]
    for pat in specific:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    # Genel: metindeki ilk 5-6 haneli sayıyı al
    m = re.search(r'\b(\d{5,6})\b', text)
    if m:
        return m.group(1)
    return None

# ──────────────────────────────────────────────────────────────
# HTML ŞABLONLARI
# ──────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<title>Numarium · Giriş</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Orbitron:wght@700;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#04070f;--s1:#080e1c;--s2:#0c1526;--bd:#152035;--ac:#7c3aed;--ac2:#a855f7;--gr:#10b981;--rd:#ef4444;--tx:#cbd5e1;--mt:#334155;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--tx);font-family:'Space Grotesk',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;overflow:hidden;}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 60% at 50% -20%,rgba(124,58,237,.18),transparent);pointer-events:none;}
body::after{content:'';position:fixed;inset:0;background-image:radial-gradient(rgba(124,58,237,.07) 1px,transparent 1px);background-size:28px 28px;pointer-events:none;}
.box{background:var(--s1);border:1px solid var(--bd);border-radius:24px;padding:36px 28px;width:100%;max-width:380px;position:relative;z-index:1;box-shadow:0 0 60px rgba(124,58,237,.12);}
.logo{text-align:center;margin-bottom:28px;}
.logo-icon{font-size:2.4rem;margin-bottom:8px;}
.logo-text{font-family:'Orbitron',sans-serif;font-size:1.6rem;font-weight:900;letter-spacing:.05em;background:linear-gradient(135deg,#a855f7,#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.logo-sub{font-size:.65rem;color:var(--mt);letter-spacing:.18em;text-transform:uppercase;margin-top:4px;}
.tabs{display:flex;background:var(--bg);border-radius:12px;padding:4px;margin-bottom:24px;}
.tab{flex:1;padding:9px;text-align:center;font-size:.78rem;font-weight:600;border-radius:9px;cursor:pointer;transition:.2s;color:var(--mt);border:none;background:transparent;font-family:'Space Grotesk',sans-serif;}
.tab.active{background:var(--s2);color:#fff;border:1px solid var(--bd);}
.field-label{font-size:.65rem;color:var(--mt);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px;display:block;}
.field{width:100%;background:var(--bg);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;color:#fff;font-family:'Space Grotesk',sans-serif;font-size:.88rem;outline:none;transition:.2s;margin-bottom:14px;}
.field:focus{border-color:var(--ac2);box-shadow:0 0 0 3px rgba(168,85,247,.1);}
.btn{width:100%;padding:13px;border:none;border-radius:10px;background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:.88rem;letter-spacing:.04em;cursor:pointer;transition:.2s;margin-top:4px;}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px);}
/* FIX 7: Google ile giriş butonu */
.btn-google{width:100%;padding:13px;border:1px solid var(--bd);border-radius:10px;background:var(--s2);color:#fff;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:.85rem;cursor:pointer;transition:.2s;display:flex;align-items:center;justify-content:center;gap:10px;text-decoration:none;margin-top:10px;}
.btn-google:hover{border-color:var(--ac2);background:var(--bg);}
.divider{display:flex;align-items:center;gap:10px;margin:14px 0;}
.divider-line{flex:1;height:1px;background:var(--bd);}
.divider-text{font-size:.62rem;color:var(--mt);letter-spacing:.1em;text-transform:uppercase;}
.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);color:var(--rd);font-size:.72rem;padding:10px 14px;border-radius:9px;margin-bottom:14px;display:{% if error %}block{% else %}none{% endif %};}
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <div class="logo-icon">◈</div>
    <div class="logo-text">NUMARIUM</div>
    <div class="logo-sub">Sanal Numara Platformu</div>
  </div>
  <div class="tabs">
    <button class="tab {% if mode=='login' %}active{% endif %}" onclick="sw('login')" id="tl">GİRİŞ YAP</button>
    <button class="tab {% if mode=='register' %}active{% endif %}" onclick="sw('register')" id="tr">KAYIT OL</button>
  </div>
  <div class="err">{{ error }}</div>
  <form method="POST" id="af" autocomplete="off">
    <input type="hidden" name="mode" id="mi" value="{{ mode }}">
    <!-- FIX 3: autocomplete="off" + readonly trick – Google şifre uyarısını engeller -->
    <label class="field-label">Kullanıcı Adı</label>
    <input class="field" type="text" name="username" placeholder="kullanici_adiniz" required autocomplete="off" readonly onfocus="this.removeAttribute('readonly')">
    <label class="field-label">Şifre</label>
    <input class="field" type="password" name="password" placeholder="••••••••" required autocomplete="new-password" readonly onfocus="this.removeAttribute('readonly')">
    <button type="submit" class="btn" id="sb">GİRİŞ YAP</button>
  </form>
  <!-- FIX 7: Google ile giriş -->
  <div class="divider">
    <div class="divider-line"></div>
    <div class="divider-text">veya</div>
    <div class="divider-line"></div>
  </div>
  <a href="/auth/google" class="btn-google">
    <svg width="18" height="18" viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
    Google ile Giriş Yap
  </a>
</div>
<script>
function sw(t){
  document.getElementById('mi').value=t;
  document.getElementById('tl').classList.toggle('active',t==='login');
  document.getElementById('tr').classList.toggle('active',t==='register');
  document.getElementById('sb').textContent=t==='login'?'GİRİŞ YAP':'KAYIT OL';
}
sw('{{ mode }}');
</script>
</body>
</html>"""


MAIN_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>Numarium</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Orbitron:wght@700;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#04070f;--s1:#080e1c;--s2:#0c1526;--bd:#152035;--ac:#7c3aed;--ac2:#a855f7;--gr:#10b981;--grl:rgba(16,185,129,.12);--grd:rgba(16,185,129,.2);--rd:#ef4444;--rdl:rgba(239,68,68,.12);--gld:#f59e0b;--gldl:rgba(245,158,11,.12);--tx:#cbd5e1;--mt:#334155;--mt2:#1e2d3d;}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
html,body{overflow-x:hidden;}
body{background:var(--bg);color:var(--tx);font-family:'Space Grotesk',sans-serif;min-height:100vh;padding-bottom:80px;}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 100% 50% at 50% 0%,rgba(124,58,237,.1),transparent 60%);pointer-events:none;z-index:0;}
.wrap{max-width:480px;margin:0 auto;padding:0 14px;position:relative;z-index:1;}

/* Topbar */
.topbar{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;position:sticky;top:0;background:rgba(4,7,15,.95);backdrop-filter:blur(16px);z-index:100;border-bottom:1px solid var(--bd);margin-bottom:16px;width:100%;}
.logo{display:flex;align-items:center;gap:7px;}
.logo-mark{font-family:'Orbitron',sans-serif;font-size:1.15rem;font-weight:900;background:linear-gradient(135deg,#a855f7,#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:.04em;}
.logo-dot{width:6px;height:6px;border-radius:50%;background:var(--ac2);animation:pulse 2s ease-in-out infinite;flex-shrink:0;}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.4;transform:scale(.6);}}
.tbr{display:flex;align-items:center;gap:6px;}

.avatar-wrap{width:34px;height:34px;background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.avatar-wrap svg{width:18px;height:18px;fill:var(--ac2);}
.admin-badge{background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);color:var(--ac2);font-size:.58rem;padding:3px 8px;border-radius:20px;letter-spacing:.08em;font-weight:600;}
.coin-row{display:flex;align-items:center;gap:5px;background:var(--s1);border:1px solid var(--bd);border-radius:10px;padding:0 10px;height:36px;}
.coin-val{font-size:.82rem;font-weight:700;color:var(--gld);}
.coin-lbl{font-size:.55rem;color:var(--mt);letter-spacing:.06em;}
.btn-plus{width:36px;height:36px;border-radius:10px;background:var(--gldl);border:1px solid rgba(245,158,11,.25);color:var(--gld);font-size:1.1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.btn-exit{background:var(--rdl);border:1px solid rgba(239,68,68,.2);color:var(--rd);font-size:.6rem;height:36px;padding:0 12px;border-radius:10px;cursor:pointer;font-family:'Space Grotesk',sans-serif;font-weight:600;white-space:nowrap;display:flex;align-items:center;}

/* Stat bar */
.stat-bar{background:var(--s1);border:1px solid var(--bd);border-radius:14px;padding:14px 16px;margin-bottom:18px;display:flex;align-items:center;gap:12px;}
.stat-em{font-size:1.5rem;}
.stat-v{font-family:'Orbitron',sans-serif;font-size:1.4rem;font-weight:700;color:var(--ac2);}
.stat-l{font-size:.62rem;color:var(--mt);letter-spacing:.1em;text-transform:uppercase;margin-top:1px;}

/* FIX 4: Sekme navigasyonu – yatay tab bar */
.tab-nav{display:flex;background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:4px;margin-bottom:18px;gap:4px;}
.tab-nav-btn{flex:1;padding:9px;text-align:center;font-size:.72rem;font-weight:700;border-radius:9px;cursor:pointer;transition:.18s;color:var(--mt);border:none;background:transparent;font-family:'Space Grotesk',sans-serif;letter-spacing:.06em;text-transform:uppercase;}
.tab-nav-btn.active{background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;box-shadow:0 2px 12px rgba(124,58,237,.3);}

.tab-panel{display:none;}
.tab-panel.active{display:block;}

/* Section heading */
.sec-head{font-size:.65rem;font-weight:700;color:var(--mt);letter-spacing:.14em;text-transform:uppercase;margin:20px 0 10px;display:flex;align-items:center;gap:8px;}
.sec-head::after{content:'';flex:1;height:1px;background:var(--bd);}

/* Card wrapper */
.cw{position:relative;overflow:hidden;border-radius:14px;margin-bottom:10px;}
.ci{transition:transform .22s cubic-bezier(.4,0,.2,1);background:var(--s1);border:1px solid var(--bd);border-radius:14px;padding:14px 15px;display:flex;align-items:center;gap:11px;cursor:default;user-select:none;-webkit-user-select:none;}
.ci.dim{opacity:.45;}

/* Swipe actions */
.ca{position:absolute;right:0;top:0;bottom:0;width:0;overflow:hidden;display:flex;align-items:stretch;justify-content:flex-end;background:#02040a;transition:width .22s cubic-bezier(.4,0,.2,1);border-radius:0 14px 14px 0;}
.ab{min-width:52px;border:none;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:1rem;flex-shrink:0;font-weight:700;padding:0 6px;}
.ab-lbl{font-size:.48rem;letter-spacing:.05em;text-transform:uppercase;font-weight:600;}
.ab-del{background:rgba(239,68,68,.18);color:var(--rd);}
.ab-hid{background:rgba(51,65,85,.25);color:var(--mt);}
.ab-min{background:var(--gldl);color:var(--gld);}
.ab-pls{background:var(--grl);color:var(--gr);}

/* Flag */
.flag{width:42px;height:42px;background:rgba(124,58,237,.08);border:1px solid rgba(124,58,237,.15);border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:1.3rem;flex-shrink:0;}

.cinfo{flex:1;min-width:0;}
.tg-name{font-size:.9rem;font-weight:700;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.tg-user{font-size:.62rem;color:var(--ac2);margin-top:2px;}
.phone-reveal{font-size:.8rem;color:var(--gr);font-weight:600;margin-top:3px;letter-spacing:.04em;}
.cntry{font-size:.6rem;color:var(--mt);margin-top:3px;}
.admin-phone{font-size:.7rem;color:var(--tx);margin-top:2px;font-weight:500;}

.cr{display:flex;align-items:center;gap:7px;flex-shrink:0;}
.cost-tag{background:var(--gldl);border:1px solid rgba(245,158,11,.2);color:var(--gld);font-size:.7rem;font-weight:700;padding:4px 8px;border-radius:8px;white-space:nowrap;}
.btn-buy{background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;border:none;padding:9px 14px;border-radius:9px;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:.72rem;letter-spacing:.03em;cursor:pointer;transition:.15s;white-space:nowrap;}
.btn-buy:hover{filter:brightness(1.1);}
.btn-buy:disabled{background:var(--mt2);color:var(--mt);cursor:not-allowed;filter:none;}
.pill-sold{background:var(--rdl);border:1px solid rgba(239,68,68,.2);color:var(--rd);font-size:.6rem;padding:4px 8px;border-radius:8px;}
.pill-mine{background:var(--grl);border:1px solid rgba(16,185,129,.25);color:var(--gr);font-size:.6rem;padding:4px 8px;border-radius:8px;}
.price-label{font-size:.6rem;color:var(--mt);margin-top:3px;text-align:right;}

/* SMS inbox */
.sms-box{background:rgba(0,0,0,.35);border:1px solid var(--bd);border-top:none;border-radius:0 0 14px 14px;padding:12px 14px;margin-top:-4px;margin-bottom:10px;}
.sms-hdr{font-size:.58rem;color:var(--mt);letter-spacing:.12em;text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:6px;}
.sms-dot{width:5px;height:5px;border-radius:50%;background:var(--gr);animation:pulse 1.8s ease-in-out infinite;}
.sms-empty{font-size:.7rem;color:var(--mt);text-align:center;padding:14px 0;}

/* Büyük kod kutusu */
.code-box{background:var(--grl);border:1px solid var(--grd);border-radius:12px;padding:16px;text-align:center;}
.code-lbl{font-size:.6rem;color:var(--gr);letter-spacing:.14em;text-transform:uppercase;margin-bottom:8px;font-weight:600;}
.code-num{font-family:'Orbitron',sans-serif;font-size:2.2rem;font-weight:900;color:#fff;letter-spacing:.3em;line-height:1;}
.code-time{font-size:.56rem;color:rgba(16,185,129,.6);margin-top:8px;}
.code-copy{display:inline-flex;align-items:center;gap:5px;background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.25);color:var(--gr);font-size:.6rem;font-weight:700;padding:5px 12px;border-radius:20px;cursor:pointer;margin-top:10px;font-family:'Space Grotesk',sans-serif;letter-spacing:.06em;border:none;}

/* FIX 5: 2FA kutusu */
.twofa-box{display:flex;align-items:center;justify-content:space-between;background:rgba(124,58,237,.08);border:1px solid rgba(124,58,237,.2);border-radius:10px;padding:11px 14px;margin-top:8px;}
.twofa-lbl{font-size:.6rem;color:var(--ac2);font-weight:600;letter-spacing:.08em;text-transform:uppercase;}
.twofa-val{font-size:.95rem;font-weight:700;color:#fff;letter-spacing:.06em;font-family:'Orbitron',sans-serif;}

/* FIX 2: İptal butonu */
.btn-cancel{display:inline-flex;align-items:center;gap:5px;background:var(--rdl);border:1px solid rgba(239,68,68,.25);color:var(--rd);font-size:.6rem;font-weight:700;padding:5px 12px;border-radius:20px;cursor:pointer;margin-top:8px;font-family:'Space Grotesk',sans-serif;letter-spacing:.06em;}
.countdown-bar{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);border-radius:8px;padding:7px 12px;margin-top:8px;font-size:.62rem;color:var(--gld);text-align:center;}

.pgroup{margin-bottom:10px;}
.pgroup .cw{margin-bottom:0;border-radius:14px 14px 0 0;}
.pgroup .ci{border-radius:14px 14px 0 0 !important;}

/* Admin form */
.admin-sec{margin-top:26px;border-top:1px solid var(--bd);padding-top:22px;}
.form-box{background:var(--s1);border:1px solid var(--bd);border-radius:14px;padding:18px;}
.fg{margin-bottom:14px;}
.fg label{display:block;font-size:.6rem;color:var(--mt);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px;font-weight:600;}
.fg input{width:100%;background:var(--bg);border:1px solid var(--bd);border-radius:9px;padding:11px 13px;color:#fff;font-family:'Space Grotesk',sans-serif;font-size:.85rem;outline:none;transition:.2s;}
.fg input:focus{border-color:var(--ac2);box-shadow:0 0 0 3px rgba(168,85,247,.08);}
.fg input::placeholder{color:var(--mt);}

.phone-wrap{display:flex;align-items:center;background:var(--bg);border:1px solid var(--bd);border-radius:9px;overflow:hidden;transition:.2s;}
.phone-wrap:focus-within{border-color:var(--ac2);box-shadow:0 0 0 3px rgba(168,85,247,.08);}
.phone-flag-preview{font-size:1.25rem;padding:0 10px 0 13px;flex-shrink:0;line-height:1;display:flex;align-items:center;}
.phone-sep{width:1px;height:24px;background:var(--bd);flex-shrink:0;}
.phone-wrap input{flex:1;background:transparent;border:none;padding:11px 13px;color:#fff;font-family:'Space Grotesk',sans-serif;font-size:.85rem;outline:none;}
.phone-wrap input::placeholder{color:var(--mt);}

.step{display:none;}
.step.on{display:block;}
.step-info{font-size:.72rem;color:var(--ac2);background:rgba(124,58,237,.08);border:1px solid rgba(124,58,237,.2);border-radius:9px;padding:10px 13px;margin-bottom:14px;}
.btn-go{background:linear-gradient(135deg,#10b981,#059669);color:#fff;border:none;padding:12px 20px;border-radius:9px;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:.82rem;letter-spacing:.04em;cursor:pointer;transition:.15s;width:100%;}
.btn-go:hover{filter:brightness(1.1);}
.btn-go:disabled{background:var(--mt2);color:var(--mt);cursor:not-allowed;filter:none;}
.btn-back{background:transparent;border:1px solid var(--bd);color:var(--mt);padding:9px 16px;border-radius:9px;font-family:'Space Grotesk',sans-serif;font-size:.75rem;cursor:pointer;margin-bottom:10px;}

/* Toast */
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:999;display:flex;flex-direction:column;align-items:center;gap:7px;width:88%;max-width:360px;pointer-events:none;}
.ti{background:var(--s2);border:1px solid var(--bd);border-radius:11px;padding:11px 16px;font-size:.76rem;color:#fff;animation:tIn .22s ease;box-shadow:0 8px 32px rgba(0,0,0,.5);text-align:center;width:100%;}
@keyframes tIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.ti.ok{border-color:rgba(16,185,129,.35);color:var(--gr);}
.ti.er{border-color:rgba(239,68,68,.35);color:var(--rd);}

/* Coin modal */
.ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.78);backdrop-filter:blur(6px);z-index:500;align-items:flex-end;justify-content:center;touch-action:none;}
.ov.show{display:flex;}
.modal{background:var(--s1);border:1px solid var(--bd);border-radius:20px 20px 0 0;padding:0 0 44px;width:100%;max-width:480px;animation:mUp .25s cubic-bezier(.4,0,.2,1);will-change:transform;transition:transform .1s linear;}
@keyframes mUp{from{transform:translateY(100%);}to{transform:translateY(0);}}
.mhandle-area{padding:14px 20px 0;cursor:grab;touch-action:none;}
.mhandle{width:38px;height:4px;background:var(--bd);border-radius:2px;margin:0 auto 16px;}
.modal-body{padding:0 20px;}
.mtitle{font-family:'Orbitron',sans-serif;font-size:.95rem;font-weight:700;color:#fff;margin-bottom:6px;}
.msub{font-size:.7rem;color:var(--mt);line-height:1.65;margin-bottom:18px;}
.pkg{display:flex;align-items:center;justify-content:space-between;background:var(--bg);border:1px solid var(--bd);border-radius:10px;padding:13px 14px;margin-bottom:7px;}
.pn{font-size:.8rem;font-weight:700;color:var(--gld);}
.pp{font-size:.75rem;color:#fff;font-weight:600;}
.tgbtn{display:flex;align-items:center;justify-content:center;gap:9px;background:rgba(124,58,237,.1);border:1px solid rgba(124,58,237,.25);color:var(--ac2);border-radius:10px;padding:13px;font-family:'Space Grotesk',sans-serif;font-size:.78rem;font-weight:700;cursor:pointer;text-decoration:none;transition:.15s;margin-top:14px;}
.tgbtn:hover{background:rgba(124,58,237,.18);}
.mclose{width:100%;padding:11px;background:transparent;border:1px solid var(--bd);border-radius:10px;color:var(--mt);font-family:'Space Grotesk',sans-serif;font-size:.75rem;cursor:pointer;margin-top:10px;}

/* Empty */
.empty{text-align:center;padding:44px 0;color:var(--mt);}
.empty-ico{font-size:2.2rem;margin-bottom:10px;}
.empty p{font-size:.75rem;line-height:1.7;}

/* User Management */
.user-search{width:100%;background:var(--bg);border:1px solid var(--bd);border-radius:9px;padding:10px 13px;color:#fff;font-family:'Space Grotesk',sans-serif;font-size:.83rem;outline:none;margin-bottom:12px;}
.user-search:focus{border-color:var(--ac2);}
.user-search::placeholder{color:var(--mt);}
.ucard{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:12px 14px;margin-bottom:8px;}
.ucard-top{display:flex;align-items:center;gap:10px;}
.uinfo{flex:1;min-width:0;}
.uname{font-size:.85rem;font-weight:700;color:#fff;}
.upw{font-size:.7rem;color:var(--mt);margin-top:3px;display:flex;align-items:center;gap:4px;}
.upw-val{color:var(--tx);font-weight:600;letter-spacing:.04em;}
.ucoins{font-size:.75rem;color:var(--gld);font-weight:600;white-space:nowrap;}
.ucoin-ctrl{display:flex;align-items:center;gap:5px;margin-top:9px;}
.ucbtn{width:32px;height:32px;border-radius:8px;border:none;cursor:pointer;font-size:.85rem;display:flex;align-items:center;justify-content:center;font-weight:700;}
.ucbtn-add{background:var(--grl);color:var(--gr);}
.ucbtn-sub{background:var(--rdl);color:var(--rd);}
.ucoin-inp{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:7px;padding:6px 9px;color:#fff;font-family:'Space Grotesk',sans-serif;font-size:.78rem;text-align:center;outline:none;}
.ucoin-inp:focus{border-color:var(--ac2);}

@keyframes si{from{opacity:0;transform:translateY(5px);}to{opacity:1;transform:translateY(0);}}
.cw{animation:si .28s ease;}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">
    <div class="logo-dot"></div>
    <div class="logo-mark">NUMARIUM</div>
  </div>
  <div class="tbr">
    <div class="avatar-wrap" title="{{ username }}">
      <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/>
      </svg>
    </div>
    {% if is_admin %}<span class="admin-badge">ADMİN</span>{% endif %}
    {% if not is_admin %}
    <div class="coin-row">
      <div>
        <div class="coin-val" id="cdisplay">{{ coins }}</div>
        <div class="coin-lbl">COIN</div>
      </div>
    </div>
    <button class="btn-plus" title="Coin Yükle" onclick="showModal()">＋</button>
    {% endif %}
    <button class="btn-exit" onclick="location.href='/logout'">ÇIKIŞ</button>
  </div>
</div>

<div class="wrap">
  <div class="stat-bar">
    <div class="stat-em">📱</div>
    <div>
      <div class="stat-v" id="total-cnt">–</div>
      <div class="stat-l">Mevcut Numara</div>
    </div>
  </div>

  <!-- FIX 4: Kullanıcı – Yatay sekme navigasyonu -->
  {% if not is_admin %}
  <div class="tab-nav">
    <button class="tab-nav-btn active" id="tab-btn-nums" onclick="switchTab('nums')">📱 Numaralar</button>
    <button class="tab-nav-btn" id="tab-btn-purchased" onclick="switchTab('purchased')">✅ Satın Alınanlar</button>
  </div>

  <div class="tab-panel active" id="panel-nums">
    <div id="list-available"></div>
  </div>
  <div class="tab-panel" id="panel-purchased">
    <div id="list-purchased"></div>
  </div>
  {% endif %}

  <!-- Admin: Tüm Numaralar -->
  {% if is_admin %}
  <div id="sec-all">
    <div class="sec-head">Tüm Numaralar</div>
    <div id="list-admin"></div>
  </div>
  <div id="sec-sold" style="display:none;">
    <div class="sec-head">Satılan Numaralar</div>
    <div id="list-sold"></div>
  </div>

  <!-- Admin: Kullanıcı Yönetimi -->
  <div class="admin-sec">
    <div class="sec-head">Kullanıcılar</div>
    <div class="form-box">
      <input class="user-search" id="user-search" placeholder="🔍 Kullanıcı ara..." oninput="filterUsers()">
      <div id="user-list"><div class="empty"><div class="empty-ico">👥</div><p>Yükleniyor...</p></div></div>
    </div>
  </div>

  <!-- Admin: Numara Ekle -->
  <div class="admin-sec">
    <div class="sec-head">Numara Ekle</div>
    <div class="form-box">
      <div class="step on" id="step1">
        <div class="fg">
          <label>Telefon Numarası</label>
          <div class="phone-wrap">
            <div class="phone-flag-preview" id="a-phone-flag">🌍</div>
            <div class="phone-sep"></div>
            <input type="tel" id="a-phone" placeholder="+905551234567" oninput="detectCountry(this.value)" autocomplete="off">
          </div>
        </div>
        <div class="fg">
          <label>Coin Fiyatı</label>
          <input type="number" id="a-cost" placeholder="50" min="1" value="50">
        </div>
        <button class="btn-go" id="btn-step1" onclick="reqCode()">📩 KOD GÖNDER</button>
      </div>

      <div class="step" id="step2">
        <button class="btn-back" onclick="goStep(1)">← Geri</button>
        <div class="step-info" id="step2-info">Kodu girin</div>
        <div class="fg">
          <label>Doğrulama Kodu</label>
          <input type="text" id="a-code" placeholder="12345" maxlength="10" inputmode="numeric">
        </div>
        <button class="btn-go" onclick="verifyCode()">✓ DOĞRULA</button>
      </div>

      <div class="step" id="step3">
        <button class="btn-back" onclick="goStep(2)">← Geri</button>
        <div class="step-info">Bu hesap 2FA korumalı. Şifreyi girin:</div>
        <div class="fg">
          <label>2FA Şifresi</label>
          <input type="password" id="a-2fa" placeholder="••••••••" autocomplete="off">
        </div>
        <button class="btn-go" onclick="verify2FA()">🔓 GİRİŞ YAP</button>
      </div>
    </div>
  </div>
  {% endif %}
</div>

{% if not is_admin %}
<div class="ov" id="cmodal" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="cmodal-inner">
    <div class="mhandle-area" id="mhandle-area">
      <div class="mhandle"></div>
    </div>
    <div class="modal-body">
      <div class="mtitle">🪙 Coin Yükle</div>
      <p class="msub">Numara satın almak için hesabınıza coin yükleyin. Telegram üzerinden sipariş verin.</p>
      <div class="pkg"><span class="pn">🪙 100 Coin</span><span class="pp">5 ₺</span></div>
      <div class="pkg"><span class="pn">🪙 500 Coin</span><span class="pp">20 ₺</span></div>
      <div class="pkg"><span class="pn">🪙 1.000 Coin</span><span class="pp">35 ₺</span></div>
      <div class="pkg"><span class="pn">🪙 5.000 Coin <small style="color:#10b981;font-size:.6rem;">EN POPÜLER</small></span><span class="pp">150 ₺</span></div>
      <a href="https://t.me/mustafapro" target="_blank" class="tgbtn">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12l-6.871 4.326-2.962-.924c-.643-.204-.657-.643.136-.953l11.57-4.461c.537-.194 1.006.131.833.941z"/></svg>
        @mustafapro'dan Sipariş Ver
      </a>
      <button class="mclose" onclick="closeModal()">Kapat</button>
    </div>
  </div>
</div>
{% endif %}

<div id="toast"></div>

<script>
const ME = "{{ username }}";
const IS_ADMIN = {{ 'true' if is_admin else 'false' }};
let _last = null;
let _addPhone = '';
let _touchX = 0;
let _openCard = null;

// ── FIX 4: Sekme geçişi ─────────────────────────────────────
function switchTab(tab) {
  ['nums','purchased'].forEach(t => {
    document.getElementById('tab-btn-' + t).classList.toggle('active', t === tab);
    document.getElementById('panel-' + t).classList.toggle('active', t === tab);
  });
}

// ── Ülke haritası ───────────────────────────────────────────
const CMAP_JS = {
  '+971':['🇦🇪','BAE'],'+966':['🇸🇦','S.Arabistan'],
  '+380':['🇺🇦','Ukrayna'],'+358':['🇫🇮','Finlandiya'],
  '+90':['🇹🇷','Türkiye'],'+44':['🇬🇧','İngiltere'],
  '+49':['🇩🇪','Almanya'],'+33':['🇫🇷','Fransa'],
  '+39':['🇮🇹','İtalya'],'+34':['🇪🇸','İspanya'],
  '+31':['🇳🇱','Hollanda'],'+46':['🇸🇪','İsveç'],
  '+91':['🇮🇳','Hindistan'],'+86':['🇨🇳','Çin'],
  '+81':['🇯🇵','Japonya'],'+55':['🇧🇷','Brezilya'],
  '+82':['🇰🇷','G.Kore'],'+48':['🇵🇱','Polonya'],
  '+20':['🇪🇬','Mısır'],'+47':['🇳🇴','Norveç'],
  '+45':['🇩🇰','Danimarka'],'+32':['🇧🇪','Belçika'],
  '+41':['🇨🇭','İsviçre'],'+43':['🇦🇹','Avusturya'],
  '+30':['🇬🇷','Yunanistan'],'+7':['🇷🇺','Rusya'],
  '+1':['🇺🇸','ABD'],
};

function detectCountry(val) {
  const el = document.getElementById('a-phone-flag');
  if (!el) return;
  const sorted = Object.keys(CMAP_JS).sort((a,b) => b.length - a.length);
  for (const k of sorted) {
    if (val.startsWith(k)) { el.textContent = CMAP_JS[k][0]; return; }
  }
  el.textContent = '🌍';
}

// ── Toast ────────────────────────────────────────────────────
function toast(msg, type='ok') {
  const d = document.createElement('div');
  d.className = 'ti ' + type;
  d.textContent = msg;
  document.getElementById('toast').appendChild(d);
  setTimeout(() => d.remove(), 3500);
}

// ── Coin modal ───────────────────────────────────────────────
function showModal() {
  document.getElementById('cmodal').classList.add('show');
  document.body.style.overflow = 'hidden';
}
function closeModal() {
  const modal = document.getElementById('cmodal-inner');
  modal.style.transition = 'transform .25s cubic-bezier(.4,0,.2,1)';
  modal.style.transform = 'translateY(100%)';
  setTimeout(() => {
    document.getElementById('cmodal').classList.remove('show');
    modal.style.transform = '';
    modal.style.transition = '';
    document.body.style.overflow = '';
  }, 240);
}

// Swipe-to-close
(function(){
  let startY = 0, dragging = false;
  const getHandle = () => document.getElementById('mhandle-area');
  const getModal  = () => document.getElementById('cmodal-inner');
  function onStart(e) { startY = (e.touches ? e.touches[0].clientY : e.clientY); dragging = true; const m = getModal(); if (m) m.style.transition = 'none'; }
  function onMove(e)  { if (!dragging) return; const y = (e.touches ? e.touches[0].clientY : e.clientY); const dy = Math.max(0, y - startY); const m = getModal(); if (m) m.style.transform = `translateY(${dy}px)`; e.preventDefault(); }
  function onEnd(e)   { if (!dragging) return; dragging = false; const y = (e.changedTouches ? e.changedTouches[0].clientY : e.clientY); const dy = y - startY; const m = getModal(); if (!m) return; if (dy > 100) { closeModal(); } else { m.style.transition = 'transform .2s cubic-bezier(.4,0,.2,1)'; m.style.transform = ''; setTimeout(() => { m.style.transition = ''; }, 210); } }
  document.addEventListener('DOMContentLoaded', () => { const h = getHandle(); if (!h) return; h.addEventListener('touchstart', onStart, {passive:false}); h.addEventListener('touchmove', onMove, {passive:false}); h.addEventListener('touchend', onEnd, {passive:true}); });
})();

document.addEventListener('DOMContentLoaded', () => {
  const ov = document.getElementById('cmodal');
  if (!ov) return;
  ov.addEventListener('touchmove', e => { if (e.target === ov) e.preventDefault(); }, {passive: false});
});

// ── Swipe (admin) ────────────────────────────────────────────
function tStart(e, id) { _touchX = e.touches[0].clientX; }
function tEnd(e, id) {
  const dx = e.changedTouches[0].clientX - _touchX;
  if (dx < -55) { if (_openCard && _openCard !== id) cardClose(_openCard); cardOpen(id); }
  else if (dx > 20) { cardClose(id); }
}
function cardOpen(id)  { const ci = document.getElementById('ci'+id); const ca = document.getElementById('ca'+id); if (ci) ci.style.transform = 'translateX(-172px)'; if (ca) ca.style.width = '172px'; _openCard = id; }
function cardClose(id) { const ci = document.getElementById('ci'+id); const ca = document.getElementById('ca'+id); if (ci) ci.style.transform = ''; if (ca) ca.style.width = '0'; if (_openCard === id) _openCard = null; }
document.addEventListener('touchstart', function(e) { if (_openCard && !e.target.closest('.cw')) cardClose(_openCard); });

// ── displayName ─────────────────────────────────────────────
function displayName(n) {
  const nm = n.tg_name || '';
  const un = n.tg_username ? '@' + n.tg_username : '';
  return nm || un || '?';
}

// ── FIX 1 & 6: Sıkı Telegram kod çıkarıcı ───────────────────
function extractTgCodeStrict(text) {
  // Sadece 5-6 haneli tam sayılar – Telegram kodu formatı
  const patterns = [
    /(?:login code|your code)[:\s]+(\d{5,6})/i,
    /(?:verification code)[:\s]+(\d{5,6})/i,
    /(?:doğrulama kodu)[:\s]+(\d{5,6})/i,
    /\b(\d{5,6})\b/,
  ];
  for (const pat of patterns) {
    const m = text.match(pat);
    if (m) return m[1];
  }
  return null;
}

function isTelegramAuthMsg(sender, text, code) {
  // Eğer backend kod çıkardıysa direkt geçerli say
  if (code) return true;
  const sl = (sender || '').toLowerCase();
  const tl = (text   || '').toLowerCase();
  return sl.includes('telegram') ||
         tl.includes('login code') ||
         tl.includes('verification code') ||
         tl.includes('doğrulama kodu') ||
         tl.includes('giriş kodu') ||
         tl.includes('your code') ||
         sender === '777000' ||
         /\b\d{5,6}\b/.test(text); // 5-6 haneli sayı varsa
}

// ── Render: Mevcut Numaralar ─────────────────────────────────
function renderAvailable(nums, coins) {
  const avail = nums.filter(n => !n.purchased_by);
  document.getElementById('total-cnt').textContent = avail.length;
  const listA = document.getElementById('list-available');
  if (avail.length === 0) {
    listA.innerHTML = `<div class="empty"><div class="empty-ico">📭</div><p>Şu an satışa açık numara yok.<br>Yakında yeni numaralar eklenecek.</p></div>`;
  } else {
    listA.innerHTML = avail.map(n => {
      const can = coins >= n.coin_cost;
      return `<div class="cw">
        <div class="ci">
          <div class="flag">${n.flag}</div>
          <div class="cinfo">
            <div class="tg-name">${displayName(n)}</div>
            ${n.tg_username ? `<div class="tg-user">@${n.tg_username}</div>` : ''}
            <div class="cntry">${n.country}</div>
          </div>
          <div class="cr">
            <span class="cost-tag">🪙 ${n.coin_cost}</span>
            <button class="btn-buy" onclick="buyNum('${n.buy_phone}',${n.coin_cost})" ${can?'':'disabled title="Yetersiz coin"'}>SATIN AL</button>
          </div>
        </div>
      </div>`;
    }).join('');
  }
}

// ── FIX 2 & 5: Render: Satın Alınanlar ──────────────────────
function renderPurchased(nums, coins) {
  const mine  = nums.filter(n => n.purchased_by === ME);
  const listP = document.getElementById('list-purchased');

  if (mine.length === 0) {
    listP.innerHTML = `<div class="empty"><div class="empty-ico">🛒</div><p>Henüz numara satın almadınız.</p></div>`;
    return;
  }

  const now = Date.now() / 1000;

  listP.innerHTML = mine.map(n => {
    const msgs   = n.messages || [];
    const tgMsg  = msgs.find(m => isTelegramAuthMsg(m.sender, m.text, m.code));

    // FIX 5: Kod kutusunu oluştur – sadece tek mesaj
    let inboxHtml = '';
    if (!tgMsg) {
      // Kod gelmedi – geri sayım göster
      const purchased_at = n.purchased_at || 0;
      const elapsed  = purchased_at ? Math.floor(now - purchased_at) : 0;
      const remaining = Math.max(0, 600 - elapsed); // 10 dk
      const mins = Math.floor(remaining / 60);
      const secs = remaining % 60;
      inboxHtml = `<div class="sms-empty">⏳ Telegram kodu bekleniyor...</div>
        <div class="countdown-bar">⏱ Otomatik iade: ${mins}:${String(secs).padStart(2,'0')}</div>`;
      // FIX 2: İptal butonu (5 dakika içinde)
      if (elapsed < 300) {
        inboxHtml += `<button class="btn-cancel" onclick="cancelNum('${n.buy_phone}')">✖ İptal Et & İade Al</button>`;
      }
    } else {
      // Backend'den gelen hazır kod varsa direkt kullan, yoksa metinden çıkar
      const code = tgMsg.code || extractTgCodeStrict(tgMsg.text);
      if (code) {
        inboxHtml = `
          <div class="code-box">
            <div class="code-lbl">Telegram Kodu</div>
            <div class="code-num" id="code_${n.buy_phone}">${code}</div>
            <div class="code-time">${tgMsg.time}</div>
            <button class="code-copy" onclick="copyCode('${n.buy_phone}')">📋 Kopyala</button>
          </div>`;
        // FIX 5: 2FA şifresi altında gösterilsin
        if (n.two_fa_password) {
          inboxHtml += `
          <div class="twofa-box">
            <div class="twofa-lbl">🔐 2FA Şifresi</div>
            <div class="twofa-val">${n.two_fa_password}</div>
          </div>`;
        }
      } else {
        // FIX 6: Geçersiz mesaj – kodu çıkaramazsa hiç gösterme
        inboxHtml = `<div class="sms-empty">⏳ Telegram kodu bekleniyor...</div>`;
      }
    }

    return `<div class="pgroup">
      <div class="cw">
        <div class="ci">
          <div class="flag">${n.flag}</div>
          <div class="cinfo">
            <div class="tg-name">${displayName(n)}</div>
            <div class="phone-reveal">📱 ${n.phone}</div>
            <div class="cntry">${n.country}</div>
          </div>
          <div class="cr"><span class="pill-mine">✓ ALINDI</span></div>
        </div>
      </div>
      <div class="sms-box">
        <div class="sms-hdr"><div class="sms-dot"></div>TELEGRAM KODU</div>
        ${inboxHtml}
      </div>
    </div>`;
  }).join('');
}

function copyCode(phone) {
  const el = document.getElementById('code_' + phone);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    toast('✅ Kod kopyalandı!', 'ok');
  }).catch(() => {
    toast(el.textContent.trim(), 'ok');
  });
}

function renderAdmin(nums) {
  const total = nums.filter(n => !n.purchased_by && !n.hidden).length;
  document.getElementById('total-cnt').textContent = total;

  const listA = document.getElementById('list-admin');
  const secS  = document.getElementById('sec-sold');
  const listS = document.getElementById('list-sold');

  if (nums.length === 0) {
    listA.innerHTML = `<div class="empty"><div class="empty-ico">📋</div><p>Henüz numara eklenmedi.</p></div>`;
  } else {
    listA.innerHTML = nums.map((n, idx) => {
      const isHid = n.hidden;
      return `<div class="cw" id="cw${idx}">
        <div class="ci${isHid?' dim':''}" id="ci${idx}"
             ontouchstart="tStart(event,${idx})" ontouchend="tEnd(event,${idx})">
          <div class="flag">${n.flag}</div>
          <div class="cinfo">
            <div class="tg-name">${displayName(n)}</div>
            <div class="admin-phone">${n.phone}</div>
            <div class="cntry">${n.country}</div>
          </div>
          <div class="cr">
            <div style="text-align:right;">
              <span class="cost-tag">🪙 ${n.coin_cost}</span>
              ${n.purchased_by
                ? `<div class="price-label" style="color:var(--rd);">→ ${n.purchased_by}</div>`
                : `<div class="price-label">${isHid?'GİZLİ':'MÜSAİT'}</div>`}
            </div>
          </div>
        </div>
        <div class="ca" id="ca${idx}">
          <button class="ab ab-min" onclick="updPrice('${n.phone}',-10)">
            <span>−10</span><span class="ab-lbl">FİYAT</span>
          </button>
          <button class="ab ab-pls" onclick="updPrice('${n.phone}',+10)">
            <span>+10</span><span class="ab-lbl">FİYAT</span>
          </button>
          <button class="ab ab-hid" onclick="toggleHide('${n.phone}',${idx})">
            <span>${isHid?'👁':'🙈'}</span><span class="ab-lbl">${isHid?'GÖSTER':'GİZLE'}</span>
          </button>
          <button class="ab ab-del" onclick="delNum('${n.phone}',${idx})">
            <span>🗑</span><span class="ab-lbl">SİL</span>
          </button>
        </div>
      </div>`;
    }).join('');
  }

  const sold = nums.filter(n => n.purchased_by);
  if (sold.length === 0) {
    secS.style.display = 'none';
  } else {
    secS.style.display = 'block';
    listS.innerHTML = sold.map(n => `<div class="cw">
      <div class="ci">
        <div class="flag">${n.flag}</div>
        <div class="cinfo">
          <div class="tg-name">${displayName(n)}</div>
          <div class="admin-phone">${n.phone}</div>
          <div class="cntry">${n.country}</div>
        </div>
        <div class="cr"><span class="pill-sold">→ ${n.purchased_by}</span></div>
      </div>
    </div>`).join('');
  }
}

async function fetchNums() {
  try {
    const r = await fetch('/api/numbers');
    const d = await r.json();
    const sig = JSON.stringify(d);
    if (sig === _last) return;
    _last = sig;
    if (IS_ADMIN) {
      renderAdmin(d.numbers);
    } else {
      document.getElementById('cdisplay').textContent = d.coins;
      renderAvailable(d.numbers, d.coins);
      renderPurchased(d.numbers, d.coins);
    }
  } catch(e) { console.error(e); }
}

// ── Satın Al ────────────────────────────────────────────────
async function buyNum(phone, cost) {
  try {
    const r = await fetch('/api/buy', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({phone})
    });
    const d = await r.json();
    if (d.ok) { toast('✅ Satın alındı!', 'ok'); _last = null; switchTab('purchased'); }
    else toast('⚠️ ' + d.error, 'er');
    fetchNums();
  } catch(e) { toast('Bağlantı hatası', 'er'); }
}

// ── FIX 2: İptal Et ────────────────────────────────────────
async function cancelNum(phone) {
  if (!confirm('Numarayı iptal edip coin iade almak istiyor musunuz?')) return;
  try {
    const r = await fetch('/api/cancel', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({phone})
    });
    const d = await r.json();
    if (d.ok) { toast('✅ İptal edildi, coin iade edildi.', 'ok'); _last = null; }
    else toast('⚠️ ' + d.error, 'er');
    fetchNums();
  } catch(e) { toast('Bağlantı hatası', 'er'); }
}

// ── Admin işlemleri ─────────────────────────────────────────
async function delNum(phone, idx) {
  if (!confirm('Bu numarayı silmek istediğinize emin misiniz?')) return;
  cardClose(idx);
  try {
    const r = await fetch('/api/admin/delete_number', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({phone})
    });
    const d = await r.json();
    if (d.ok) { toast('🗑️ Numara silindi', 'ok'); _last = null; }
    else toast('⚠️ ' + d.error, 'er');
    fetchNums();
  } catch(e) { toast('Hata', 'er'); }
}

async function updPrice(phone, delta) {
  try {
    await fetch('/api/admin/update_price', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({phone, delta})
    });
    _last = null; fetchNums();
  } catch(e) {}
}

async function toggleHide(phone, idx) {
  cardClose(idx);
  try {
    await fetch('/api/admin/toggle_hidden', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({phone})
    });
    _last = null; fetchNums();
  } catch(e) {}
}

function goStep(n) {
  ['step1','step2','step3'].forEach((s,i) => {
    document.getElementById(s).className = 'step' + (i===n-1?' on':'');
  });
}

async function reqCode() {
  const phone = document.getElementById('a-phone').value.trim();
  const cost  = parseInt(document.getElementById('a-cost').value) || 50;
  if (!phone) { toast('Telefon numarası boş', 'er'); return; }
  if (!phone.startsWith('+')) { toast('Numara + ile başlamalıdır (ör: +90555...)', 'er'); return; }
  const btn = document.getElementById('btn-step1');
  btn.disabled = true; btn.textContent = '📡 Bekleniyor...';
  try {
    const r = await fetch('/api/admin/request_code', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({phone, coin_cost:cost})
    });
    const d = await r.json();
    if (d.ok) {
      _addPhone = phone;
      document.getElementById('step2-info').textContent = phone + ' numarasına kod gönderildi.';
      goStep(2);
    } else { toast('⚠️ ' + d.error, 'er'); }
  } catch(e) { toast('Bağlantı hatası', 'er'); }
  btn.disabled = false; btn.textContent = '📩 KOD GÖNDER';
}

async function verifyCode() {
  const code = document.getElementById('a-code').value.trim();
  if (!code) { toast('Kod boş', 'er'); return; }
  try {
    const r = await fetch('/api/admin/verify_code', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phone: _addPhone, code})
    });
    const d = await r.json();
    if (d.ok) {
      toast('✅ ' + (d.tg_name||_addPhone) + ' eklendi!', 'ok');
      document.getElementById('a-phone').value = '';
      document.getElementById('a-code').value  = '';
      document.getElementById('a-phone-flag').textContent = '🌍';
      goStep(1); _last = null; fetchNums();
    } else if (d.needs_2fa) { goStep(3); }
    else { toast('⚠️ ' + d.error, 'er'); }
  } catch(e) { toast('Bağlantı hatası', 'er'); }
}

async function verify2FA() {
  const pw = document.getElementById('a-2fa').value.trim();
  if (!pw) { toast('Şifre boş', 'er'); return; }
  try {
    const r = await fetch('/api/admin/verify_code', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({phone: _addPhone, code: '', two_fa: pw})
    });
    const d = await r.json();
    if (d.ok) {
      toast('✅ ' + (d.tg_name||_addPhone) + ' eklendi!', 'ok');
      document.getElementById('a-phone').value = '';
      document.getElementById('a-2fa').value   = '';
      document.getElementById('a-phone-flag').textContent = '🌍';
      goStep(1); _last = null; fetchNums();
    } else { toast('⚠️ ' + d.error, 'er'); }
  } catch(e) { toast('Bağlantı hatası', 'er'); }
}

// ── Polling ──────────────────────────────────────────────────
fetchNums();
setInterval(fetchNums, 4000);

// ── Admin: Kullanıcı Yönetimi ────────────────────────────────
let _allUsers = [];

async function fetchUsers() {
  if (!IS_ADMIN) return;
  try {
    const r = await fetch('/api/admin/users');
    const d = await r.json();
    _allUsers = d.users || [];
    renderUsers(_allUsers);
  } catch(e) { console.error(e); }
}

function filterUsers() {
  const q = document.getElementById('user-search').value.toLowerCase();
  renderUsers(_allUsers.filter(u => u.username.toLowerCase().includes(q)));
}

function renderUsers(users) {
  const el = document.getElementById('user-list');
  if (!el) return;
  if (users.length === 0) {
    el.innerHTML = '<div class="empty"><p>Kullanıcı bulunamadı.</p></div>';
    return;
  }
  el.innerHTML = users.map(u => `
    <div class="ucard" id="uc_${u.username}">
      <div class="ucard-top">
        <div class="uinfo">
          <div class="uname">👤 ${u.username}</div>
          <div class="upw">🔑 <span class="upw-val">${u.password || '—'}</span></div>
        </div>
        <div class="ucoins" id="ucval_${u.username}">🪙 ${u.coins}</div>
      </div>
      <div class="ucoin-ctrl">
        <button class="ucbtn ucbtn-sub" title="Coin Sil" onclick="modCoins('${u.username}',-1)">−</button>
        <input class="ucoin-inp" id="uamt_${u.username}" type="number" value="100" min="1">
        <button class="ucbtn ucbtn-add" title="Coin Ekle" onclick="modCoins('${u.username}',1)">+</button>
      </div>
    </div>`).join('');
}

async function modCoins(username, sign) {
  const inp   = document.getElementById('uamt_' + username);
  const amt   = parseInt(inp ? inp.value : 100) || 100;
  const delta = sign * amt;
  try {
    const r = await fetch('/api/admin/update_coins', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username, delta})
    });
    const d = await r.json();
    if (d.ok) {
      const el = document.getElementById('ucval_' + username);
      if (el) el.textContent = '🪙 ' + d.coins;
      const u = _allUsers.find(x => x.username === username);
      if (u) u.coins = d.coins;
      toast((sign > 0 ? '✅ +' : '✅ ') + delta + ' coin → ' + username, 'ok');
    } else { toast('⚠️ ' + d.error, 'er'); }
  } catch(e) { toast('Bağlantı hatası', 'er'); }
}

if (IS_ADMIN) { fetchUsers(); setInterval(fetchUsers, 8000); }
</script>
</body>
</html>"""


# ── Flask Routes ───────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    if "username" not in fs:
        return redirect(url_for("login"))
    users    = load_users()
    username = fs["username"]
    coins    = users.get(username, {}).get("coins", 0)
    is_admin = (username == ADMIN_USER)
    return render_template_string(MAIN_HTML, username=username, coins=coins, is_admin=is_admin)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "username" in fs:
        return redirect(url_for("index"))
    error, mode = "", "login"
    if request.method == "POST":
        mode  = request.form.get("mode", "login")
        un    = request.form.get("username", "").strip()
        pw    = request.form.get("password", "").strip()
        users = load_users()
        if mode == "register":
            if un in users:         error = "Bu kullanıcı adı alınmış."
            elif len(un) < 3:       error = "En az 3 karakter gerekli."
            elif len(pw) < 4:       error = "Şifre en az 4 karakter olmalı."
            else:
                users[un] = {"password": pw, "coins": 0}
                save_users(users)
                fs["username"] = un
                return redirect(url_for("index"))
        else:
            u = users.get(un)
            if not u or u["password"] != pw:
                error = "Kullanıcı adı veya şifre hatalı."
            else:
                fs["username"] = un
                return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=error, mode=mode)


@app.route("/logout")
def logout():
    fs.clear()
    return redirect(url_for("login"))


# ── FIX 7: Google OAuth ────────────────────────────────────────
# Not: Çalışması için python-dotenv ve requests kütüphanesi gerekir.
# pip install requests
# Google Cloud Console'dan OAuth 2.0 Client ID alın ve yukarıdaki
# GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET değerlerini doldurun.

@app.route("/auth/google")
def auth_google():
    if GOOGLE_CLIENT_ID == "YOUR_GOOGLE_CLIENT_ID":
        # Credentials henüz ayarlanmamış — kullanıcıya bilgi ver
        return render_template_string("""<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Google OAuth Kurulumu</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet">
<style>
body{background:#04070f;color:#cbd5e1;font-family:'Space Grotesk',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}
.box{background:#080e1c;border:1px solid #152035;border-radius:20px;padding:32px 28px;max-width:420px;width:100%;}
h2{color:#a855f7;font-size:1.1rem;margin-bottom:16px;}
p{font-size:.85rem;line-height:1.6;color:#94a3b8;margin-bottom:12px;}
code{background:#0c1526;border:1px solid #152035;padding:2px 7px;border-radius:6px;font-size:.8rem;color:#10b981;}
ol{padding-left:20px;font-size:.83rem;color:#94a3b8;line-height:2;}
a.back{display:inline-block;margin-top:20px;background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;padding:10px 22px;border-radius:10px;text-decoration:none;font-weight:700;font-size:.85rem;}
</style></head>
<body><div class="box">
<h2>⚙️ Google OAuth Kurulumu Gerekli</h2>
<p>Google ile giriş için önce Google Cloud Console'dan OAuth credentials almanız gerekiyor.</p>
<ol>
  <li><a href="https://console.cloud.google.com" target="_blank" style="color:#a855f7;">console.cloud.google.com</a> adresine gidin</li>
  <li>Yeni proje oluşturun → <b>APIs &amp; Services → Credentials</b></li>
  <li><b>OAuth 2.0 Client ID</b> oluşturun (Web application)</li>
  <li>Redirect URI olarak ekleyin:<br><code>http://localhost:5000/auth/google/callback</code></li>
  <li><code>numarium.py</code> dosyasında şu satırları doldurun:<br>
    <code>GOOGLE_CLIENT_ID = "buraya_client_id"</code><br>
    <code>GOOGLE_CLIENT_SECRET = "buraya_secret"</code>
  </li>
  <li>Uygulamayı yeniden başlatın</li>
</ol>
<a href="/login" class="back">← Geri Dön</a>
</div></body></html>""")
    import urllib.parse
    state = secrets.token_hex(16)
    fs["oauth_state"] = state
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "prompt":        "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(url)


@app.route("/auth/google/callback")
def auth_google_callback():
    import urllib.request, urllib.parse
    # State doğrulama
    if request.args.get("state") != fs.get("oauth_state"):
        return redirect(url_for("login"))

    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))

    # Token al
    try:
        token_data = urllib.parse.urlencode({
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        }).encode()
        req  = urllib.request.Request("https://oauth2.googleapis.com/token", data=token_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        resp = urllib.request.urlopen(req, timeout=10)
        tok  = json.loads(resp.read())

        # Kullanıcı bilgisi al
        info_req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {tok['access_token']}"}
        )
        info_resp = urllib.request.urlopen(info_req, timeout=10)
        info      = json.loads(info_resp.read())

        google_id = info.get("sub", "")
        email     = info.get("email", "")
        name      = info.get("name",  "")

        # Kullanıcı adı: email'den @ öncesi, benzersiz yap
        base_un = email.split("@")[0].replace(".", "_").lower()
        users   = load_users()

        # Zaten kayıtlı mı? (google_id ile eşleş)
        un = None
        for u_key, u_val in users.items():
            if u_val.get("google_id") == google_id:
                un = u_key
                break

        if un is None:
            # Yeni kullanıcı
            un = base_un
            suffix = 2
            while un in users:
                un = f"{base_un}{suffix}"; suffix += 1
            users[un] = {
                "password":  hashlib.sha256(secrets.token_hex(32).encode()).hexdigest(),
                "coins":     0,
                "google_id": google_id,
                "email":     email,
                "name":      name,
            }
            save_users(users)

        fs["username"] = un
        return redirect(url_for("index"))

    except Exception as e:
        print(f"[Google OAuth] hata: {e}")
        return redirect(url_for("login"))


@app.route("/api/numbers")
def api_numbers():
    if "username" not in fs:
        return jsonify({"error": "Unauthorized"}), 401
    un       = fs["username"]
    is_admin = (un == ADMIN_USER)
    users    = load_users()
    coins    = users.get(un, {}).get("coins", 0)
    numbers  = load_numbers()
    msgs     = load_msgs()
    result   = []
    for n in numbers:
        phone         = n["phone"]
        flag, country = ci(phone)
        purchased_by  = n.get("purchased_by", "")
        hidden        = n.get("hidden", False)
        mine          = (purchased_by == un)

        if not is_admin and hidden and not mine:
            continue

        item = {
            "phone":        phone if (mine or is_admin) else "",
            "buy_phone":    phone,
            "flag":         flag,
            "country":      country,
            "coin_cost":    n.get("coin_cost", 50),
            "purchased_by": purchased_by,
            "purchased_at": n.get("purchased_at", 0),
            "tg_name":      n.get("tg_name", ""),
            "tg_username":  n.get("tg_username", ""),
            "hidden":       hidden,
            "code_received": n.get("code_received", False),
        }
        if mine:
            item["messages"]        = msgs.get(phone, [])
            item["two_fa_password"] = n.get("two_fa_password", "")
        result.append(item)

    return jsonify({"numbers": result, "coins": coins})


@app.route("/api/buy", methods=["POST"])
def api_buy():
    if "username" not in fs:
        return jsonify({"ok": False, "error": "Giriş yapın"}), 401
    data    = request.get_json()
    phone   = data.get("phone", "").strip()
    un      = fs["username"]
    users   = load_users()
    numbers = load_numbers()
    user    = users.get(un)
    if not user:
        return jsonify({"ok": False, "error": "Kullanıcı bulunamadı"})
    num = next((n for n in numbers if n["phone"] == phone), None)
    if not num:
        return jsonify({"ok": False, "error": "Numara bulunamadı"})
    if num.get("purchased_by"):
        return jsonify({"ok": False, "error": "Bu numara zaten satılmış"})
    if num.get("hidden"):
        return jsonify({"ok": False, "error": "Bu numara mevcut değil"})
    cost = num.get("coin_cost", 50)
    if user["coins"] < cost:
        return jsonify({"ok": False, "error": f"Yetersiz coin ({user['coins']}/{cost})"})
    user["coins"]      -= cost
    num["purchased_by"] = un
    num["purchased_at"] = time.time()  # FIX 2: Satın alma zamanını kaydet
    num["code_received"] = False
    msgs = load_msgs()
    if phone in msgs:
        del msgs[phone]
    save_msgs(msgs)
    save_users(users)
    save_numbers(numbers)
    return jsonify({"ok": True, "phone": phone})


# ── FIX 2: İptal API'si ───────────────────────────────────────
@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    if "username" not in fs:
        return jsonify({"ok": False, "error": "Giriş yapın"}), 401
    data    = request.get_json()
    phone   = data.get("phone", "").strip()
    un      = fs["username"]
    users   = load_users()
    numbers = load_numbers()
    num     = next((n for n in numbers if n["phone"] == phone), None)
    if not num:
        return jsonify({"ok": False, "error": "Numara bulunamadı"})
    if num.get("purchased_by") != un:
        return jsonify({"ok": False, "error": "Bu numara size ait değil"})
    if num.get("code_received"):
        return jsonify({"ok": False, "error": "Kod alındıktan sonra iptal edilemez"})
    purchased_at = num.get("purchased_at", 0)
    if time.time() - purchased_at > 300:  # 5 dakika
        return jsonify({"ok": False, "error": "İptal süresi (5 dakika) dolmuş"})
    cost = num.get("coin_cost", 50)
    if un in users:
        users[un]["coins"] = users[un].get("coins", 0) + cost
    num["purchased_by"] = ""
    num["purchased_at"] = 0
    num["code_received"] = False
    save_users(users)
    save_numbers(numbers)
    return jsonify({"ok": True})


@app.route("/api/admin/request_code", methods=["POST"])
def api_admin_request_code():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 403
    data      = request.get_json()
    phone     = data.get("phone", "").strip()
    coin_cost = int(data.get("coin_cost", 50))
    if not phone:
        return jsonify({"ok": False, "error": "Numara boş"})
    if not phone.startswith("+"):
        return jsonify({"ok": False, "error": "Numara + ile başlamalıdır"})
    if any(n["phone"] == phone for n in load_numbers()):
        return jsonify({"ok": False, "error": "Bu numara zaten kayıtlı"})

    async def _req():
        sp = os.path.join(ACCOUNTS_DIR, phone)
        c  = TelegramClient(sp, API_ID, API_HASH)
        await c.connect()
        r = await c.send_code_request(phone)
        pending_add[phone] = {"client": c, "phone_hash": r.phone_code_hash, "coin_cost": coin_cost}
        return {"ok": True}

    try:
        return jsonify(run_async(_req()))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/admin/verify_code", methods=["POST"])
def api_admin_verify_code():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 403
    data   = request.get_json()
    phone  = data.get("phone", "").strip()
    code   = data.get("code", "").strip()
    two_fa = data.get("two_fa", "").strip()

    async def _verify():
        p = pending_add.get(phone)
        if not p:
            return {"ok": False, "error": "Oturum süresi dolmuş, tekrar deneyin"}
        c = p["client"]
        try:
            if two_fa:
                await c.sign_in(password=two_fa)
            else:
                await c.sign_in(phone, code, phone_code_hash=p["phone_hash"])

            me      = await c.get_me()
            tg_name = " ".join(filter(None, [
                me.first_name or "", me.last_name or ""
            ])).strip()
            tg_user = me.username or ""

            nums = load_numbers()
            if not any(n["phone"] == phone for n in nums):
                nums.append({
                    "phone":           phone,
                    "coin_cost":       p["coin_cost"],
                    "purchased_by":    "",
                    "purchased_at":    0,
                    "code_received":   False,
                    "hidden":          False,
                    "tg_name":         tg_name,
                    "tg_username":     tg_user,
                    "two_fa_password": two_fa,
                })
                save_numbers(nums)

            _attach_handler(c, phone)
            monitor_clients[phone] = c
            del pending_add[phone]
            print(f"[VERIFY] Numara eklendi ve handler baglandi: {phone}")
            return {"ok": True, "tg_name": tg_name or phone}

        except SessionPasswordNeededError:
            return {"ok": False, "error": "2FA gerekli", "needs_2fa": True}
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            return {"ok": False, "error": "Kod hatalı veya süresi dolmuş"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    try:
        return jsonify(run_async(_verify()))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/admin/delete_number", methods=["POST"])
def api_admin_delete_number():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 403
    phone = request.get_json().get("phone", "").strip()
    nums  = [n for n in load_numbers() if n["phone"] != phone]
    save_numbers(nums)
    if phone in monitor_clients:
        asyncio.run_coroutine_threadsafe(monitor_clients[phone].disconnect(), _loop)
        del monitor_clients[phone]
    return jsonify({"ok": True})


@app.route("/api/admin/update_price", methods=["POST"])
def api_admin_update_price():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 403
    data  = request.get_json()
    phone = data.get("phone", "").strip()
    delta = int(data.get("delta", 0))
    nums  = load_numbers()
    for n in nums:
        if n["phone"] == phone:
            n["coin_cost"] = max(1, n.get("coin_cost", 50) + delta)
            break
    save_numbers(nums)
    return jsonify({"ok": True})


@app.route("/api/admin/toggle_hidden", methods=["POST"])
def api_admin_toggle_hidden():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 403
    phone = request.get_json().get("phone", "").strip()
    nums  = load_numbers()
    for n in nums:
        if n["phone"] == phone:
            n["hidden"] = not n.get("hidden", False)
            break
    save_numbers(nums)
    return jsonify({"ok": True})


@app.route("/api/admin/users")
def api_admin_users():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"error": "Yetkisiz"}), 403
    users  = load_users()
    result = [
        {
            "username": u,
            "coins":    d.get("coins", 0),
            "password": d.get("password", "—"),
        }
        for u, d in users.items()
        if u != ADMIN_USER
    ]
    result.sort(key=lambda x: x["username"])
    return jsonify({"users": result})


@app.route("/api/admin/update_coins", methods=["POST"])
def api_admin_update_coins():
    if "username" not in fs or fs["username"] != ADMIN_USER:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 403
    data   = request.get_json()
    target = data.get("username", "").strip()
    delta  = int(data.get("delta", 0))
    users  = load_users()
    if target not in users:
        return jsonify({"ok": False, "error": "Kullanıcı bulunamadı"})
    users[target]["coins"] = max(0, users[target].get("coins", 0) + delta)
    save_users(users)
    return jsonify({"ok": True, "coins": users[target]["coins"]})


# ── Web sunucusu ───────────────────────────────────────────────

def run_web():
    print("\n◈  Numarium — http://0.0.0.0:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


# ── Terminal ───────────────────────────────────────────────────

def terminal():
    run_async(_init_monitors(), timeout=60)

    print("\n" + "=" * 42)
    print("   ◈  Numarium — Terminal Yöneticisi")
    print("=" * 42)

    while True:
        print("\n[1] Numara listesi")
        print("[2] Çıkış")
        choice = input("\n→ Seçim: ").strip()

        if choice == "1":
            nums = load_numbers()
            if not nums:
                print("  Kayıtlı numara yok.")
            else:
                print(f"\n  Toplam {len(nums)} numara:\n")
                for n in nums:
                    flag, _ = ci(n["phone"])
                    mon_st  = "▶ İzleniyor" if n["phone"] in monitor_clients else "■ Pasif"
                    buyer   = f"→ {n['purchased_by']}" if n.get("purchased_by") else "Mevcut"
                    hidden  = " [GİZLİ]" if n.get("hidden") else ""
                    tgname  = n.get("tg_name", "?")
                    print(f"  {flag} {n['phone']}  |  {tgname}  |  🪙{n.get('coin_cost',50)}  |  {buyer}  |  {mon_st}{hidden}")

        elif choice == "2":
            print("\n  Çıkılıyor...\n")
            break


# ── Başlangıç ──────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    terminal()
