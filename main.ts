// ============================================================
// IPTV Proxy — Deno Deploy (v2 - TLS Retry Fix)
// Tek bağlantı prensibi: yayıncıya giden istek Deno isolate'inden
// tek IP olarak gider. Tüm izleyiciler aynı cache'lenmiş manifesti alır.
// FFmpeg yok — kaynak m3u8 doğrudan proxy'lenir.
// ============================================================

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

const SESSION_COOKIE = "iptv_session";
const SESSION_TTL_MS = 24 * 60 * 60 * 1000;
const MANIFEST_CACHE_MS = 3000;   // 3 saniye manifest cache
const SEGMENT_CACHE_MS = 30000;   // 30 saniye segment cache (aynı segment tekrar istenirse)

// ------------------------------------------------------------
// Kanallar — environment variable'dan okunur
// IPTV_CHANNELS env: JSON dizisi, örn:
// [{"id":"ch1","name":"Kanal 1","group":"Spor","url":"http://...m3u8"}]
// ------------------------------------------------------------
function loadChannels(): any[] {
  const raw = Deno.env.get("IPTV_CHANNELS");
  if (!raw) {
    return [
      { id: "test", name: "Test Kanalı", group: "Test", url: "http://ornek.m3u8" }
    ];
  }
  try {
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) return arr;
  } catch (e) {
    console.error("IPTV_CHANNELS parse hatası:", e);
  }
  return [];
}

const CHANNELS = loadChannels();

// ------------------------------------------------------------
// Cache'ler — tek bağlantı prensibinin kalbi
// Aynı manifest/segment kaç izleyici isterse istesin,
// yayıncıya giden fetch TEK olur.
// ------------------------------------------------------------
const manifestCache: Record<string, { body: string; expiresAt: number }> = {};
const segmentCache: Record<string, { body: Uint8Array; contentType: string; expiresAt: number }> = {};
const pendingManifest: Record<string, Promise<string> | null> = {};
const pendingSegment: Record<string, Promise<Uint8Array> | null> = {};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------
// TLS Kapanış Hatası İçin Retry Mekanizması
// Deno bazen "error reading a body from connection" hatası verir.
// Bu fonksiyon, hatayı alınca isteği birkaç kez tekrar dener.
// ------------------------------------------------------------
async function fetchWithRetry(url: string, options?: RequestInit, maxRetries = 3): Promise<Response> {
  let lastError: Error | null = null;
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fetch(url, options);
    } catch (e) {
      lastError = e as Error;
      console.warn(`Fetch denemesi ${i + 1}/${maxRetries} başarısız (${url}): ${lastError.message}`);
      // Kısa bir bekleme sonrası tekrar dene
      await sleep(200 * (i + 1));
    }
  }
  throw lastError;
}

// ------------------------------------------------------------
// Auth
// ------------------------------------------------------------
function timingSafeEqual(a: string, b: string): boolean {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function getCookie(req: Request, name: string): string | null {
  const cookie = req.headers.get("Cookie") || "";
  const match = cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return match ? match[1] : null;
}

async function hmacSign(payload: string, secret: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payload));
  return btoa(String.fromCharCode(...new Uint8Array(sig)));
}

async function createSession(secret: string): Promise<string> {
  const payload = btoa(JSON.stringify({ exp: Date.now() + SESSION_TTL_MS }));
  const sig = await hmacSign(payload, secret);
  return payload + "." + sig;
}

async function verifySession(cookie: string | null, secret: string): Promise<boolean> {
  if (!cookie || !cookie.includes(".")) return false;
  const [payload, sig] = cookie.split(".");
  try {
    const expected = await hmacSign(payload, secret);
    if (!timingSafeEqual(sig, expected)) return false;
    const data = JSON.parse(atob(payload));
    return typeof data.exp === "number" && data.exp > Date.now();
  } catch { return false; }
}

async function requireAuth(req: Request): Promise<boolean> {
  const pass = Deno.env.get("ADMIN_PASSWORD");
  if (!pass) return false;
  const cookie = getCookie(req, SESSION_COOKIE);
  return await verifySession(cookie, pass);
}

