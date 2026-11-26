#!/usr/bin/env python3
"""
radio-tui - Lightweight curses internet radio player
Supports SomaFM, Radio Browser (40k+ worldwide stations), and custom favorites.
Plays through PipeWire/PulseAudio or ALSA default (dmix / BlueALSA bridge).
"""

import atexit
import curses
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP_VERSION = "1.2.0"
CONFIG_DIR = Path.home() / ".config" / "radio-tui"
FAV_FILE = CONFIG_DIR / "favorites.json"
RECENT_FILE = CONFIG_DIR / "recent.json"
RECENT_MAX = 50
USER_AGENT = f"radio-tui/{APP_VERSION}"

RADIO_BROWSER_HOSTS = [
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]


PR_SET_PDEATHSIG = 1


class Terminate(Exception):
    """Raised from a signal handler to unwind and clean up."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _set_pdeathsig(sig=signal.SIGTERM):
    """Ask the kernel to deliver `sig` to this process when its parent dies.

    This is the safety net that guarantees the ffmpeg/paplay pipeline never
    outlives radio-tui, even if radio-tui is SIGKILLed.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, int(sig), 0, 0, 0)
    except Exception:
        pass


def _child_preexec(pgid=None):
    """preexec_fn for the audio children.

    The child puts itself in its own process group (so we can signal the whole
    pipeline at once) and sets PR_SET_PDEATHSIG so it dies with us.
    """
    def _fn():
        try:
            os.setpgid(0, 0 if pgid is None else pgid)
        except Exception:
            pass
        _set_pdeathsig(signal.SIGTERM)
    return _fn


def _drain_stream(stream, sink):
    """Read a child process' stderr into `sink` so the pipe never fills up
    (a full stderr pipe would block the child)."""
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            sink.append(chunk)
    except Exception:
        pass


YOUTUBE_HOSTS = (
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
)

# Prefer the highest quality AAC (m4a) audio, then any mp4a stream, then
# whatever else is available.
YTDLP_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"

# How long to wait for ffmpeg to open the input before considering it failed.
OPEN_GRACE = 6.0


