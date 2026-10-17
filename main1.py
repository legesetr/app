#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg tabanlı HLS re-stream proxy (Canlı İzleyici Sayacı & Kalıcı Aylık Kota Takibi).
Geliştirmeler: Kalıcı Toplam Uptime Takibi, Kanal Zamanlayıcı, Standby Mesajı & Ortam Değişkeni Güvenliği.
"""

import os
import re
import json
import time
import shutil
import asyncio
import subprocess
import logging
import datetime
from pathlib import Path
from aiohttp import web

# ==================== AYARLAR & GÜVENLİK ====================
BIND_HOST    = "0.0.0.0"
PROXY_PORT   = int(os.environ.get("PORT", 8080))
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "admin123")  # Render Environment'tan okunur

BASE_DIR = Path(__file__).resolve().parent
LOCAL_M3U_PATH  = os.environ.get("LOCAL_M3U_PATH", str(BASE_DIR / "playlist.m3u"))
LOCAL_JSON_PATH = os.environ.get("LOCAL_JSON_PATH", str(BASE_DIR / "channels.json"))
LOG_DIR         = os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))
USAGE_FILE      = os.environ.get("USAGE_FILE", str(BASE_DIR / "bandwidth_usage.json"))
STATE_FILE      = os.environ.get("STATE_FILE", str(BASE_DIR / "server_state.json"))

HLS_BASE_DIR = "/tmp/iptv_hls"
STANDBY_TS_PATH = os.path.join(HLS_BASE_DIR, "standby.ts")

HLS_TIME       = 4
HLS_LIST_SIZE  = 12
IDLE_TIMEOUT   = 100
STARTUP_WAIT   = 60
FFMPEG_BIN     = "ffmpeg"
APP_START_TIME = time.time()


# ==================== KALICI AYLIK KOTA TAKİPÇİSİ ====================
class BandwidthTracker:
    """Kotayı diske kaydeder ve her ay başında otomatik olarak sıfırlar."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.current_month = time.strftime("%Y-%m")
        self.bytes_used = 0
        self.dirty = False
        self.load()

    def load(self):
        now_month = time.strftime("%Y-%m")
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("month") == now_month:
                        self.bytes_used = data.get("bytes", 0)
                        self.current_month = now_month
                    else:
                        self.bytes_used = 0
                        self.current_month = now_month
                        self.save()
            except Exception:
                self.bytes_used = 0
        else:
            self.bytes_used = 0
            self.current_month = now_month
            self.save()

    def add_bytes(self, n: int):
        now_month = time.strftime("%Y-%m")
        if now_month != self.current_month:
            self.current_month = now_month
            self.bytes_used = 0

        self.bytes_used += n
        self.dirty = True

    def save(self):
        try:
            data = {
                "month": self.current_month,
                "bytes": self.bytes_used,
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.dirty = False
        except Exception as e:
            log.warning(f"Kota dosyaya kaydedilemedi: {e}")

    async def periodic_save(self):
        """Performans için her 10 saniyede bir diske yazar."""
        while True:
            await asyncio.sleep(10)
            if self.dirty:
                self.save()

tracker = BandwidthTracker(USAGE_FILE)


# ==================== SUNUCU DURUMU & UPTIME KONTROLÜ ====================
class ServerState:
    """Sunucu açık kalma süresi ve özelleştirilmiş standby mesajlarını saklar."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.total_uptime_seconds = 0.0
        self.standby_message = "YAYIN SU ANDA KAPALIDIR\n\nMac Saatinde Acilacaktir"
        self.last_save_time = time.time()
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.total_uptime_seconds = data.get("total_uptime_seconds", 0.0)
                    self.standby_message = data.get("standby_message", "YAYIN SU ANDA KAPALIDIR\n\nMac Saatinde Acilacaktir")
            except Exception:
                pass

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "total_uptime_seconds": self.total_uptime_seconds,
                    "standby_message": self.standby_message
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"Sunucu durumu kaydedilemedi: {e}")

server_state = ServerState(STATE_FILE)

async def uptime_tracker_task():
    """Periyodik olarak sunucunun toplam çalışma süresini günceller ve kaydeder."""
    while True:
        await asyncio.sleep(10)
        now = time.time()
        elapsed = now - server_state.last_save_time
        server_state.total_uptime_seconds += elapsed
        server_state.last_save_time = now
        server_state.save()

def format_uptime(seconds: float) -> str:
    """Saniyeyi gün, saat ve dakikaya dönüştürür."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts = []
    if days > 0:
        parts.append(f"{days} Gün")
    if hours > 0 or days > 0:
        parts.append(f"{hours} Saat")
    parts.append(f"{minutes} Dk")
    return " ".join(parts) if parts else "0 Dk"


# ==================== KANALLAR (GİZLİ ORTAM DEĞİŞKENLİ) ====================
# Linkler Render.com Environment'tan çekilir, GitHub'da görünmez!
DEFAULT_KANALLAR = [
    {
        "id": "futbol_tv",
        "name": "FUTBOL TV",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": os.environ.get("URL_FUTBOL_TV", "http://varsayilan-yayin-adresi.m3u8")
    }
]

def load_dynamic_channels():
    """Kanalları JSON dosyasından yükler, yoksa ortam değişkenlerinden varsayılanları oluşturur."""
    if os.path.exists(LOCAL_JSON_PATH):
        try:
            with open(LOCAL_JSON_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                # Eksik kanal varsa ortam değişkenleriyle tamamla
                saved_ids = {c["id"] for c in saved}
                for def_ch in DEFAULT_KANALLAR:
                    if def_ch["id"] not in saved_ids:
                        saved.append(def_ch)
                return saved
        except Exception as e:
            log.warning(f"Kanallar JSON dosyasından yüklenemedi: {e}")
    try:
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_KANALLAR, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Varsayılan kanallar yazılamadı: {e}")
    return DEFAULT_KANALLAR

KANALLAR = load_dynamic_channels()
DELETED_CHANNELS = ["bein_sports_1_6781", "BEİN SPORTS 1 (6781)", "bein sports 1 (6781)"]
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}
NO_CACHE_HEADERS = {
    **CORS_HEADERS,
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0"
}

# ==================== LOG ====================
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "proxy.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("iptv")


def get_client_ip(request):
    """Kullanıcının gerçek IP adresini tespit eder (Cloudflare/Render Proxy Uyumlu)"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    return request.remote or "unknown"


def get_memory_usage_mb():
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(usage / 1024, 2)
    except Exception:
        return 0.0


# ==================== STANDBY EKRANI ====================
def generate_standby_clip(force=False):
    if not force and os.path.exists(STANDBY_TS_PATH) and os.path.getsize(STANDBY_TS_PATH) > 0:
        return
    os.makedirs(HLS_BASE_DIR, exist_ok=True)

    text_file_path = os.path.join(HLS_BASE_DIR, "standby_text.txt")
    try:
        with open(text_file_path, "w", encoding="utf-8") as f:
            f.write(server_state.standby_message)
    except Exception as e:
        log.warning(f"Standby metin dosyası yazılamadı: {e}")

    text_file_path_ff = text_file_path.replace('\\', '/').replace(':', '\\:')

    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={HLS_TIME}:r=25",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(HLS_TIME),
        "-vf", f"drawtext=textfile='{text_file_path_ff}':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-b:v", "35k",
        "-c:a", "aac", "-b:a", "16k",
        "-f", "mpegts", STANDBY_TS_PATH
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        log.info("Standby klibi başarıyla oluşturuldu/güncellendi.")
    except Exception as e:
        log.warning(f"Standby klibi oluşturulamadı: {e}")


# ==================== OTO-ZAMANLAYICI KONTROLÜ ====================
def _slot_contains(now_minutes: int, start_str: str, end_str: str) -> bool:
    """Belirtilen slot içinde olup olmadığını kontrol eder (gece aşımı destekli)."""
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes == end_minutes:
            return False
        if start_minutes <= end_minutes:
            return start_minutes <= now_minutes < end_minutes
        else:
            return now_minutes >= start_minutes or now_minutes < end_minutes
    except Exception:
        return False

def check_schedule(st) -> bool | None:
    if not st.ch.get("sched_enabled", False):
        return None

    tr_tz = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(tr_tz)
    day_of_week = now.weekday()
    sched_days = st.ch.get("sched_days", [])
    try:
        sched_days = [int(d) for d in sched_days]
    except Exception:
        sched_days = []

    if day_of_week not in sched_days:
        return False

    now_minutes = now.hour * 60 + now.minute

    # Yeni çoklu slot sistemi
    slots = st.ch.get("sched_slots", [])
    if slots:
        for slot in slots:
            try:
                start_str = slot.get("start", "00:00")
                end_str = slot.get("end", "00:00")
                if _slot_contains(now_minutes, start_str, end_str):
                    return True
            except Exception:
                continue
        return False

    # Geriye dönük uyumluluk: Eski tek slot sistemi
    start_str = st.ch.get("sched_start", "00:00")
    end_str = st.ch.get("sched_end", "00:00")
    return _slot_contains(now_minutes, start_str, end_str)


# ==================== FFMPEG YÖNETİCİSİ ====================
class ChannelStream:
    def __init__(self, channel: dict):
        self.ch      = channel
        self.id      = channel["id"]
        self.src     = channel["url"]
        self.dir     = os.path.join(HLS_BASE_DIR, self.id)
        self.proc: subprocess.Popen | None = None
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.enabled = channel.get("enabled", True)
        self.viewers = {}
        self.last_sched_state = None  # Geçiş durum takipçisi

    def record_viewer(self, ip: str):
        if ip and ip != "unknown":
            self.viewers[ip] = time.time()
            self.touch()

    def get_viewer_count(self) -> int:
        now = time.time()
        active = [ip for ip, last_seen in self.viewers.items() if (now - last_seen) <= 12]
        self.viewers = {ip: last_seen for ip, last_seen in self.viewers.items() if (now - last_seen) <= 60}
        return len(active)

    def _prepare_dir(self):
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)

    def _build_cmd(self):
        m3u8_path = os.path.join(self.dir, "index.m3u8")
        seg_pattern = os.path.join(self.dir, "seg_%05d.ts")

        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-user_agent", "VLC/3.0.18 LibVLC/3.0.18",
            "-i", self.src, "-c", "copy", "-f", "hls",
            "-hls_time", str(HLS_TIME), "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments+temp_file",
            "-hls_segment_type", "mpegts", "-hls_segment_filename", seg_pattern,
            "-hls_allow_cache", "1", m3u8_path
        ]
        return cmd

    async def start(self):
        if not self.enabled:
            return
        async with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self._prepare_dir()
            cmd = self._build_cmd()
            ff_log = open(os.path.join(LOG_DIR, f"{self.id}.ffmpeg.log"), "ab")
            self.proc = subprocess.Popen(cmd, stdout=ff_log, stderr=ff_log, stdin=subprocess.DEVNULL, start_new_session=True)
            self.started_at = time.time()

    async def stop(self):
        async with self.lock:
            if not self.proc:
                return
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def playlist_path(self):
        return os.path.join(self.dir, "index.m3u8")

    def playlist_ready(self) -> bool:
        p = self.playlist_path()
        if not os.path.exists(p):
            return False
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            return content.count("#EXTINF") >= 2
        except Exception:
            return False

    def touch(self):
        self.last_request = time.time()


# ==================== YÖNETİCİ ====================
class StreamManager:
    def __init__(self):
        self.streams: dict[str, ChannelStream] = {ch["id"]: ChannelStream(ch) for ch in KANALLAR}

    def get(self, cid: str) -> ChannelStream | None:
        return self.streams.get(cid)

    def total_viewers(self) -> int:
        return sum(st.get_viewer_count() for st in self.streams.values())

    async def ensure_running(self, cid: str) -> ChannelStream | None:
        st = self.streams.get(cid)
        if not st or not st.enabled:
            return None
        if not st.is_alive():
            await st.start()
        waited = 0.0
        while waited < STARTUP_WAIT:
            if st.playlist_ready():
                return st
            await asyncio.sleep(0.5)
            waited += 0.5
            if not st.is_alive() and st.enabled:
                await asyncio.sleep(0.5)
                await st.start()
        return st

    async def update_channel_url(self, cid: str, new_url: str) -> bool:
        st = self.streams.get(cid)
        if not st:
            return False
        
        was_running = st.is_alive()
        if was_running:
            await st.stop()
        
        st.src = new_url
        st.ch["url"] = new_url
        
        try:
            channels_data = [s.ch for s in self.streams.values()]
            with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(channels_data, f, indent=2, ensure_ascii=False)
            log.info(f"Kanal URL güncellendi: {cid} -> {new_url}")
        except Exception as e:
            log.error(f"Kanallar JSON dosyasına yazılamadı: {e}")

        if was_running and st.enabled:
            await st.start()
        
        return True

    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for cid, st in self.streams.items():
                sched_state = check_schedule(st)
                
                # Sadece takvim durum geçişlerinde (Açılış/Kapanış saatinde) müdahale eder.
                if sched_state is not None:
                    if st.last_sched_state is not None:
                        if sched_state != st.last_sched_state:
                            st.enabled = sched_state
                            st.ch["enabled"] = sched_state
                            if sched_state:
                                st.touch()
                                await st.start()
                                log.info(f"[Zamanlayıcı] {cid} yayını takvim geçişiyle otomatik olarak BAŞLATILDI.")
                            else:
                                await st.stop()
                                log.info(f"[Zamanlayıcı] {cid} yayını takvim geçişiyle otomatik olarak DURDURULDU.")
                            
                            # Durum değişimini diske kaydet
                            try:
                                channels_data = [s.ch for s in self.streams.values()]
                                with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
                                    json.dump(channels_data, f, indent=2, ensure_ascii=False)
                            except Exception:
                                pass
                    else:
                        # İlk başlama anında takvimi eşitle
                        st.enabled = sched_state
                        st.ch["enabled"] = sched_state
                        if sched_state and not st.is_alive():
                            await st.start()
                        elif not sched_state and st.is_alive():
                            await st.stop()
                    
                    st.last_sched_state = sched_state
                else:
                    st.last_sched_state = None

                # Genel boşta kalma ve durum kontrolleri
                if not st.enabled and st.is_alive():
                    await st.stop()
                elif st.is_alive() and st.last_request and (now - st.last_request) > IDLE_TIMEOUT:
                    await st.stop()
                elif (not st.is_alive()) and st.enabled and st.last_request and (now - st.last_request) < IDLE_TIMEOUT:
                    await st.start()

manager = StreamManager()


# ==================== HTTP HANDLER'LAR ====================
async def handle_m3u8(request):
    cid = request.match_info.get("channel_id")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Yok", headers=CORS_HEADERS)

    client_ip = get_client_ip(request)
    st.record_viewer(client_ip)

    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    dynamic_proxy_url = f"{scheme}://{host}"

    if not st.enabled:
        seq = int(time.time() // HLS_TIME)
        standby_lines = [
            "#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{HLS_TIME}",
            f"#EXT-X-MEDIA-SEQUENCE:{seq}",
            f"#EXTINF:{HLS_TIME}.000,", f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq}",
            f"#EXTINF:{HLS_TIME}.000,", f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq + 1}",
            f"#EXTINF:{HLS_TIME}.000,", f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq + 2}",
        ]
        resp_text = "\n".join(standby_lines)
        tracker.add_bytes(len(resp_text.encode('utf-8')))
        return web.Response(text=resp_text, content_type="application/vnd.apple.mpegurl", headers={**CORS_HEADERS, "Cache-Control": "no-cache"})

    res = await manager.ensure_running(cid)
    if not res:
        return web.Response(status=503, text="Yayın başlatılamadı.", headers=CORS_HEADERS)

    pl = st.playlist_path()
    if not os.path.exists(pl):
        return web.Response(status=503, text="Yayın hazırlanıyor...", headers=CORS_HEADERS)

    try:
        with open(pl, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return web.Response(status=500, text=str(e), headers=CORS_HEADERS)

    out_lines = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out_lines.append(s)
        else:
            seg_name = s.split("?")[0].split("/")[-1]
            out_lines.append(f"{dynamic_proxy_url}/hls/{cid}/{seg_name}")

    resp_text = "\n".join(out_lines)
    tracker.add_bytes(len(resp_text.encode('utf-8')))
    return web.Response(text=resp_text, content_type="application/vnd.apple.mpegurl", headers={**CORS_HEADERS, "Cache-Control": "no-cache"})


async def handle_segment(request):
    cid = request.match_info.get("channel_id")
    name = request.match_info.get("name")

    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+\.(ts|m4s|mp4|aac|key)", name):
        return web.Response(status=400, headers=CORS_HEADERS)

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, headers=CORS_HEADERS)

    client_ip = get_client_ip(request)
    st.record_viewer(client_ip)

    seg_path = os.path.join(st.dir, name)
    if not os.path.exists(seg_path):
        await asyncio.sleep(0.3)
        if not os.path.exists(seg_path):
            return web.Response(status=404, headers=CORS_HEADERS)

    try:
        file_size = os.path.getsize(seg_path)
        tracker.add_bytes(file_size)
        return web.FileResponse(seg_path, headers={**CORS_HEADERS, "Cache-Control": "public, max-age=6", "Content-Type": "video/mp2t"})
    except Exception as e:
        return web.Response(status=500, text=str(e), headers=CORS_HEADERS)


async def handle_standby_segment(request):
    if not os.path.exists(STANDBY_TS_PATH):
        generate_standby_clip()
    if not os.path.exists(STANDBY_TS_PATH):
        return web.Response(status=404, headers=CORS_HEADERS)

    file_size = os.path.getsize(STANDBY_TS_PATH)
    tracker.add_bytes(file_size)
    return web.FileResponse(STANDBY_TS_PATH, headers={**CORS_HEADERS, "Cache-Control": "public, max-age=4", "Content-Type": "video/mp2t"})


async def handle_health(request):
    key = request.query.get("key")
    is_admin = (key == ADMIN_KEY)
    status = {
        "server": {
            "uptime_seconds": int(time.time() - APP_START_TIME),
            "uptime_session_str": format_uptime(time.time() - APP_START_TIME),
            "uptime_total_str": format_uptime(server_state.total_uptime_seconds),
            "ram_usage_mb": get_memory_usage_mb(),
            "total_served_mb": round(tracker.bytes_used / (1024 * 1024), 2),
            "total_served_gb": round(tracker.bytes_used / (1024 * 1024 * 1024), 3),
            "total_viewers": manager.total_viewers(),
            "standby_message": server_state.standby_message
        },
        "channels": {}
    }
    for cid, st in manager.streams.items():
        slots = st.ch.get("sched_slots", [])
        if not slots:
            slots = [{
                "start": st.ch.get("sched_start", "00:00"),
                "end": st.ch.get("sched_end", "00:00")
            }]

        status["channels"][cid] = {
            "name": st.ch.get("name", cid), 
            "url": st.src if is_admin else "YAYIN LINKI GIZLENMISTIR",
            "enabled": st.enabled,
            "running": st.is_alive(),
            "ready": st.playlist_ready(),
            "viewers": st.get_viewer_count(),
            "sched_enabled": st.ch.get("sched_enabled", False),
            "sched_days": st.ch.get("sched_days", []),
            "sched_slots": slots,
            "sched_start": st.ch.get("sched_start", "00:00"),
            "sched_end": st.ch.get("sched_end", "00:00")
        }
    return web.json_response(status, headers=NO_CACHE_HEADERS)


# ==================== YÖNETİCİ PANELİ ====================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IPTV Kontrol & Canlı İzleyici Paneli</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 650px; margin: auto; }
        .card { background: #1e293b; padding: 15px; border-radius: 12px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .stat-box { background: #334155; padding: 12px; border-radius: 8px; text-align: center; }
        .stat-val { font-size: 18px; font-weight: bold; color: #38bdf8; }
        .stat-lbl { font-size: 11px; color: #94a3b8; margin-top: 4px; }
        h2 { color: #38bdf8; margin-top: 0; }
        h3 { margin: 0 0 5px 0; color: #f8fafc; }
        .btn { padding: 10px 18px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; transition: 0.2s; }
        .btn-on { background: #22c55e; }
        .btn-off { background: #ef4444; }
        .btn-logout { background: #475569; font-size: 11px; padding: 6px 12px; margin-top: 10px; }
        .status-badge { display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .badge-active { background: #15803d; }
        .badge-disabled { background: #b91c1c; }
        .badge-viewer { background: #0369a1; color: #e0f2fe; margin-left: 4px; }
        .badge-ffmpeg-on { background: #0284c7; color: white; }
        .badge-ffmpeg-off { background: #64748b; color: #cbd5e1; }
        input[type=password] { padding: 12px; border-radius: 8px; border: 1px solid #475569; background: #1e293b; color: white; width: 100%; box-sizing: border-box; margin-bottom: 12px; font-size: 16px; text-align: center; }
        #loginArea { max-width: 400px; margin: 100px auto; text-align: center; }
        .edit-group { margin-top: 12px; border-top: 1px solid #334155; padding-top: 10px; display: flex; gap: 8px; }
        .edit-input { flex: 1; padding: 8px 10px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #cbd5e1; font-size: 13px; }
        .btn-save { background: #3b82f6; font-size: 12px; padding: 6px 12px; }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #16a34a; color: white; padding: 12px 24px; border-radius: 8px; z-index: 9999; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
        
        .switch { position: relative; display: inline-block; width: 40px; height: 20px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #475569; transition: .3s; border-radius: 20px; }
        .slider:before { position: absolute; content: ""; height: 14px; width: 14px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; }
        input:checked + .slider { background-color: #22c55e; }
        input:checked + .slider:before { transform: translateX(20px); }
        
        .sched-box { margin-top: 15px; background: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #334155; }
        .days-container { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 6px; margin-bottom: 8px; }
        .days-container label { font-size: 11px; background: #334155; padding: 4px 6px; border-radius: 4px; cursor: pointer; display: flex; align-items: center; gap: 2px; }
        .days-container input { margin: 0; }
        
        .slot-row { display:flex; gap:6px; margin-bottom:6px; align-items:center; background:#1e293b; padding:6px; border-radius:6px; }
        .btn-remove-slot { background: #dc2626; padding: 6px 10px; font-size: 14px; border-radius: 6px; }
        .btn-add-slot { background: #8b5cf6; width: 100%; padding: 8px; margin-bottom: 10px; font-size: 12px; }
    </style>
</head>
<body>

    <div id="toastMsg" class="toast"></div>

    <!-- GİRİŞ EKRANI -->
    <div id="loginArea" class="card">
        <h2>🔒 Yönetici Girişi</h2>
        <p style="color: #94a3b8; font-size: 13px; margin-bottom: 15px;">Lütfen devam etmek için şifrenizi girin.</p>
        <input type="password" id="adminPassword" placeholder="Şifre" onkeypress="handleKeyPress(event)">
        <button class="btn btn-on" style="width: 100%;" onclick="attemptLogin()">Giriş Yap</button>
    </div>

    <!-- PANEL ALANI -->
    <div id="panelArea" style="display: none;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>📊 Sunucu & İzleyici Durumu</h2>
            <button class="btn btn-logout" onclick="logout()">Çıkış Yap</button>
        </div>
        
        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-val" id="totalViewers" style="color:#a855f7;">0 Kişi</div>
                <div class="stat-lbl">Canlı İzleyici</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="servedGb">0.00 GB</div>
                <div class="stat-lbl">Harcanan Kota (Aylık)</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="uptimeTotal" style="color:#22c55e;">0 Gün 0 Saat</div>
                <div class="stat-lbl">Toplam Sunucu Uptime</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="ramMb">0 MB</div>
                <div class="stat-lbl">RAM / Oturum Uptime</div>
            </div>
        </div>

        <!-- YAYIN KAPALI MESAJ AYARI -->
        <div class="card">
            <h2 style="font-size:16px;">⚙️ Yayın Kapalı Ekran Mesajı</h2>
            <p style="color:#94a3b8; font-size:11px; margin-top:-6px; margin-bottom:8px;">Yayın kapalıyken ekranda gösterilecek metni ayarlayın.</p>
            <div style="display:flex; flex-direction:column; gap:8px;">
                <textarea id="standbyMessageInput" rows="2" class="edit-input" style="width:100%; padding:8px; border-radius:6px; resize: none;" placeholder="Yayın Kapalı Ekran Mesajı"></textarea>
                <button class="btn btn-save" style="width:100%; padding:8px;" onclick="updateStandbyMessage()">Standby Ekran Mesajını Güncelle</button>
            </div>
        </div>

        <h2>📺 Yayın Kontrolü</h2>
        <div id="channels"></div>
    </div>

    <script>
        let updateInterval = null;
        let standbyLoaded = false;

        function showToast(msg, color) {
            const t = document.getElementById('toastMsg');
            t.innerText = msg;
            t.style.background = color || '#16a34a';
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 2500);
        }

        function getStoredKey() {
            return localStorage.getItem("admin_key") || "";
        }

        function handleKeyPress(e) {
            if (e.key === 'Enter') {
                attemptLogin();
            }
        }

        async function verifyKey(key) {
            try {
                const res = await fetch(`/admin/verify?key=${encodeURIComponent(key)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    const data = await res.json();
                    return data.valid;
                }
            } catch (e) {}
            return false;
        }

        async function attemptLogin() {
            const key = document.getElementById('adminPassword').value;
            const isValid = await verifyKey(key);
            if (isValid) {
                localStorage.setItem("admin_key", key);
                showPanel();
            } else {
                alert('Şifre Hatalı!');
            }
        }

        function logout() {
            localStorage.removeItem("admin_key");
            document.getElementById('adminPassword').value = "";
            document.getElementById('panelArea').style.display = "none";
            document.getElementById('loginArea').style.display = "block";
            if (updateInterval) clearInterval(updateInterval);
        }

        async function showPanel() {
            document.getElementById('loginArea').style.display = "none";
            document.getElementById('panelArea').style.display = "block";
            await loadStatus();
            if (updateInterval) clearInterval(updateInterval);
            updateInterval = setInterval(loadStatus, 3000);
        }

        function addSlotRow(id, start, end) {
            const container = document.getElementById(`sched_slots_container_${id}`);
            if (!container) return;
            const rowId = `slot_${id}_${Date.now()}_${Math.floor(Math.random()*10000)}`;
            const div = document.createElement('div');
            div.id = rowId;
            div.className = 'slot-row';
            div.innerHTML = `
                <div style="flex:1;">
                    <input type="time" class="edit-input sched-slot-start-${id}" value="${start || '00:00'}" style="width:100%;">
                </div>
                <span style="color:#94a3b8; font-weight:bold;">→</span>
                <div style="flex:1;">
                    <input type="time" class="edit-input sched-slot-end-${id}" value="${end || '00:00'}" style="width:100%;">
                </div>
                <button class="btn btn-remove-slot" onclick="document.getElementById('${rowId}').remove()">✕</button>
            `;
            container.appendChild(div);
        }

        async function loadStatus() {
            try {
                const key = getStoredKey();
                const res = await fetch(`/health?key=${encodeURIComponent(key)}&_=${Date.now()}`, { cache: 'no-store' });
                if (!res.ok) return;
                const data = await res.json();
                
                document.getElementById('totalViewers').innerText = data.server.total_viewers + " Kişi";
                document.getElementById('servedGb').innerText = data.server.total_served_gb + " GB";
                document.getElementById('uptimeTotal').innerText = data.server.uptime_total_str;
                document.getElementById('ramMb').innerText = data.server.ram_usage_mb + " MB / " + data.server.uptime_session_str;

                if (!standbyLoaded && data.server.standby_message) {
                    document.getElementById('standbyMessageInput').value = data.server.standby_message;
                    standbyLoaded = true;
                }

                const container = document.getElementById('channels');

                for (const [id, info] of Object.entries(data.channels)) {
                    let card = document.getElementById(`card_${id}`);
                    if (!card) {
                        card = document.createElement('div');
                        card.id = `card_${id}`;
                        card.className = 'card';
                        container.appendChild(card);
                    }

                    const inputId = `url_${id}`;

                    if (!card.querySelector('.edit-input')) {
                        card.innerHTML = `
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <div>
                                    <h3 class="channel-title">${info.name || id}</h3>
                                    <div style="display:flex; gap: 5px; align-items:center; flex-wrap: wrap; margin-bottom: 6px;">
                                        <span class="status-badge badge-state ${info.enabled ? 'badge-active' : 'badge-disabled'}">
                                            ${info.enabled ? 'YAYINDA' : 'KAPALI'}
                                        </span>
                                        <span class="status-badge badge-viewer badge-viewers-count">
                                            👥 ${info.viewers} İzleyici
                                        </span>
                                        <span class="status-badge badge-ffmpeg ${info.running ? 'badge-ffmpeg-on' : 'badge-ffmpeg-off'}">
                                            ${info.running ? '● FFmpeg Aktif' : '○ FFmpeg Kapalı'}
                                        </span>
                                    </div>
                                </div>
                                <button class="btn btn-toggle-action ${info.enabled ? 'btn-off' : 'btn-on'}" onclick="toggleChannel('${id}', ${!info.enabled})">
                                    ${info.enabled ? 'YAYINI KAPAT' : 'YAYINI AÇ'}
                                </button>
                            </div>
                            
                            <div class="edit-group">
                                <input type="text" id="${inputId}" class="edit-input" value="${info.url}" placeholder="Yayın (.m3u8) Linki">
                                <button class="btn btn-save" onclick="updateChannelUrl('${id}')">Kaydet</button>
                            </div>

                            <!-- OTO ZAMANLAYICI BÖLÜMÜ -->
                            <div class="sched-box">
                                <div style="display:flex; justify-content:space-between; align-items:center;">
                                    <span style="font-weight:bold; font-size:12px; color:#38bdf8;">⏰ Otomatik Zamanlayıcı (Aç / Kapat)</span>
                                    <label class="switch">
                                        <input type="checkbox" id="sched_enable_${id}" onchange="toggleScheduleUI('${id}')">
                                        <span class="slider"></span>
                                    </label>
                                </div>
                                <div id="sched_fields_${id}" style="display:none; flex-direction:column; margin-top:8px;">
                                    <span style="font-size:11px; color:#94a3b8;">Aktif Günler:</span>
                                    <div class="days-container">
                                        <label><input type="checkbox" class="sched-day-${id}" value="0"> Pzt</label>
                                        <label><input type="checkbox" class="sched-day-${id}" value="1"> Sal</label>
                                        <label><input type="checkbox" class="sched-day-${id}" value="2"> Çar</label>
                                        <label><input type="checkbox" class="sched-day-${id}" value="3"> Per</label>
                                        <label><input type="checkbox" class="sched-day-${id}" value="4"> Cum</label>
                                        <label><input type="checkbox" class="sched-day-${id}" value="5"> Cmt</label>
                                        <label><input type="checkbox" class="sched-day-${id}" value="6"> Paz</label>
                                    </div>
                                    
                                    <span style="font-size:11px; color:#94a3b8; display:block; margin-bottom:6px;">Zaman Aralıkları (Günde Birden Fazla Eklenebilir):</span>
                                    <div id="sched_slots_container_${id}"></div>
                                    <button class="btn btn-add-slot" onclick="addSlotRow('${id}')">+ Zaman Aralığı Ekle</button>
                                    
                                    <button class="btn btn-save" style="background:#059669; width:100%;" onclick="saveSchedule('${id}')">Zamanlayıcı Ayarlarını Kaydet</button>
                                </div>
                            </div>
                        `;

                        document.getElementById(`sched_enable_${id}`).checked = info.sched_enabled;
                        info.sched_days.forEach(d => {
                            const cb = card.querySelector(`.sched-day-${id}[value="${d}"]`);
                            if (cb) cb.checked = true;
                        });
                        
                        // Mevcut slot'ları yükle
                        const slots = (info.sched_slots && info.sched_slots.length > 0) 
                            ? info.sched_slots 
                            : [{start: info.sched_start || '00:00', end: info.sched_end || '00:00'}];
                        slots.forEach(slot => addSlotRow(id, slot.start, slot.end));
                        
                        toggleScheduleUI(id);
                    } else {
                        card.querySelector('.channel-title').innerText = info.name || id;
                        
                        const badgeState = card.querySelector('.badge-state');
                        badgeState.className = `status-badge badge-state ${info.enabled ? 'badge-active' : 'badge-disabled'}`;
                        badgeState.innerText = info.enabled ? 'YAYINDA' : 'KAPALI';
                        
                        const badgeViewers = card.querySelector('.badge-viewers-count');
                        badgeViewers.innerText = `👥 ${info.viewers} İzleyici`;
                        
                        const badgeFfmpeg = card.querySelector('.badge-ffmpeg');
                        badgeFfmpeg.className = `status-badge badge-ffmpeg ${info.running ? 'badge-ffmpeg-on' : 'badge-ffmpeg-off'}`;
                        badgeFfmpeg.innerText = info.running ? '● FFmpeg Aktif' : '○ FFmpeg Kapalı';
                        
                        const btnToggle = card.querySelector('.btn-toggle-action');
                        btnToggle.className = `btn btn-toggle-action ${info.enabled ? 'btn-off' : 'btn-on'}`;
                        btnToggle.innerText = info.enabled ? 'YAYINI KAPAT' : 'YAYINI AÇ';
                        btnToggle.setAttribute('onclick', `toggleChannel('${id}', ${!info.enabled})`);
                    }
                }
            } catch(e) {}
        }

        function toggleScheduleUI(id) {
            const enabled = document.getElementById(`sched_enable_${id}`).checked;
            document.getElementById(`sched_fields_${id}`).style.display = enabled ? "flex" : "none";
        }

        async function saveSchedule(id) {
            const key = getStoredKey();
            const enabled = document.getElementById(`sched_enable_${id}`).checked;
            
            const days = [];
            document.querySelectorAll(`.sched-day-${id}:checked`).forEach(cb => {
                days.push(cb.value);
            });

            // Çoklu slot verilerini topla
            const starts = document.querySelectorAll(`.sched-slot-start-${id}`);
            const ends = document.querySelectorAll(`.sched-slot-end-${id}`);
            const slots = [];
            for (let i = 0; i < starts.length; i++) {
                const s = starts[i].value || "00:00";
                const e = ends[i].value || "00:00";
                slots.push({start: s, end: e});
            }

            if (slots.length === 0) {
                showToast('⚠ En az bir zaman aralığı eklemelisiniz!', '#dc2626');
                return;
            }

            try {
                const slotsJson = JSON.stringify(slots);
                const res = await fetch(`/admin/update_schedule?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&enabled=${enabled}&days=${days.join(",")}&slots=${encodeURIComponent(slotsJson)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    showToast('✅ Zamanlayıcı ayarları başarıyla kaydedildi!');
                    setTimeout(loadStatus, 500);
                } else {
                    showToast('❌ Ayarlar kaydedilemedi!', '#dc2626');
                }
            } catch(e) {
                showToast('❌ Bağlantı hatası: ' + e.message, '#dc2626');
            }
        }

        async function updateStandbyMessage() {
            const key = getStoredKey();
            const message = document.getElementById('standbyMessageInput').value.trim();
            if(!message) {
                showToast("Lütfen geçerli bir mesaj girin!", "#dc2626");
                return;
            }

            try {
                const res = await fetch(`/admin/update_standby?key=${encodeURIComponent(key)}&message=${encodeURIComponent(message)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    showToast('✅ Standby ekran mesajı güncellendi ve yeniden oluşturuldu!');
                } else {
                    showToast('❌ Güncelleme başarısız oldu!', '#dc2626');
                }
            } catch(e) {
                showToast('❌ Bağlantı hatası: ' + e.message, '#dc2626');
            }
        }

        async function toggleChannel(id, enable) {
            const key = getStoredKey();
            const res = await fetch(`/admin/toggle?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&enable=${enable}&_=${Date.now()}`, { cache: 'no-store' });
            if (res.ok) {
                setTimeout(loadStatus, 500);
            } else {
                alert('Oturum Geçersiz veya Şifre Hatalı!');
                logout();
            }
        }

        async function updateChannelUrl(id) {
            const key = getStoredKey();
            const inputEl = document.getElementById(`url_${id}`);
            const newUrl = inputEl.value.trim();
            if(!newUrl) {
                showToast("Lütfen geçerli bir yayın linki girin!", "#dc2626");
                return;
            }
            
            try {
                const res = await fetch(`/admin/update_url?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&url=${encodeURIComponent(newUrl)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    const data = await res.json();
                    inputEl.value = data.url || newUrl;
                    inputEl.blur();
                    showToast('✅ Yayın linki başarıyla güncellendi!');
                    setTimeout(loadStatus, 800);
                } else {
                    showToast('❌ Hata oluştu veya yetkisiz!', '#dc2626');
                    if (res.status === 401) logout();
                }
            } catch(e) {
                showToast('❌ Bağlantı hatası: ' + e.message, '#dc2626');
            }
        }

        async function init() {
            const storedKey = getStoredKey();
            if (storedKey) {
                const isValid = await verifyKey(storedKey);
                if (isValid) {
                    showPanel();
                    return;
                }
            }
            logout();
        }

        init();
    </script>
</body>
</html>
"""

async def handle_admin_page(request):
    return web.Response(text=ADMIN_HTML, content_type="text/html")

async def handle_admin_verify(request):
    key = request.query.get("key")
    if key == ADMIN_KEY:
        return web.json_response({"valid": True}, headers=NO_CACHE_HEADERS)
    return web.json_response({"valid": False}, status=401, headers=NO_CACHE_HEADERS)

async def handle_admin_toggle(request):
    key = request.query.get("key")
    cid = request.query.get("id")
    enable = request.query.get("enable") == "true"

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    st.enabled = enable
    st.ch["enabled"] = enable

    try:
        channels_data = [s.ch for s in manager.streams.values()]
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(channels_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Kanal durumu diske kaydedilemedi: {e}")

    if not enable:
        await st.stop()
    else:
        st.touch()          
        await st.start()    
        await asyncio.sleep(0.5) 

    return web.json_response({"success": True, "id": cid, "enabled": st.enabled}, headers=NO_CACHE_HEADERS)

async def handle_admin_update_url(request):
    key = request.query.get("key")
    cid = request.query.get("id")
    new_url = request.query.get("url")

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    if not new_url:
        return web.Response(status=400, text="Geçersiz URL")

    success = await manager.update_channel_url(cid, new_url)
    if not success:
        return web.Response(status=500, text="Güncelleme başarısız")
    
    return web.json_response({
        "success": True, 
        "id": cid, 
        "url": st.src
    }, headers=NO_CACHE_HEADERS)

async def handle_admin_update_standby(request):
    key = request.query.get("key")
    message = request.query.get("message")

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    if message is None:
        return web.Response(status=400, text="Geçersiz Mesaj")

    server_state.standby_message = message
    server_state.save()
    generate_standby_clip(force=True)

    return web.json_response({"success": True, "message": message}, headers=NO_CACHE_HEADERS)

async def handle_admin_update_schedule(request):
    key = request.query.get("key")
    cid = request.query.get("id")
    enabled = request.query.get("enabled") == "true"
    days_str = request.query.get("days", "")
    slots_str = request.query.get("slots", "")  # JSON string (çoklu slot)
    
    # Geriye dönük uyumluluk için eski parametreler
    start = request.query.get("start", "")
    end = request.query.get("end", "")

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    try:
        days_list = [int(d) for d in days_str.split(",") if d.strip() != ""]
    except ValueError:
        days_list = []

    # Çoklu slot verisini parse et
    slots_list = []
    if slots_str:
        try:
            parsed = json.loads(slots_str)
            if isinstance(parsed, list):
                for s in parsed:
                    if isinstance(s, dict):
                        slot_start = s.get("start", "00:00") or "00:00"
                        slot_end = s.get("end", "00:00") or "00:00"
                        slots_list.append({"start": slot_start, "end": slot_end})
        except Exception as e:
            log.warning(f"Slot parse hatası: {e}")
            slots_list = []

    # Eğer slots gelmediyse ve eski start/end gönderilmişse geriye dönük uyumluluk
    if not slots_list and start and end:
        slots_list = [{"start": start, "end": end}]

    st.ch["sched_enabled"] = enabled
    st.ch["sched_days"] = days_list
    st.ch["sched_slots"] = slots_list
    
    # Geriye dönük uyumluluk için ilk slot'u da eski alanlara yaz
    if slots_list:
        st.ch["sched_start"] = slots_list[0]["start"]
        st.ch["sched_end"] = slots_list[0]["end"]

    # Zamanlayıcı değiştiğinde durum takipçisini sıfırla ki takvimi hemen eşitlesin
    st.last_sched_state = None

    try:
        channels_data = [s.ch for s in manager.streams.values()]
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(channels_data, f, indent=2, ensure_ascii=False)
        log.info(f"Kanal zamanlayıcı ayarları kaydedildi: {cid} ({len(slots_list)} slot)")
    except Exception as e:
        log.error(f"Zamanlayıcı ayarları kaydedilemedi: {e}")

    sched_state = check_schedule(st)
    if sched_state is not None:
        st.enabled = sched_state
        if sched_state and not st.is_alive():
            await st.start()
        elif not sched_state and st.is_alive():
            await st.stop()

    return web.json_response({"success": True, "id": cid, "slots_count": len(slots_list)}, headers=NO_CACHE_HEADERS)


# ==================== APP STARTUP & CLEANUP ====================
async def on_startup(app):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    generate_standby_clip()
    app["monitor_task"] = asyncio.create_task(manager.monitor())
    app["save_task"] = asyncio.create_task(tracker.periodic_save())
    app["uptime_task"] = asyncio.create_task(uptime_tracker_task())
    log.info("IPTV HLS Re-stream Proxy başlatıldı.")

async def on_cleanup(app):
    tracker.save()
    server_state.save()
    for st in manager.streams.values():
        await st.stop()
    for task_name in ["monitor_task", "save_task", "uptime_task"]:
        t = app.get(task_name)
        if t:
            t.cancel()

def make_app():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_get("/admin/verify", handle_admin_verify)
    app.router.add_get("/admin/toggle", handle_admin_toggle)
    app.router.add_get("/admin/update_url", handle_admin_update_url)
    app.router.add_get("/admin/update_standby", handle_admin_update_standby)
    app.router.add_get("/admin/update_schedule", handle_admin_update_schedule)
    app.router.add_get("/live/{channel_id}.m3u8", handle_m3u8)
    app.router.add_get("/hls/standby/seg.ts", handle_standby_segment)
    app.router.add_get("/hls/{channel_id}/{name}", handle_segment)
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    if shutil.which(FFMPEG_BIN) is None:
        raise SystemExit("HATA: ffmpeg bulunamadı.")
    web.run_app(make_app(), host=BIND_HOST, port=PROXY_PORT, access_log=None)