// ------------------------------------------------------------
// Yardımcılar
// ------------------------------------------------------------
function isPlaylist(url: string): boolean {
  try {
    const u = new URL(url);
    return u.pathname.endsWith(".m3u8") || u.pathname.endsWith(".m3u");
  } catch { return false; }
}

function jsonResp(obj: any, status = 200, extra?: Record<string, string>): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...(extra || {}) },
  });
}

const CHANNEL_URLS = new Map<string, string>();
for (const ch of CHANNELS) CHANNEL_URLS.set(ch.id, ch.url);

// ------------------------------------------------------------
// Manifest fetch + cache — YAYINCIYA TEK İSTEK
// ------------------------------------------------------------
async function fetchManifest(url: string, cacheKey: string): Promise<string> {
  const cached = manifestCache[cacheKey];
  if (cached && cached.expiresAt > Date.now()) return cached.body;

  if (pendingManifest[cacheKey]) return await pendingManifest[cacheKey]!;

  pendingManifest[cacheKey] = (async () => {
    // fetchWithRetry kullanılıyor
    const resp = await fetchWithRetry(url, { headers: { "User-Agent": UA, "Accept": "*/*" } });
    if (!resp.ok) throw new Error("Manifest upstream " + resp.status);
    const body = await resp.text();
    manifestCache[cacheKey] = { body, expiresAt: Date.now() + MANIFEST_CACHE_MS };
    return body;
  })();

  try {
    return await pendingManifest[cacheKey]!;
  } finally {
    pendingManifest[cacheKey] = null;
  }
}

// ------------------------------------------------------------
// Segment fetch + cache — YAYINCIYA TEK İSTEK
// Aynı segmenti 100 TiviMate istese bile Deno bir kez indirir.
// ------------------------------------------------------------
async function fetchSegmentBinary(url: string): Promise<{ body: Uint8Array; contentType: string }> {
  const cached = segmentCache[url];
  if (cached && cached.expiresAt > Date.now()) {
    return { body: cached.body, contentType: cached.contentType };
  }

  if (pendingSegment[url]) {
    const body = await pendingSegment[url]!;
    const ct = segmentCache[url]?.contentType || "video/mp2t";
    return { body, contentType: ct };
  }

  pendingSegment[url] = (async () => {
    // fetchWithRetry kullanılıyor
    const resp = await fetchWithRetry(url, { headers: { "User-Agent": UA, "Accept": "*/*" } });
    if (!resp.ok) throw new Error("Segment upstream " + resp.status);
    const ct = resp.headers.get("Content-Type") || "video/mp2t";
    const body = new Uint8Array(await resp.arrayBuffer());
    segmentCache[url] = { body, contentType: ct, expiresAt: Date.now() + SEGMENT_CACHE_MS };
    return body;
  })();

  try {
    const body = await pendingSegment[url]!;
    const ct = segmentCache[url]?.contentType || "video/mp2t";
    return { body, contentType: ct };
  } finally {
    pendingSegment[url] = null;
  }
}

// ------------------------------------------------------------
// Manifest proxy — iç URL'leri kendi proxy'mize çevir
// ------------------------------------------------------------
async function proxyManifest(sourceUrl: string, channelId: string, baseOrigin: string): Promise<Response> {
  const text = await fetchManifest(sourceUrl, channelId);
  const baseUrl = new URL(sourceUrl);
  const out: string[] = [];
  let hasStart = false, hasIndependent = false;

  for (const line of text.split("\n")) {
    const tr = line.trim();
    if (!tr) { out.push(line); continue; }
    if (tr.startsWith("#EXT-X-START")) hasStart = true;
    if (tr.startsWith("#EXT-X-INDEPENDENT-SEGMENTS")) hasIndependent = true;

    if (tr.startsWith("#") && tr.includes('URI="')) {
      out.push(tr.replace(/URI="([^"]+)"/g, (_: string, u: string) => {
        try {
          const abs = new URL(u, baseUrl).href;
          return 'URI="' + baseOrigin + "/seg?c=" + channelId + "&u=" + encodeURIComponent(abs) + '"';
        } catch { return 'URI="' + u + '"'; }
      }));
      continue;
    }
    if (tr.startsWith("#")) { out.push(line); continue; }

    try {
      const abs = new URL(tr, baseUrl).href;
      out.push(baseOrigin + "/seg?c=" + channelId + "&u=" + encodeURIComponent(abs));
    } catch { out.push(line); }
  }

  // Canlı yayın ipuçları
  const extras: string[] = [];
  if (!hasIndependent) extras.push("#EXT-X-INDEPENDENT-SEGMENTS");
  if (!hasStart) extras.push("#EXT-X-START:TIME-OFFSET=-6.0,PRECISE=YES");
  if (extras.length) {
    const idx = out.findIndex((l) => l.trim().startsWith("#EXTM3U"));
    out.splice(idx >= 0 ? idx + 1 : 0, 0, ...extras);
  }

  return new Response(out.join("\n"), {
    headers: {
      "Content-Type": "application/vnd.apple.mpegurl",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-cache",
    },
  });
}