def is_youtube_url(url):
    """True for youtube.com / youtu.be links, which need yt-dlp."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def _http_get_text(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_json(url, timeout=10):
    return json.loads(_http_get_text(url, timeout=timeout))


def fetch_somafm():
    data = _http_get_json("https://somafm.com/channels.json")
    stations = []
    for ch in data.get("channels", []):
        cid = ch.get("id", "")
        pls_url = ""
        for pl in ch.get("playlists", []):
            if pl.get("format") in ("mp3", "aac", "aacp"):
                pls_url = pl.get("url", "")
                if pl.get("format") == "mp3":
                    break
        stream_url = f"https://ice1.somafm.com/{cid}-128-mp3" if cid else pls_url
        stations.append({
            "name": f"SomaFM: {ch.get('title', cid)}",
            "url": stream_url,
            "pls": pls_url,
            "country": "US",
            "tags": ch.get("genre", ""),
            "bitrate": "128",
            "codec": "MP3",
        })
    return stations


def _rb_request(path):
    last_err = None
    for host in RADIO_BROWSER_HOSTS:
        try:
            data = _http_get_json(f"{host}{path}", timeout=10)
            out = []
            for st in data:
                url = (st.get("url_resolved") or st.get("url") or "").strip()
                if not url:
                    continue
                out.append({
                    "name": (st.get("name") or "Unknown").strip(),
                    "url": url,
                    "country": (st.get("countrycode") or st.get("country") or "").strip(),
                    "tags": (st.get("tags") or "").strip(),
                    "bitrate": str(st.get("bitrate") or ""),
                    "codec": (st.get("codec") or "").strip(),
                })
            return out
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Radio Browser unreachable: {last_err}")


def fetch_rb_top(limit=100, offset=0):
    return _rb_request(f"/json/stations/topvote/{limit}?offset={offset}&hidebroken=true")


def fetch_rb_search(query, limit=100, offset=0):
    q = urllib.parse.quote(query.strip())
    return _rb_request(f"/json/stations/search?name={q}&order=votes&reverse=true&hidebroken=true&limit={limit}&offset={offset}")


def fetch_rb_country(country, limit=100, offset=0):
    c = urllib.parse.quote(country.strip())
    param = "countrycode" if len(country.strip()) == 2 else "country"
    return _rb_request(f"/json/stations/search?{param}={c}&order=votes&reverse=true&hidebroken=true&limit={limit}&offset={offset}")


def fetch_rb_tag(tag, limit=100, offset=0):
    t = urllib.parse.quote(tag.strip())
    return _rb_request(f"/json/stations/search?tag={t}&order=votes&reverse=true&hidebroken=true&limit={limit}&offset={offset}")


def _rb_list(path):
    last_err = None
    for host in RADIO_BROWSER_HOSTS:
        try:
            return _http_get_json(f"{host}{path}", timeout=10)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Radio Browser unreachable: {last_err}")


def fetch_rb_countries(limit=100, offset=0):
    data = _rb_list(f"/json/countries?order=stationcount&reverse=true&hidebroken=true&limit={limit}&offset={offset}")
    return [
        {"_kind": "country", "name": f"{c.get('name', '?')} ({c.get('stationcount', 0)} stations)", "value": c.get("name", "")}
        for c in data if c.get("name") and c.get("stationcount", 0) > 0
    ]


def fetch_rb_tags(limit=100, offset=0):
    data = _rb_list(f"/json/tags?order=stationcount&reverse=true&hidebroken=true&limit={limit}&offset={offset}")
    return [
        {"_kind": "tag", "name": f"#{t.get('name', '?')} ({t.get('stationcount', 0)} stations)", "value": t.get("name", "")}
        for t in data if t.get("name") and t.get("stationcount", 0) > 0
    ]


def resolve_stream_url(station):
    url = station.get("url", "").strip()
    if not url:
        url = station.get("pls", "").strip()
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".pls"):
        txt = _http_get_text(url, timeout=8)
        for line in txt.splitlines():
            if line.lower().startswith("file1="):
                return line.split("=", 1)[1].strip()
    elif lower.endswith(".m3u"):
        txt = _http_get_text(url, timeout=8)
        for line in txt.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s
    return url


def load_favorites():
    try:
        if FAV_FILE.exists():
            data = json.loads(FAV_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_favorites(favs):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        FAV_FILE.write_text(json.dumps(favs, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_recent():
    """Recently played stations, most recent first."""
    try:
        if RECENT_FILE.exists():
            data = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict) and s.get("url")]
    except Exception:
        pass
    return []


def save_recent(stations):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RECENT_FILE.write_text(json.dumps(stations[:RECENT_MAX], indent=2), encoding="utf-8")
    except Exception:
        pass


def remember_station(station, recent):
    """Move a station to the front of the recent list (in place)."""
    if not station or not station.get("url"):
        return
    entry = {
        "name": station.get("name", "Radio"),
        "url": station.get("url", ""),
        "country": station.get("country", ""),
        "tags": station.get("tags", ""),
        "bitrate": station.get("bitrate", ""),
        "codec": station.get("codec", ""),
    }
    recent[:] = [s for s in recent if s.get("url") != entry["url"]]
    recent.insert(0, entry)
    del recent[RECENT_MAX:]
    save_recent(recent)


def adjust_volume(delta_pct=5):
    sign = "+" if delta_pct >= 0 else "-"
    abs_pct = abs(int(delta_pct))
    if shutil.which("pactl"):
        try:
            r = subprocess.run(["pactl", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
            if r.returncode == 0:
                subprocess.run(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{sign}{abs_pct}%"],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )
                out = subprocess.check_output(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], stderr=subprocess.DEVNULL, timeout=2).decode()
                m = re.search(r"(\d+)%", out)
                return f"Volume: {m.group(1)}%" if m else f"Volume {sign}{abs_pct}%"
        except Exception:
            pass
    if shutil.which("bluealsa-cli"):
        try:
            pcms = subprocess.check_output(["bluealsa-cli", "list-pcms"], stderr=subprocess.DEVNULL, timeout=2).decode().splitlines()
            pcm = next((l.strip() for l in pcms if "a2dpsrc/sink" in l or "scosrc/sink" in l), "")
            if pcm:
                vol_out = subprocess.check_output(["bluealsa-cli", "volume", pcm], stderr=subprocess.DEVNULL, timeout=2).decode()
                nums = re.findall(r"\b(\d+)\b", vol_out)
                cur = int(nums[-1]) if nums else 64
                step = max(1, round(127 * abs_pct / 100))
                new_v = max(0, min(127, cur + (step if delta_pct >= 0 else -step)))
                subprocess.run(["bluealsa-cli", "volume", pcm, str(new_v), str(new_v)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                return f"Bluetooth Volume: {round(new_v * 100 / 127)}%"
        except Exception:
            pass
    if shutil.which("amixer"):
        for ctl in ("Master", "PCM"):
            try:
                out = subprocess.check_output(["amixer", "sset", ctl, f"{abs_pct}%{sign}"], stderr=subprocess.DEVNULL, timeout=2).decode()
                m = re.search(r"\[(\d+)%\]", out)
                return f"ALSA {ctl}: {m.group(1)}%" if m else f"ALSA {ctl} {sign}{abs_pct}%"
            except Exception:
                continue
    return "No mixer control available"


class RadioPlayer:
    def __init__(self):
        self._lock = threading.Lock()
        self._gen = 0
        self._pgid = None
        self._procs = []
        self.state = "idle"
        self.station_name = ""
        self.error_msg = ""
        self.source_note = ""
        atexit.register(self.stop)

    def _terminate(self, pgid, procs):
        """Kill the audio pipeline: process group first, then the processes
        themselves (the group may not exist yet if setpgid raced), then
        escalate to SIGKILL for anything still alive."""
        if pgid:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                pass
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=1.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass

    def stop(self):
        with self._lock:
            self._gen += 1
            pgid = self._pgid
            procs = list(self._procs)
            self._pgid = None
            self._procs = []
            self.state = "idle"
            self.error_msg = ""
            self.source_note = ""
        self._terminate(pgid, procs)

    def play(self, station):
        self.stop()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self.state = "loading"
            self.station_name = station.get("name", "Radio")
            self.error_msg = ""
            self.source_note = ""
        t = threading.Thread(target=self._worker, args=(gen, station), daemon=True)
        t.start()

    def _is_current(self, gen):
        with self._lock:
            return self._gen == gen

    def _audio_sink(self):
        """Return the playback command, or None if no sink is available."""
        paplay_bin = None
        if shutil.which("pactl"):
            r = subprocess.run(["pactl", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
            if r.returncode == 0:
                paplay_bin = shutil.which("paplay")
        aplay_bin = shutil.which("aplay")
        if paplay_bin:
            return [paplay_bin, "--raw", "--format=s16le", "--rate=48000", "--channels=2",
                    "--client-name=radio-tui"]
        if aplay_bin:
            return [aplay_bin, "-q", "-D", "default", "-f", "S16_LE", "-r", "48000", "-c", "2", "-t", "raw"]
        return None

    def _spawn(self, ff_bin, ap_cmd, url, producer_cmd):
        """Spawn [producer]? -> ffmpeg -> audio sink.

        Without a producer ffmpeg reads the URL itself.  With one (curl for
        legacy SHOUTcast servers, yt-dlp for YouTube) the producer feeds raw
        bytes into ffmpeg's stdin.
        """
        prod_proc = None
        if producer_cmd is None:
            ff_cmd = [
                ff_bin, "-loglevel", "error",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", url,
                "-vn", "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
            ]
            ff_proc = subprocess.Popen(
                ff_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=_child_preexec(),
            )
            leader = ff_proc.pid
            procs = [ff_proc]
        else:
            prod_proc = subprocess.Popen(
                producer_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=_child_preexec(),
            )
            ff_cmd = [
                ff_bin, "-loglevel", "error",
                "-i", "pipe:0",
                "-vn", "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
            ]
            ff_proc = subprocess.Popen(
                ff_cmd, stdin=prod_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=_child_preexec(prod_proc.pid),
            )
            if prod_proc.stdout:
                prod_proc.stdout.close()
            leader = prod_proc.pid
            procs = [prod_proc, ff_proc]

        ap_proc = subprocess.Popen(
            ap_cmd, stdin=ff_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            preexec_fn=_child_preexec(leader),
        )
        if ff_proc.stdout:
            ff_proc.stdout.close()

        # Do the same setpgid from the parent side: the child's own setpgid
        # may not have run yet, and a stop() in that window would killpg an
        # empty group and leave the pipeline running.
        try:
            os.setpgid(leader, leader)
        except Exception:
            pass
        for p in procs:
            try:
                os.setpgid(p.pid, leader)
            except Exception:
                pass
        try:
            os.setpgid(ap_proc.pid, leader)
        except Exception:
            pass
        return leader, procs + [ap_proc], ff_proc, ap_proc, prod_proc

    def _run_pipeline(self, gen, station, ff_bin, ap_cmd, url, producer_cmd, note):
        """Run one playback pipeline and update the player state.

        Returns a dict with "status" (playing / ended / failed_open / stale),
        the captured stderr and whether the failure looks like a legacy
        SHOUTcast (ICY) response.
        """
        try:
            leader, procs, ff_proc, ap_proc, prod_proc = self._spawn(ff_bin, ap_cmd, url, producer_cmd)
        except Exception as e:
            with self._lock:
                if self._gen == gen:
                    self.state = "error"
                    self.error_msg = str(e)[:80]
            return {"status": "failed_open", "error": str(e), "icy": False}

        ff_err = []
        prod_err = []
        threading.Thread(target=_drain_stream, args=(ff_proc.stderr, ff_err), daemon=True).start()
        if prod_proc is not None:
            threading.Thread(target=_drain_stream, args=(prod_proc.stderr, prod_err), daemon=True).start()

        with self._lock:
            if self._gen != gen:
                stale = True
            else:
                stale = False
                self._pgid = leader
                self._procs = procs
                self.state = "playing"
                self.source_note = note
                self.error_msg = ""
        if stale:
            self._terminate(leader, procs)
            return {"status": "stale"}

        # ffmpeg exits immediately when the input cannot be opened.  Watch for
        # that early exit so the curl fallback can take over.
        deadline = time.time() + OPEN_GRACE
        while time.time() < deadline and ff_proc.poll() is None:
            time.sleep(0.15)

        if ff_proc.poll() is not None:
            time.sleep(0.2)  # let the stderr drain threads catch up
            ff_text = b"".join(ff_err).decode(errors="replace")
            prod_text = b"".join(prod_err).decode(errors="replace")
            rc = ff_proc.returncode
            self._terminate(leader, procs)
            icy = ("Malformed HTTP status line" in ff_text
                   or "Invalid data found when processing input" in ff_text)
            with self._lock:
                if self._gen == gen:
                    self._pgid = None
                    self._procs = []
                    self.source_note = ""
                    if rc == 0:
                        self.state = "idle"
                    else:
                        self.state = "error"
                        detail = prod_text or ff_text
                        lines = [l for l in detail.strip().splitlines() if l.strip()]
                        self.error_msg = (lines[-1] if lines else f"ffmpeg exited {rc}")[:80]
            return {"status": "ended" if rc == 0 else "failed_open",
                    "error": ff_text, "icy": icy}

        # Normal playback: wait for the sink to finish.
        _, ap_err = ap_proc.communicate()
        ff_proc.wait()
        self._terminate(leader, procs)  # reap a producer that is still running
        ff_text = b"".join(ff_err).decode(errors="replace")
        prod_text = b"".join(prod_err).decode(errors="replace")
        with self._lock:
            if self._gen == gen:
                if ap_proc.returncode not in (0, None, -signal.SIGTERM, -signal.SIGKILL):
                    self.state = "error"
                    self.error_msg = (ap_err or b"").decode(errors="replace").strip()[:80]
                elif ff_proc.returncode not in (0, None, -signal.SIGTERM, -signal.SIGKILL):
                    # The producer (yt-dlp) usually knows better what went wrong.
                    lines = [l for l in (prod_text or ff_text).strip().splitlines() if l.strip()]
                    self.state = "error"
                    self.error_msg = (lines[-1] if lines else f"ffmpeg exited {ff_proc.returncode}")[:80]
                else:
                    self.state = "idle"
                self._pgid = None
                self._procs = []
                self.source_note = ""
        return {"status": "ended"}

    def _worker(self, gen, station):
        try:
            ff_bin = shutil.which("ffmpeg")
            ap_cmd = self._audio_sink()
            if not ff_bin or not ap_cmd:
                with self._lock:
                    if self._gen == gen:
                        self.state = "error"
                        self.error_msg = "Need ffmpeg + aplay (or paplay)"
                return

            stream_url = resolve_stream_url(station)

            # YouTube links are not plain streams: yt-dlp resolves them and
            # hands the best AAC audio to ffmpeg.
            if is_youtube_url(stream_url):
                yt_dlp = shutil.which("yt-dlp")
                if not yt_dlp:
                    with self._lock:
                        if self._gen == gen:
                            self.state = "error"
                            self.error_msg = "YouTube URL needs yt-dlp (pip install yt-dlp)"
                    return
                producer = [yt_dlp, "-f", YTDLP_FORMAT, "--no-playlist", "-q", "-o", "-", stream_url]
                self._run_pipeline(gen, station, ff_bin, ap_cmd, stream_url, producer, "yt-dlp")
                return

            res = self._run_pipeline(gen, station, ff_bin, ap_cmd, stream_url, None, "")
            # Legacy SHOUTcast servers answer with an "ICY 200 OK" line instead
            # of an HTTP status line, which ffmpeg >= 9.1 refuses to parse.
            # Retry through curl, which accepts that response.
            if (res.get("status") == "failed_open" and res.get("icy")
                    and self._is_current(gen)
                    and stream_url.lower().startswith(("http://", "https://"))):
                curl = shutil.which("curl")
                if curl:
                    producer = [curl, "-sS", "-N", "--http0.9", "--connect-timeout", "20", stream_url]
                    self._run_pipeline(gen, station, ff_bin, ap_cmd, stream_url, producer, "curl")
        except Terminate:
            raise
        except Exception as e:
            with self._lock:
                if self._gen == gen:
                    self.state = "error"
                    self.error_msg = str(e)[:80]


def _drain_input(stdscr, timeout=0.05, restore_timeout=250):
    """Throw away anything already queued on the input stream.

    Needed after ESC: a lone ESC and the start of an escape sequence (arrow
    keys, function keys) are indistinguishable until the rest of the sequence
    arrives.  Without draining, the tail of the sequence leaks into the main
    loop and triggers random commands (e.g. "[A" -> "a" -> add-station prompt).
    """
    try:
        stdscr.timeout(int(timeout * 1000))
        while True:
            if stdscr.getch() == -1:
                break
    except Exception:
        pass
    finally:
        try:
            stdscr.timeout(restore_timeout)
        except Exception:
            pass


def prompt_input(stdscr, prompt):
    h, w = stdscr.getmaxyx()
    stdscr.timeout(-1)
    curses.noecho()
    try:
        curses.curs_set(1)
    except Exception:
        pass
    buf = []
    try:
        while True:
            stdscr.attron(curses.color_pair(2))
            stdscr.move(h - 2, 0)
            stdscr.clrtoeol()
            text = prompt + "".join(buf)
            stdscr.addstr(h - 2, 1, text[:w - 2])
            stdscr.attroff(curses.color_pair(2))
            stdscr.refresh()
            ch = stdscr.getch()
            if ch == 27:  # ESC cancels
                _drain_input(stdscr, restore_timeout=-1)
                return ""
            if ch in (10, 13, curses.KEY_ENTER):
                return "".join(buf).strip()
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                continue
            if ch in (9, curses.KEY_BTAB):
                continue
            if ch < 0 or ch in (
                curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT,
                curses.KEY_HOME, curses.KEY_END, curses.KEY_DC, curses.KEY_RESIZE,
                curses.KEY_PPAGE, curses.KEY_NPAGE,
            ):
                continue
            if 32 <= ch < 127 or ch >= 160:
                if len(buf) < 120:
                    buf.append(chr(ch))
    except Terminate:
        raise
    except Exception:
        return ""
    finally:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.timeout(250)


def main_curses(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)

    stdscr.timeout(250)
    player = RadioPlayer()
    favs = load_favorites()

    tabs = ["Favorites", "SomaFM", "Top World", "Countries", "Genres", "Search", "Recent"]
    tab_idx = 1 if not favs else 0
    stations = list(favs) if tab_idx == 0 else []
    recent = load_recent()
    idx = 0
    scroll = 0
    msg = "Enter play/open  Tab/1-7 switch feed  / search  c country  g genre"
    loading_list = False
    sub_label = ""
    page_size = 100
    offset = 0
    has_more = False
    load_gen = 0
    filter_text = ""

    def _fetch_page(t_idx, arg, off):
        if t_idx == 1:
            return fetch_somafm(), False
        if t_idx == 2:
            res = fetch_rb_top(limit=page_size, offset=off)
            return res, len(res) >= page_size
        if t_idx == 3:
            res = fetch_rb_country(arg, limit=page_size, offset=off) if arg else fetch_rb_countries(limit=page_size, offset=off)
            return res, len(res) >= page_size
        if t_idx == 4:
            res = fetch_rb_tag(arg, limit=page_size, offset=off) if arg else fetch_rb_tags(limit=page_size, offset=off)
            return res, len(res) >= page_size
        if t_idx == 5:
            res = fetch_rb_search(arg or "radio", limit=page_size, offset=off)
            return res, len(res) >= page_size
        if t_idx == 6:
            return list(recent), False
        return [], False

    def load_tab(new_idx, arg=None):
        nonlocal tab_idx, stations, idx, scroll, msg, loading_list, sub_label, offset, has_more, load_gen, filter_text
        tab_idx = new_idx
        idx = 0
        scroll = 0
        offset = 0
        has_more = False
        filter_text = ""
        sub_label = arg or ""
        load_gen += 1
        gen = load_gen
        if tab_idx == 0:
            stations = list(favs)
            msg = f"Favorites: {len(stations)} station(s)"
            return
        if tab_idx == 6:
            recent[:] = load_recent()
            stations = list(recent)
            msg = f"Recent: {len(stations)} station(s)" if stations else "Recent: no stations played yet"
            return
        loading_list = True
        msg = f"Loading {tabs[tab_idx]}" + (f" ({arg})" if arg else "") + "..."
        stdscr.refresh()

        def _bg():
            nonlocal stations, msg, loading_list, offset, has_more
            try:
                res, more = _fetch_page(tab_idx, arg, 0)
                if gen != load_gen:
                    return
                stations = res
                offset = len(res)
                has_more = more
                msg = f"Loaded {len(res)} items ({tabs[tab_idx]}" + (f": {arg}" if arg else "") + ")"
            except Exception as e:
                if gen == load_gen:
                    msg = f"Fetch error: {e}"
            finally:
                if gen == load_gen:
                    loading_list = False

        threading.Thread(target=_bg, daemon=True).start()

    def load_more():
        nonlocal loading_list
        if loading_list or not has_more or tab_idx in (0, 1, 6):
            return
        loading_list = True
        gen = load_gen
        cur_tab, cur_arg, cur_off = tab_idx, sub_label, offset
        msg_prefix = f"Loading more {tabs[cur_tab]}..."
        nonlocal msg
        msg = msg_prefix

        def _bg():
            nonlocal stations, msg, loading_list, offset, has_more
            try:
                res, more = _fetch_page(cur_tab, cur_arg, cur_off)
                if gen != load_gen:
                    return
                seen = {(s.get("url") or s.get("name")) for s in stations}
                added = [s for s in res if (s.get("url") or s.get("name")) not in seen]
                stations.extend(added)
                offset += len(res)
                has_more = more and bool(res)
                msg = f"Loaded {len(stations)} items ({tabs[cur_tab]}" + (f": {cur_arg}" if cur_arg else "") + ")"
            except Exception as e:
                if gen == load_gen:
                    msg = f"Fetch error: {e}"
            finally:
                if gen == load_gen:
                    loading_list = False

        threading.Thread(target=_bg, daemon=True).start()

    if tab_idx == 1:
        load_tab(1)

    try:
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()

            # Header
            hdr = f" radio-tui v{APP_VERSION}  " + "  ".join(
                f"[{i+1}:{t}]" if i == tab_idx else f" {i+1}:{t} " for i, t in enumerate(tabs)
            )
            stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(0, 0, hdr[:w - 1])
            stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

            # List
            view_list = (
                [s for s in stations if filter_text.lower() in (s.get("name", "") + " " + s.get("tags", "")).lower()]
                if filter_text else stations
            )
            if idx >= len(view_list):
                idx = max(0, len(view_list) - 1)
            list_h = max(1, h - 5)
            if idx < scroll:
                scroll = idx
            elif idx >= scroll + list_h:
                scroll = idx - list_h + 1

            fav_urls = {f.get("url") for f in favs}
            for row in range(list_h):
                si = scroll + row
                if si >= len(view_list):
                    break
                st = view_list[si]
                star = "*" if st.get("url") in fav_urls else " "
                meta = " ".join(x for x in [st.get("country", ""), st.get("codec", ""), st.get("bitrate", "") + ("k" if st.get("bitrate") else "")] if x)
                line = f"{'>' if si == idx else ' '} {star} {st.get('name', '')}"
                if meta:
                    line += f"  [{meta}]"
                if si == idx:
                    stdscr.attron(curses.color_pair(2) | curses.A_BOLD)
                stdscr.addstr(row + 2, 1, line[:w - 2])
                if si == idx:
                    stdscr.attroff(curses.color_pair(2) | curses.A_BOLD)

            # Player bar
            if player.state == "playing":
                pcol, ptxt = 3, f"Playing: {player.station_name}"
                if player.source_note:
                    pcol, ptxt = 3, f"Playing: {player.station_name} ({player.source_note})"
            elif player.state == "loading":
                pcol, ptxt = 2, f"Connecting: {player.station_name}..."
            elif player.state == "error":
                pcol, ptxt = 4, f"Error: {player.error_msg}"
            else:
                pcol, ptxt = 1, "Idle"
            stdscr.attron(curses.color_pair(pcol))
            stdscr.addstr(h - 3, 1, ptxt[:w - 2])
            stdscr.attroff(curses.color_pair(pcol))

            status_msg = f"[filter: {filter_text}] {msg}" if filter_text else msg
            stdscr.addstr(h - 2, 1, status_msg[:w - 2])
            stdscr.attron(curses.color_pair(1))
            stdscr.addstr(h - 1, 1, "Enter play/open  Esc back  l filter  s stop  +/- vol  f fav  a add  / search  c country  g genre  1-7/Tab  q quit"[:w - 2])
            stdscr.attroff(curses.color_pair(1))
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                if has_more and not loading_list and not filter_text and view_list and idx >= len(view_list) - 5:
                    load_more()
                continue
            if key in (ord("q"), ord("Q")):
                break
            elif key == 27:
                # ESC: cancel filter / leave a country or genre listing.
                _drain_input(stdscr)
                if filter_text:
                    filter_text = ""
                    idx = 0
                    scroll = 0
                    msg = "Filter cleared"
                elif sub_label and tab_idx in (3, 4):
                    load_tab(tab_idx)
            elif key in (curses.KEY_BACKSPACE, 127, 8) and sub_label and tab_idx in (3, 4):
                load_tab(tab_idx)
            elif key == curses.KEY_UP:
                idx = max(0, idx - 1)
            elif key == curses.KEY_DOWN:
                idx = min(max(0, len(view_list) - 1), idx + 1)
                if has_more and not loading_list and not filter_text and view_list and idx >= len(view_list) - 5:
                    load_more()
            elif key == curses.KEY_PPAGE:
                idx = max(0, idx - list_h)
            elif key == curses.KEY_NPAGE:
                idx = min(max(0, len(view_list) - 1), idx + list_h)
                if has_more and not loading_list and not filter_text and view_list and idx >= len(view_list) - 5:
                    load_more()
            elif key in (ord("l"), ord("L")):
                fval = prompt_input(stdscr, "Filter list (blank to clear): ")
                filter_text = fval
                idx = 0
                scroll = 0
                msg = f"Filtered ({len([s for s in stations if fval.lower() in (s.get('name', '') + ' ' + s.get('tags', '')).lower()])} matches)" if fval else "Filter cleared"
            elif key in (9,):
                load_tab((tab_idx + 1) % len(tabs))
            elif ord("1") <= key <= ord("7"):
                n = key - ord("1")
                if n == 5:
                    q = prompt_input(stdscr, "Search station: ")
                    if q:
                        load_tab(5, q)
                else:
                    load_tab(n)
            elif key == ord("/"):
                q = prompt_input(stdscr, "Search station: ")
                if q:
                    load_tab(5, q)
            elif key in (ord("c"), ord("C")):
                c = prompt_input(stdscr, "Country (or blank to browse all): ")
                load_tab(3, c or None)
            elif key in (ord("g"), ord("G")):
                g = prompt_input(stdscr, "Genre/tag (or blank to browse all): ")
                load_tab(4, g or None)
            elif key in (ord("a"), ord("A")):
                name = prompt_input(stdscr, "Station name: ")
                url = prompt_input(stdscr, "Stream URL: ") if name else ""
                if name and url:
                    favs.append({"name": name, "url": url, "country": "CUSTOM", "tags": "", "bitrate": "", "codec": ""})
                    save_favorites(favs)
                    msg = f"Added {name} to Favorites"
                    if tab_idx == 0:
                        stations = list(favs)
            elif key in (ord("f"), ord("F")) and view_list:
                st = view_list[idx]
                if st.get("_kind"):
                    continue
                u = st.get("url")
                if any(f.get("url") == u for f in favs):
                    favs = [f for f in favs if f.get("url") != u]
                    msg = f"Removed from Favorites: {st.get('name')}"
                else:
                    favs.append(st)
                    msg = f"Saved to Favorites: {st.get('name')}"
                save_favorites(favs)
                if tab_idx == 0:
                    stations = list(favs)
                    idx = min(idx, max(0, len(stations) - 1))
            elif key in (ord("+"), ord("=")):
                msg = adjust_volume(+5)
            elif key in (ord("-"), ord("_")):
                msg = adjust_volume(-5)
            elif key in (ord("s"), ord("S")):
                player.stop()
                msg = "Stopped"
            elif key in (curses.KEY_ENTER, 10, 13) and view_list:
                sel = view_list[idx]
                if sel.get("_kind") in ("country", "tag"):
                    load_tab(tab_idx, sel.get("value"))
                else:
                    player.play(sel)
                    remember_station(sel, recent)
                    if tab_idx == 6:
                        stations = list(recent)
                        idx = 0
                        scroll = 0
    finally:
        player.stop()


def main():
    # Make ESC snappy and let ncurses decode arrow/function keys reliably.
    os.environ.setdefault("ESCDELAY", "100")

    def _handler(signum, frame):
        raise Terminate(signum)

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except Exception:
            pass

    try:
        curses.wrapper(main_curses)
    except Terminate as e:
        # Signal received: the cleanup in main_curses' finally block has already
        # stopped playback, so the terminal can be restored and we are done.
        return 128 + e.signum
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