// ------------------------------------------------------------
// Admin: HTML
// ------------------------------------------------------------
const ADMIN_HTML = `<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IPTV Proxy — Yönetim</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:20px;background:#0f172a;color:#f8fafc;min-height:100vh}
.hidden{display:none!important}
#login-view{max-width:400px;margin:80px auto;background:#1e293b;padding:40px;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.5)}
h1{font-size:22px;margin:0 0 24px}
h2{color:#38bdf8;margin:0 0 20px;font-size:20px}
input[type=password],input[type=text]{width:100%;padding:12px;margin-bottom:16px;background:#0f172a;border:1px solid #475569;border-radius:8px;color:#f8fafc;font-size:14px}
input:focus{outline:none;border-color:#38bdf8}
button{background:#3b82f6;color:#fff;border:none;padding:12px 20px;border-radius:8px;cursor:pointer;font-weight:600;font-size:14px}
button:hover{background:#2563eb}
button.danger{background:#dc2626}
button.danger:hover{background:#b91c1c}
button.secondary{background:#475569}
button.secondary:hover{background:#64748b}
.card{background:#1e293b;padding:20px;border-radius:12px;margin-bottom:16px;box-shadow:0 4px 6px rgba(0,0,0,.3)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.stat{background:#334155;padding:12px;border-radius:8px;text-align:center}
.stat-val{font-size:20px;font-weight:bold;color:#38bdf8}
.stat-lbl{font-size:11px;color:#94a3b8;margin-top:4px}
.ch-name{font-weight:bold;color:#f8fafc;font-size:15px;margin-bottom:4px}
.ch-url{font-family:monospace;font-size:11px;color:#94a3b8;word-break:break-all;background:#0f172a;padding:8px;border-radius:6px;margin-bottom:10px}
.msg{position:fixed;top:20px;right:20px;padding:14px 24px;border-radius:8px;font-weight:500;z-index:100;max-width:500px;box-shadow:0 10px 30px rgba(0,0,0,.5)}
.msg.ok{background:#059669}
.msg.err{background:#dc2626}
@media(max-width:768px){body{padding:12px}.grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<div id="login-view">
  <h1>🔒 Yönetim Girişi</h1>
  <form id="login-form">
    <input type="password" id="password-input" placeholder="Şifre" required autofocus>
    <button type="submit" style="width:100%">Giriş Yap</button>
  </form>
</div>

<div id="admin-view" class="hidden">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
    <h2>📺 IPTV Proxy — Kanallar</h2>
    <button id="logout-btn" class="secondary">Çıkış</button>
  </div>

  <div class="card">
    <div class="grid" style="grid-template-columns:repeat(3,1fr)">
      <div class="stat"><div class="stat-val" id="stat-total">0</div><div class="stat-lbl">Toplam Kanal</div></div>
      <div class="stat"><div class="stat-val" id="stat-cache-m">0</div><div class="stat-lbl">Manifest Cache</div></div>
      <div class="stat"><div class="stat-val" id="stat-cache-s">0</div><div class="stat-lbl">Segment Cache</div></div>
    </div>
  </div>

  <div id="channels" class="grid"></div>
</div>

<script>
var el = function(id){return document.getElementById(id)};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function msg(text, type){
  type=type||'ok';
  var d=document.createElement('div');
  d.className='msg '+type;
  d.textContent=text;
  document.body.appendChild(d);
  setTimeout(function(){d.remove()}, 4000);
}
function showLogin(){el('login-view').classList.remove('hidden');el('admin-view').classList.add('hidden')}
function showAdmin(){el('login-view').classList.add('hidden');el('admin-view').classList.remove('hidden')}

el('login-form').addEventListener('submit', async function(e){
  e.preventDefault();
  var pw = el('password-input').value;
  var r = await fetch('/api/admin/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  if(r.ok){el('password-input').value='';await loadChannels();msg('Hoş geldiniz!')}
  else{var j=await r.json().catch(function(){return{}});msg(j.error||'Yanlış şifre','err')}
});

el('logout-btn').addEventListener('click', async function(){
  await fetch('/api/admin/logout',{method:'POST'});
  showLogin();
});

async function loadChannels(){
  var r = await fetch('/api/admin/channels');
  if(r.status===401){showLogin();return}
  if(!r.ok){msg('Kanallar yüklenemedi','err');return}
  var channels = await r.json();
  renderChannels(channels);
  var stats = await fetch('/api/admin/stats').then(function(r){return r.json()}).catch(function(){return{}});
  el('stat-total').textContent = channels.length;
  el('stat-cache-m').textContent = stats.manifestCache || 0;
  el('stat-cache-s').textContent = stats.segmentCache || 0;
  showAdmin();
}

function renderChannels(channels){
  var c = el('channels');
  c.innerHTML = '';
  channels.forEach(function(ch){
    var div = document.createElement('div');
    div.className = 'card';
    div.innerHTML =
      '<div class="ch-name">' + esc(ch.name) + '</div>' +
      '<div style="font-size:11px;color:#94a3b8;margin-bottom:8px">ID: ' + esc(ch.id) + ' · Grup: ' + esc(ch.group||'-') + '</div>' +
      '<div class="ch-url">' + esc(ch.url) + '</div>' +
      '<div style="display:flex;gap:8px">' +
        '<button class="secondary" style="flex:1;padding:8px" onclick="copyUrl(\'' + esc(ch.url) + '\')">URL Kopyala</button>' +
        '<button class="danger" style="padding:8px 12px" onclick="deleteChannel(\'' + esc(ch.id) + '\')">Sil</button>' +
      '</div>';
    c.appendChild(div);
  });
  if(channels.length === 0){
    c.innerHTML = '<div class="card" style="text-align:center;color:#6b7280">Henüz kanal yok. Environment variable ile ekleyin: IPTV_CHANNELS</div>';
  }
}

function copyUrl(url){
  navigator.clipboard.writeText(url).then(function(){msg('URL kopyalandı!')});
}

async function deleteChannel(id){
  if(!confirm('"' + id + '" silinsin mi? (Sadece bu oturum için, kalıcı değil)')) return;
  msg('Silme kalıcı değildir. Environment variable\'dan kaldırın.', 'err');
}

loadChannels();
</script>
</body>
</html>`;

// ------------------------------------------------------------
// Admin handlers
// ------------------------------------------------------------
async function handleLogin(req: Request): Promise<Response> {
  const pass = Deno.env.get("ADMIN_PASSWORD");
  if (!pass) {
    return jsonResp({
      error: "ADMIN_PASSWORD ayarlanmamış",
      detail: "Deno Deploy panelinde Environment Variables bölümüne ADMIN_PASSWORD ekleyin (Production context)."
    }, 500);
  }
  let body: any;
  try { body = await req.json(); } catch { body = {}; }
  const password = body.password || "";
  if (!timingSafeEqual(password, pass)) return jsonResp({ error: "Yanlış şifre" }, 401);

  const session = await createSession(pass);
  const maxAge = Math.floor(SESSION_TTL_MS / 1000);
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Set-Cookie": SESSION_COOKIE + "=" + session + "; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=" + maxAge,
    },
  });
}

function handleLogout(): Response {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Set-Cookie": SESSION_COOKIE + "=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0",
    },
  });
}

// ------------------------------------------------------------
// Ana handler
// ------------------------------------------------------------
async function handler(req: Request): Promise<Response> {
  const url = new URL(req.url);
  const path = url.pathname;
  const baseOrigin = url.origin;

  // Debug
  if (path === "/api/admin/debug") {
    return jsonResp({
      ADMIN_PASSWORD_set: !!Deno.env.get("ADMIN_PASSWORD"),
      IPTV_CHANNELS_set: !!Deno.env.get("IPTV_CHANNELS"),
      channelCount: CHANNELS.length,
      manifestCacheEntries: Object.keys(manifestCache).length,
      segmentCacheEntries: Object.keys(segmentCache).length,
    });
  }

  if (path === "/api/admin/stats") {
    return jsonResp({
      manifestCache: Object.keys(manifestCache).length,
      segmentCache: Object.keys(segmentCache).length,
    });
  }

  if (path === "/admin" || path === "/admin/") {
    return new Response(ADMIN_HTML, {
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  if (path === "/api/admin/login" && req.method === "POST") return await handleLogin(req);
  if (path === "/api/admin/logout") return handleLogout();

  if (path === "/api/admin/channels") {
    const ok = await requireAuth(req);
    if (!ok) return jsonResp({ error: "Unauthorized" }, 401);
    if (req.method === "GET") {
      return jsonResp(CHANNELS.map((ch: any) => ({
        id: ch.id, name: ch.name, group: ch.group || "", url: ch.url
      })));
    }
    return new Response("Method not allowed", { status: 405 });
  }

  // Playlist
  if (path === "/playlist.m3u" || path === "/playlist") {
    const lines = ["#EXTM3U"];
    for (const ch of CHANNELS) {
      lines.push(`#EXTINF:-1 tvg-id="${ch.id}" tvg-name="${ch.name}" group-title="${ch.group || "Genel"}",${ch.name}`);
      lines.push(`${baseOrigin}/live/${ch.id}.m3u8`);
    }
    return new Response(lines.join("\n"), {
      headers: { "Content-Type": "audio/x-mpegurl; charset=utf-8", "Cache-Control": "no-cache" },
    });
  }

  // Live manifest
  const liveMatch = path.match(/^\/live\/([A-Za-z0-9_\-]+)\.m3u8$/);
  if (liveMatch) {
    const channelId = liveMatch[1];
    const sourceUrl = CHANNEL_URLS.get(channelId);
    if (!sourceUrl) return new Response("Kanal yok: " + channelId, { status: 404 });
    try {
      return await proxyManifest(sourceUrl, channelId, baseOrigin);
    } catch (e: any) {
      return jsonResp({ error: e.message }, 502);
    }
  }

  // Segment
  if (path === "/seg") {
    const c = url.searchParams.get("c");
    const u = url.searchParams.get("u");
    if (!c || !u) return new Response("Missing params", { status: 400 });

    // İç içe manifest ise manifest olarak işle
    if (isPlaylist(u)) {
      try {
        return await proxyManifest(u, c, baseOrigin);
      } catch (e: any) {
        return jsonResp({ error: "Nested manifest: " + e.message }, 502);
      }
    }

    try {
      const { body, contentType } = await fetchSegmentBinary(u);
      return new Response(body, {
        headers: {
          "Content-Type": contentType,
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=30",
        },
      });
    } catch (e: any) {
      return new Response("Segment error: " + e.message, { status: 502 });
    }
  }

  if (path === "/status") {
    const result: any = {};
    for (const ch of CHANNELS) {
      result[ch.id] = {
        name: ch.name,
        urlHidden: true,
        manifestCached: !!manifestCache[ch.id],
      };
    }
    return jsonResp(result);
  }

  return new Response(
    "IPTV Proxy (Deno) — Tek Bağlantı Prensibi\n\n" +
    "Public:\n- /playlist.m3u\n- /live/<id>.m3u8\n- /seg?c=<id>&u=<url>\n- /status\n\n" +
    "Admin:\n- /admin\n- /api/admin/debug\n",
    { status: 404, headers: { "Content-Type": "text/plain" } }
  );
}

Deno.serve(handler);
