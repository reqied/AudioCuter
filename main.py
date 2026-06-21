"""
Аудиорезак: два выреза на волне, прослушивание, тёмная и «дофаминовая» темы.
Нужны: FFmpeg (ffmpeg, ffprobe), Python-пакеты: pygame, Pillow (см. requirements.txt).
По желанию: загрузка по HTTPS или файл из Telegram Bot API (токен + file_id).
Горячие клавиши: пробел — пауза/продолжить, стрелки — ±1 с, F1/F2 — прослушать вырез 1/2 (не в полях ввода).
Перетаскивание файла в окно — Windows, пакет windnd (pip install windnd).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import array
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox

# --- Pillow: сглаженная волна (суперсэмплинг) ---
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
except ImportError as e:
    raise SystemExit("Установите Pillow: pip install Pillow") from e

# --- Воспроизведение ---
try:
    import pygame
except ImportError:
    pygame = None  # type: ignore[assignment]

try:
    import windnd  # type: ignore[import-untyped]
except ImportError:
    windnd = None  # type: ignore[assignment]


THEMES: dict[str, dict[str, str | tuple[int, ...]]] = {
    "dark": {
        "name": "Тёмная",
        "bg": "#0e0e12",
        "panel": "#16161e",
        "fg": "#ececf1",
        "muted": "#8b8b9a",
        "entry_bg": "#1e1e2a",
        "entry_fg": "#ececf1",
        "accent": "#7c9cff",
        "accent2": "#a78bfa",
        "btn_bg": "#252532",
        "btn_fg": "#ececf1",
        "wave_bg": "#0a0a10",
        "wave_fill": "#4a6bdc",
        "wave_line": "#6d8cff",
        "playhead": "#ffffff",
        "r1": "#5b7cfa",
        "r2": "#c084fc",
        "r1_border": "#9db4ff",
        "r2_border": "#e9d5ff",
        "hl": "#2a2a38",
    },
    "dopamine": {
        "name": "Дофамин",
        # Тёплый шоколад + неон: отсылка к «коричневой гамме» + яркие акценты
        "bg": "#1f1528",
        "panel": "#2d1f3a",
        "fg": "#fff5f0",
        "muted": "#d4b8c8",
        "entry_bg": "#3a2848",
        "entry_fg": "#fff8f3",
        "accent": "#ff6b9d",
        "accent2": "#4ecdc4",
        "btn_bg": "#ff8c42",
        "btn_fg": "#1a0a06",
        "wave_bg": "#24182f",
        "wave_fill": "#ff6b9d",
        "wave_line": "#ffe66d",
        "playhead": "#ffffff",
        "r1": "#ff6b9d",
        "r2": "#4ecdc4",
        "r1_border": "#ffd1e0",
        "r2_border": "#b8fff5",
        "hl": "#402a55",
    },
}


def parse_time_to_seconds(text: str) -> float | None:
    text = text.strip().replace(",", ".")
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        m, s = nums
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = nums
        return h * 3600 + m * 60 + s
    return None


def format_duration_sec(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    s, ms_rem = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}.{ms_rem:03d}".rstrip("0").rstrip(".")


def ffprobe_duration(path: Path, ffprobe: str) -> float | None:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        return float(out.strip())
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def ffmpeg_cut(
    ffmpeg: str,
    src: Path,
    out: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[bool, str]:
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-ss",
        f"{start_sec:.6f}",
        "-t",
        f"{duration_sec:.6f}",
        "-map",
        "0:a:0?",
        "-vn",
        str(out),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return False, str(e)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip() or f"код {p.returncode}"
        return False, err[:2000]
    return True, ""


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix_hex(a: str, b: str, t: float) -> str:
    """t=0 → a, t=1 → b"""
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    bl = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".oga", ".wma", ".webm"})


def _is_audio_path(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_SUFFIXES

MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
# Цвет подписи ошибки в диалогах (после темизации не затирается)
_ERR_LABEL_FG = "#f87171"


def _suffix_from_url(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        suf = Path(p.path).suffix.lower()
        if suf in AUDIO_SUFFIXES:
            return suf
    except Exception:
        pass
    return ".mp3"


def download_http_to_file(url: str, dest: Path) -> tuple[bool, str]:
    """Скачивает по http(s) в dest (потоково, лимит размера)."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False, "Нужна ссылка, начинающаяся с http:// или https://"
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AudioCuter/1.0; +https://ffmpeg.org/)",
            "Accept": "*/*",
        },
        method="GET",
    )
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        return False, f"Файл больше {MAX_DOWNLOAD_BYTES // (1024 * 1024)} МБ — загрузка прервана."
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, str(e.reason if hasattr(e, "reason") else e)
    except OSError as e:
        return False, str(e)
    if total == 0:
        return False, "Пустой ответ (0 байт)."
    return True, ""


def telegram_file_download_url(bot_token: str, file_id: str) -> tuple[str | None, str]:
    """Возвращает прямой https URL на файл в облаке Telegram или текст ошибки."""
    token = bot_token.strip()
    fid = file_id.strip()
    if not token or not fid:
        return None, "Укажите токен бота и file_id."
    q = urllib.parse.urlencode({"file_id": fid})
    api_url = f"https://api.telegram.org/bot{token}/getFile?{q}"
    req = urllib.request.Request(api_url, headers={"User-Agent": "AudioCuter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            body = ""
        return None, f"HTTP {e.code}: {body or e.reason}"
    except urllib.error.URLError as e:
        return None, str(e.reason if hasattr(e, "reason") else e)
    except OSError as e:
        return None, str(e)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, raw[:600]
    if not data.get("ok"):
        return None, str(data.get("description", data))
    result = data.get("result") or {}
    fp = result.get("file_path")
    if not fp or not isinstance(fp, str):
        return None, "В ответе getFile нет file_path."
    fp = fp.lstrip("/")
    return f"https://api.telegram.org/file/bot{token}/{fp}", ""


def _ffmpeg_run(cmd: list[str]) -> tuple[int, bytes, str]:
    """Запуск ffmpeg/ffprobe: без всплывающей консоли на Windows."""
    kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    p = subprocess.run(cmd, **kw)
    err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
    out = p.stdout or b""
    return p.returncode, out, err


def _peaks_from_s16mv(smv: memoryview | array.array, n: int, target_buckets: int) -> list[float]:
    nb = max(8, target_buckets)
    peaks = [0.0] * nb
    if n <= 0:
        return peaks
    for i in range(n):
        v = abs(float(smv[i]) / 32768.0)
        b = min(nb - 1, (i * nb) // n)
        if v > peaks[b]:
            peaks[b] = v
    m = max(peaks) or 1e-9
    return [p / m for p in peaks]


def _peaks_from_float_samples(samples: memoryview | array.array, n: int, target_buckets: int) -> list[float]:
    nb = max(8, target_buckets)
    peaks = [0.0] * nb
    if n <= 0:
        return peaks
    for i in range(n):
        v = float(samples[i])  # memoryview/array
        b = min(nb - 1, (i * nb) // n)
        av = abs(v)
        if av > peaks[b]:
            peaks[b] = av
    m = max(peaks) or 1e-9
    return [p / m for p in peaks]


def decode_mono_peaks(
    path: Path, ffmpeg: str, ffprobe: str, target_buckets: int = 8192
) -> tuple[list[float], str]:
    """Пиковая огибающая 0..1 и текст ошибки (пусто при успехе).

    Раньше использовался struct.unpack на миллионы float — строка формата превышала лимит и ломала разбор.
    """
    dur = ffprobe_duration(path, ffprobe)
    if dur is None or dur <= 0:
        return [], "не удалось получить длительность (ffprobe)."

    max_samples = 1_200_000
    ar = int(min(16000, max(800, max_samples / max(dur, 0.001))))

    def try_decode(fmt: str) -> tuple[list[float], str]:
        cmd = [
            ffmpeg,
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0?",
            "-ac",
            "1",
            "-ar",
            str(ar),
            "-f",
            fmt,
            "-",
        ]
        code, raw, err = _ffmpeg_run(cmd)
        if code != 0:
            return [], err or f"ffmpeg завершился с кодом {code}"
        if fmt == "f32le":
            nbytes = (len(raw) // 4) * 4
            if nbytes < 4:
                return [], "ffmpeg не вернул аудио (пустой поток)."
            try:
                fmv = memoryview(raw[:nbytes]).cast("f")
            except (TypeError, ValueError):
                buf = array.array("f")
                buf.frombytes(raw[:nbytes])
                fmv = buf
                nbytes = len(buf) * 4
            n = nbytes // 4
            peaks = _peaks_from_float_samples(fmv, n, target_buckets)
            return peaks, ""
        # s16le fallback
        nbytes = (len(raw) // 2) * 2
        if nbytes < 2:
            return [], err or "пустой s16le поток."
        try:
            smv = memoryview(raw[:nbytes]).cast("h")
        except (TypeError, ValueError):
            buf = array.array("h")
            buf.frombytes(raw[:nbytes])
            smv = buf
        n = len(smv)
        peaks = _peaks_from_s16mv(smv, n, target_buckets)
        return peaks, ""

    peaks, err = try_decode("f32le")
    if peaks:
        return peaks, ""
    peaks2, err2 = try_decode("s16le")
    if peaks2:
        return peaks2, ""
    detail = err or err2 or "неизвестная ошибка ffmpeg."
    return [], detail[:900]


def render_wave_pil(
    peaks: list[float],
    width: int,
    height: int,
    wave_bg: str,
    wave_fill: str,
    wave_line: str,
    *,
    layout_scale: float = 1.0,
    internal_super: int = 4,
) -> Image.Image:
    """Рисуем в повышенном разрешении (DPI × внутренний суперсэмпл), затем LANCZOS в логический размер канваса."""
    ls = max(1.0, min(3.0, float(layout_scale)))
    isup = max(2, int(internal_super))
    w = max(8, int(width * ls * isup))
    h = max(8, int(height * ls * isup))
    bg = _hex_to_rgb(wave_bg)
    fill = _hex_to_rgb(wave_fill)
    line = _hex_to_rgb(wave_line)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    if not peaks or w < 4:
        return img.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)

    n = len(peaks)
    mid = h // 2
    max_h = mid - max(8, h // 12)

    pts_top: list[tuple[float, float]] = []
    pts_bot: list[tuple[float, float]] = []
    for x in range(w):
        idx = min(n - 1, int(x * (n - 1) / max(w - 1, 1)))
        ph = peaks[idx] * max_h
        xf = x + 0.5
        pts_top.append((xf, mid - ph))
        pts_bot.append((xf, mid + ph))

    poly = pts_top + pts_bot[::-1]
    if len(poly) >= 3:
        draw.polygon(poly, fill=fill, outline=line)
    lw = max(1, int(round(isup * 0.35)))
    draw.line(pts_top, fill=line, width=lw)
    draw.line(pts_bot, fill=line, width=lw)
    # Лёгкое сглаживание на высоком разрешении, не «мыло» на итоговой картинке
    img = img.filter(ImageFilter.GaussianBlur(radius=0.22 * isup / 4.0))
    return img.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)


class AudioCutterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Аудиорезак — два выреза")
        self.minsize(640, 560)
        self.geometry("720x620")

        self._theme_id = tk.StringVar(value="dark")
        self._source_path: Path | None = None
        self._duration_sec: float | None = None
        self._peaks: list[float] = []
        self._wave_photo: ImageTk.PhotoImage | None = None
        self._wave_img_pil: Image.Image | None = None

        # (start_sec, end_sec) для вырезов 1 и 2
        self._r1 = (0.0, 1.0)
        self._r2 = (0.0, 1.0)

        self._drag: tuple[str, str] | None = None  # ('r1'|'r2', 'L'|'R'|'M')
        self._canvas_w = 1
        self._canvas_h = 160

        self._playing = False
        self._play_start_mono = 0.0
        self._music_loaded: Path | None = None
        self._source_is_temp = False
        self._preview_end_sec: float | None = None

        self._trace_guard = False
        self._build_ui()
        self._apply_theme()
        self._theme_id.trace_add("write", lambda *_: self._apply_theme())

        self._canvas.bind("<Configure>", self._on_wave_configure)
        self._canvas.bind("<ButtonPress-1>", self._on_wave_press)
        self._canvas.bind("<B1-Motion>", self._on_wave_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_wave_release)
        self._canvas.bind("<Double-Button-1>", self._on_wave_double)

        for v in (
            self._start1,
            self._end1,
            self._start2,
            self._end2,
        ):
            v.trace_add("write", self._on_time_entry_change)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._tick_playhead)

        self.bind_all("<KeyPress>", self._on_global_keypress, add="+")
        self._setup_drop_files()

    def _t(self) -> dict[str, str | tuple[int, ...]]:
        return THEMES[self._theme_id.get()]

    def _pixel_scale(self) -> float:
        """Масштаб физических пикселей к логическим (Windows HiDPI / Tk scaling)."""
        try:
            self.update_idletasks()
            wid = int(self.winfo_id())
            if wid and sys.platform == "win32":
                import ctypes

                dpi = ctypes.windll.user32.GetDpiForWindow(wid)
                return max(1.0, min(3.0, dpi / 96.0))
        except Exception:
            pass
        try:
            return max(1.0, min(3.0, float(self.tk.call("tk", "scaling"))))
        except tk.TclError:
            return 1.0

    def _ui_font_size(self, base: int) -> int:
        return max(8, int(round(base * self._pixel_scale())))

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}
        top = tk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", **pad)
        tk.Label(top, text="Тема:").pack(side=tk.LEFT, padx=(0, 8))
        for key, spec in THEMES.items():
            tk.Radiobutton(
                top,
                text=str(spec["name"]),
                variable=self._theme_id,
                value=key,
                indicatoron=0,
                width=12,
                borderwidth=0,
            ).pack(side=tk.LEFT, padx=3)

        row = 1
        bar = tk.Frame(self)
        bar.grid(row=row, column=0, sticky="ew", **pad)
        tk.Button(bar, text="Загрузить аудио…", command=self._load_audio).pack(side=tk.LEFT)
        tk.Button(bar, text="По ссылке…", command=self._dialog_load_url).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(bar, text="Из Telegram…", command=self._dialog_load_telegram).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(bar, text="▶ / ⏸", command=self._play_toggle, width=8).pack(side=tk.LEFT, padx=8)
        tk.Button(bar, text="▶ Вырез 1", command=lambda: self._preview_cut_n(1)).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(bar, text="▶ Вырез 2", command=lambda: self._preview_cut_n(2)).pack(side=tk.LEFT, padx=(4, 0))
        self._time_var = tk.StringVar(value="— / —")
        tk.Label(bar, textvariable=self._time_var).pack(side=tk.LEFT)
        row += 1

        self._path_var = tk.StringVar(value="Файл не выбран")
        tk.Label(self, textvariable=self._path_var, wraplength=680, justify=tk.LEFT).grid(
            row=row, column=0, sticky="ew", **pad
        )
        row += 1
        self._duration_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._duration_var).grid(row=row, column=0, sticky="w", **pad)
        row += 1

        wave_wrap = tk.Frame(self)
        wave_wrap.grid(row=row, column=0, sticky="nsew", **pad)
        self.rowconfigure(row, weight=1)
        self.columnconfigure(0, weight=1)
        wave_wrap.rowconfigure(0, weight=1)
        wave_wrap.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(wave_wrap, height=self._canvas_h, highlightthickness=0, bd=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas_bg = self._canvas.create_rectangle(0, 0, 10, 10, fill="#000", outline="")
        self._img_item = self._canvas.create_image(0, 0, anchor=tk.NW)
        self._rect1 = self._canvas.create_rectangle(0, 0, 0, 0, outline="", width=0)
        self._rect2 = self._canvas.create_rectangle(0, 0, 0, 0, outline="", width=0)
        self._play_line = self._canvas.create_line(0, 0, 0, 1, fill="#fff", width=2)
        self._hint_wave = tk.Label(
            self,
            text=(
                "На волне: тяните рамки (края — длина, середина — сдвиг). Двойной щелчок — перемотка. "
                "Пробел — пауза/играть, ←/→ — ±1 с, F1/F2 — прослушать вырез 1 или 2 (вне полей ввода). "
                "Можно перетащить аудиофайл в окно (Windows + windnd)."
            ),
            wraplength=680,
            justify=tk.LEFT,
        )
        row += 1
        self._hint_wave.grid(row=row, column=0, sticky="w", **pad)
        row += 1

        hint = "Время в полях синхронизируется с рамками. Имя файла — с расширением (.mp3, .wav, …)."
        self._hint2 = tk.Label(self, text=hint, wraplength=680, justify=tk.LEFT)
        self._hint2.grid(row=row, column=0, sticky="w", **pad)
        self._muted_hint_labels = frozenset({self._hint_wave, self._hint2})
        row += 1

        f1 = tk.LabelFrame(self, text="Вырез 1", padx=8, pady=8)
        f1.grid(row=row, column=0, sticky="ew", **pad)
        self._grid_cut_fields(f1, "start1", "end1", "name1", "_part1")
        row += 1

        f2 = tk.LabelFrame(self, text="Вырез 2", padx=8, pady=8)
        f2.grid(row=row, column=0, sticky="ew", **pad)
        self._grid_cut_fields(f2, "start2", "end2", "name2", "_part2")
        row += 1

        tk.Button(self, text="Сохранить оба выреза…", command=self._export_both).grid(
            row=row, column=0, sticky="w", **pad
        )

    def _grid_cut_fields(
        self, parent: tk.LabelFrame, start_key: str, end_key: str, name_key: str, default_suffix: str
    ) -> None:
        parent.columnconfigure(1, weight=1)
        tk.Label(parent, text="Начало").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        v_s = tk.StringVar()
        setattr(self, f"_{start_key}", v_s)
        tk.Entry(parent, textvariable=v_s, width=18).grid(row=0, column=1, sticky="ew", padx=4, pady=2)

        tk.Label(parent, text="Конец").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        v_e = tk.StringVar()
        setattr(self, f"_{end_key}", v_e)
        tk.Entry(parent, textvariable=v_e, width=18).grid(row=1, column=1, sticky="ew", padx=4, pady=2)

        tk.Label(parent, text="Имя файла").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        v_n = tk.StringVar()
        setattr(self, f"_{name_key}", v_n)
        v_n._default_suffix = default_suffix  # type: ignore[attr-defined]
        tk.Entry(parent, textvariable=v_n, width=40).grid(row=2, column=1, sticky="ew", padx=4, pady=2)

    def _apply_theme(self) -> None:
        th = self._t()
        bg = str(th["bg"])
        fg = str(th["fg"])
        panel = str(th["panel"])
        self.configure(bg=bg)

        for w in self.winfo_children():
            self._paint_widget(w, th)

        self._canvas.configure(bg=str(th["wave_bg"]))
        self._canvas.itemconfigure(self._canvas_bg, fill=str(th["wave_bg"]))
        self._canvas.itemconfigure(self._play_line, fill=str(th["playhead"]))
        self._redraw_wave_bitmap()
        self._draw_regions()

    def _paint_widget(self, w: tk.Misc, th: dict[str, str | tuple[int, ...]]) -> None:
        bg = str(th["bg"])
        fg = str(th["fg"])
        panel = str(th["panel"])
        muted = str(th["muted"])
        eb = str(th["entry_bg"])
        ef = str(th["entry_fg"])
        btn_bg = str(th["btn_bg"])
        btn_fg = str(th["btn_fg"])
        hl = str(th["hl"])

        c = w.winfo_class()
        if c == "Frame" or c == "LabelFrame":
            try:
                w.configure(bg=bg if w.master == self else panel)
                if c == "LabelFrame":
                    try:
                        w.configure(labelforeground=fg)
                    except tk.TclError:
                        pass
            except tk.TclError:
                pass
        elif c == "Label":
            try:
                parent_bg = w.master.cget("bg") if isinstance(w.master, (tk.Frame, tk.LabelFrame, tk.Tk)) else bg
                hints = getattr(self, "_muted_hint_labels", frozenset())
                lab_fg = muted if w in hints else fg
                w.configure(bg=parent_bg, fg=lab_fg, font=tkfont.Font(size=self._ui_font_size(10)))
            except tk.TclError:
                pass
        elif c == "Button":
            try:
                w.configure(
                    bg=btn_bg,
                    fg=btn_fg,
                    activebackground=_mix_hex(btn_bg, "#ffffff", 0.15),
                    activeforeground=btn_fg,
                    font=tkfont.Font(size=self._ui_font_size(10)),
                )
            except tk.TclError:
                pass
        elif c == "Radiobutton":
            try:
                fs = self._ui_font_size(10)
                w.configure(
                    bg=bg,
                    fg=fg,
                    selectcolor=hl,
                    activebackground=bg,
                    activeforeground=fg,
                    indicatoron=0,
                    borderwidth=0,
                    padx=8,
                    pady=4,
                    font=tkfont.Font(size=fs, weight="bold"),
                )
            except tk.TclError:
                pass
        elif c == "Entry":
            try:
                w.configure(
                    bg=eb,
                    fg=ef,
                    insertbackground=ef,
                    relief=tk.FLAT,
                    highlightthickness=1,
                    font=tkfont.Font(size=self._ui_font_size(10)),
                    highlightbackground=str(th["accent"]),
                    highlightcolor=str(th["accent"]),
                )
            except tk.TclError:
                pass

        for ch in w.winfo_children():
            self._paint_widget(ch, th)

    def _sec_to_x(self, sec: float) -> float:
        d = self._duration_sec or 1.0
        w = max(1, self._canvas_w - 1)
        sec = max(0.0, min(sec, d))
        return sec / d * w

    def _x_to_sec(self, x: float) -> float:
        d = self._duration_sec or 1.0
        w = max(1, self._canvas_w - 1)
        x = max(0.0, min(x, w))
        return x / w * d

    def _default_ranges(self, dur: float) -> tuple[tuple[float, float], tuple[float, float]]:
        a1 = dur * 0.05
        b1 = dur * 0.20
        a2 = dur * 0.28
        b2 = dur * 0.45
        b1 = max(b1, a1 + 0.25)
        b2 = max(b2, a2 + 0.25)
        return (a1, min(b1, dur)), (a2, min(b2, dur))

    def _set_ranges_to_vars(self) -> None:
        self._trace_guard = True
        try:
            self._start1.set(format_duration_sec(self._r1[0]))
            self._end1.set(format_duration_sec(self._r1[1]))
            self._start2.set(format_duration_sec(self._r2[0]))
            self._end2.set(format_duration_sec(self._r2[1]))
        finally:
            self._trace_guard = False

    def _on_time_entry_change(self, *_args: object) -> None:
        if self._trace_guard or self._duration_sec is None:
            return
        d = self._duration_sec
        s1 = parse_time_to_seconds(self._start1.get())
        e1 = parse_time_to_seconds(self._end1.get())
        s2 = parse_time_to_seconds(self._start2.get())
        e2 = parse_time_to_seconds(self._end2.get())
        if None in (s1, e1, s2, e2):
            return
        if e1 <= s1 or e2 <= s2:
            return
        s1, e1 = max(0, s1), min(e1, d)
        s2, e2 = max(0, s2), min(e2, d)
        if e1 <= s1 or e2 <= s2:
            return
        self._r1 = (s1, e1)
        self._r2 = (s2, e2)
        self._draw_regions()

    def _on_wave_configure(self, ev: tk.Event) -> None:
        if ev.width < 2:
            return
        self._canvas_w = ev.width
        self._canvas_h = max(120, ev.height)
        self._canvas.coords(self._canvas_bg, 0, 0, ev.width, ev.height)
        self._redraw_wave_bitmap()
        self._draw_regions()

    def _redraw_wave_bitmap(self) -> None:
        th = self._t()
        w, h = max(2, self._canvas_w), max(2, self._canvas_h)
        if self._peaks:
            dpr = self._pixel_scale()
            pil = render_wave_pil(
                self._peaks,
                w,
                h,
                str(th["wave_bg"]),
                str(th["wave_fill"]),
                str(th["wave_line"]),
                layout_scale=dpr,
                internal_super=4,
            )
            self._wave_img_pil = pil
            self._wave_photo = ImageTk.PhotoImage(pil)
            self._canvas.itemconfigure(self._img_item, image=self._wave_photo)
        else:
            self._wave_photo = None
            self._canvas.itemconfigure(self._img_item, image="")
            self._canvas.configure(bg=str(th["wave_bg"]))
        self._canvas.coords(self._img_item, 0, 0)

    def _hit_test(self, x: float) -> tuple[str, str] | None:
        slop = 10
        # Сверху вырез 2 — чтобы при перекрытии ловить верхний
        for rid, rr in (("r2", self._r2), ("r1", self._r1)):
            x0, x1 = self._sec_to_x(rr[0]), self._sec_to_x(rr[1])
            if x0 > x1:
                x0, x1 = x1, x0
            mid = (x0 + x1) / 2
            if abs(x - x0) <= slop:
                return rid, "L"
            if abs(x - x1) <= slop:
                return rid, "R"
            if x0 + slop < x < x1 - slop:
                return rid, "M"
        return None

    def _on_wave_press(self, ev: tk.Event) -> None:
        self._drag = self._hit_test(ev.x)
        if self._drag and self._duration_sec:
            rid, part = self._drag
            r = self._r1 if rid == "r1" else self._r2
            a, b = r
            if part == "M":
                self._move_anchor = (a, b, self._x_to_sec(ev.x))

    def _on_wave_drag(self, ev: tk.Event) -> None:
        if self._drag is None or self._duration_sec is None:
            return
        rid, part = self._drag
        r = self._r1 if rid == "r1" else self._r2
        a, b = r
        t = self._x_to_sec(ev.x)
        d = self._duration_sec
        min_len = 0.12

        if part == "L":
            na = max(0.0, min(t, b - min_len))
            nb = b
        elif part == "R":
            na = a
            nb = min(d, max(t, a + min_len))
        elif part == "M" and hasattr(self, "_move_anchor"):
            aa, bb, t0 = self._move_anchor
            dt = t - t0
            width = bb - aa
            na = aa + dt
            nb = bb + dt
            if na < 0:
                na, nb = 0.0, width
            if nb > d:
                na, nb = d - width, d
        else:
            return

        if rid == "r1":
            self._r1 = (na, nb)
        else:
            self._r2 = (na, nb)
        self._set_ranges_to_vars()
        self._draw_regions()

    def _on_wave_release(self, _ev: tk.Event) -> None:
        self._drag = None
        if hasattr(self, "_move_anchor"):
            del self._move_anchor

    def _on_wave_double(self, ev: tk.Event) -> None:
        if self._duration_sec is None or not self._source_path:
            return
        t = self._x_to_sec(ev.x)
        self._seek_play(t)

    def _draw_regions(self) -> None:
        th = self._t()
        h = max(2, self._canvas_h)
        wbg = str(th["wave_bg"])
        # Сильнее к фону волны = визуально прозрачнее
        fill_mix = 0.18
        c1 = _mix_hex(wbg, str(th["r1"]), fill_mix)
        c2 = _mix_hex(wbg, str(th["r2"]), fill_mix)
        b1 = _mix_hex(wbg, str(th["r1_border"]), 0.42)
        b2 = _mix_hex(wbg, str(th["r2_border"]), 0.42)

        x10, x11 = self._sec_to_x(self._r1[0]), self._sec_to_x(self._r1[1])
        x20, x21 = self._sec_to_x(self._r2[0]), self._sec_to_x(self._r2[1])
        if x10 > x11:
            x10, x11 = x11, x10
        if x20 > x21:
            x20, x21 = x21, x20

        self._canvas.coords(self._rect1, x10, 0, x11, h)
        self._canvas.itemconfigure(self._rect1, fill=c1, outline=b1, width=1)

        self._canvas.coords(self._rect2, x20, 0, x21, h)
        self._canvas.itemconfigure(self._rect2, fill=c2, outline=b2, width=1)

        self._canvas.tag_raise(self._rect1)
        self._canvas.tag_raise(self._rect2)
        self._canvas.tag_raise(self._play_line)

    def _tick_playhead(self) -> None:
        try:
            if pygame and pygame.mixer.get_init():
                busy = pygame.mixer.music.get_busy()
                if self._playing and not busy:
                    self._playing = False
                if self._duration_sec and self._playing and busy:
                    pos = pygame.mixer.music.get_pos() / 1000.0
                    cur = self._play_start_mono + pos
                    d = self._duration_sec
                    pend = self._preview_end_sec
                    if pend is not None and cur >= pend - 0.06:
                        pygame.mixer.music.stop()
                        self._playing = False
                        self._play_start_mono = min(pend, d)
                        self._preview_end_sec = None
                        x = self._sec_to_x(self._play_start_mono)
                        self._canvas.coords(self._play_line, x, 0, x, self._canvas_h)
                        self._time_var.set(f"{format_duration_sec(self._play_start_mono)} / {format_duration_sec(d)}")
                    else:
                        x = self._sec_to_x(min(max(0.0, cur), d))
                        self._canvas.coords(self._play_line, x, 0, x, self._canvas_h)
                        self._time_var.set(f"{format_duration_sec(cur)} / {format_duration_sec(d)}")
                elif self._duration_sec:
                    d = self._duration_sec
                    self._time_var.set(f"— / {format_duration_sec(d)}")
        except Exception:
            pass
        self.after(60, self._tick_playhead)

    def _ensure_pygame(self) -> bool:
        if pygame is None:
            messagebox.showerror("Воспроизведение", "Установите pygame: pip install pygame")
            return False
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
        return True

    def _play_toggle(self) -> None:
        if not self._source_path:
            messagebox.showinfo("Аудио", "Сначала загрузите файл.")
            return
        if not self._ensure_pygame():
            return
        assert pygame is not None
        if self._music_loaded != self._source_path:
            pygame.mixer.music.load(str(self._source_path))
            self._music_loaded = self._source_path

        busy = pygame.mixer.music.get_busy()
        if self._playing and busy:
            pygame.mixer.music.pause()
            self._playing = False
            return
        if not self._playing and busy:
            pygame.mixer.music.unpause()
            self._playing = True
            return

        self._preview_end_sec = None
        pygame.mixer.music.play(start=self._play_start_mono)
        self._playing = True

    def _seek_play(self, sec: float) -> None:
        if not self._source_path or not self._ensure_pygame():
            return
        assert pygame is not None
        d = self._duration_sec or 0.0
        sec = max(0.0, min(sec, d))
        self._preview_end_sec = None
        self._play_start_mono = sec
        pygame.mixer.music.load(str(self._source_path))
        pygame.mixer.music.play(start=sec)
        self._music_loaded = self._source_path
        self._playing = True
        self._canvas.coords(self._play_line, self._sec_to_x(sec), 0, self._sec_to_x(sec), self._canvas_h)

    def _current_transport_sec(self) -> float:
        d = self._duration_sec or 0.0
        if not pygame or not pygame.mixer.get_init():
            return max(0.0, min(self._play_start_mono, d))
        try:
            if pygame.mixer.music.get_busy():
                cur = self._play_start_mono + pygame.mixer.music.get_pos() / 1000.0
                return max(0.0, min(cur, d))
        except Exception:
            pass
        return max(0.0, min(self._play_start_mono, d))

    def _seek_relative(self, delta: float) -> None:
        if not self._source_path or self._duration_sec is None:
            return
        if not self._ensure_pygame():
            return
        cur = self._current_transport_sec()
        self._seek_play(cur + delta)

    def _preview_cut_n(self, n: int) -> None:
        if not self._source_path:
            messagebox.showinfo("Аудио", "Сначала загрузите файл.")
            return
        if self._duration_sec is None:
            return
        r = self._r1 if n == 1 else self._r2
        a, b = float(r[0]), float(r[1])
        d = self._duration_sec
        b = min(b, d)
        if b - a < 0.08:
            messagebox.showwarning("Вырез", "Слишком короткий фрагмент для прослушивания.")
            return
        self._play_preview_region(a, b)

    def _play_preview_region(self, start_sec: float, end_sec: float) -> None:
        if not self._source_path or not self._ensure_pygame():
            return
        assert pygame is not None
        if self._music_loaded != self._source_path:
            pygame.mixer.music.load(str(self._source_path))
            self._music_loaded = self._source_path
        self._preview_end_sec = end_sec
        self._play_start_mono = start_sec
        pygame.mixer.music.play(start=start_sec)
        self._playing = True

    def _hotkey_ok(self, ev: tk.Event) -> bool:
        try:
            w = ev.widget
            top = w.winfo_toplevel()
        except tk.TclError:
            return False
        if top is not self:
            return False
        if isinstance(w, tk.Entry):
            return False
        return True

    def _on_global_keypress(self, ev: tk.Event) -> str | None:
        if not self._hotkey_ok(ev):
            return None
        ks = ev.keysym
        if ks == "space":
            self._play_toggle()
            return "break"
        if ks == "Left":
            self._seek_relative(-1.0)
            return "break"
        if ks == "Right":
            self._seek_relative(1.0)
            return "break"
        if ks == "F1":
            self._preview_cut_n(1)
            return "break"
        if ks == "F2":
            self._preview_cut_n(2)
            return "break"
        return None

    def _setup_drop_files(self) -> None:
        if windnd is None or sys.platform != "win32":
            return

        def on_drop(files: list[str]) -> None:
            if not files:
                return
            chosen: Path | None = None
            for raw in files:
                p = Path(raw)
                if p.is_file() and _is_audio_path(p):
                    chosen = p
                    break
            if chosen is None:
                for raw in files:
                    p = Path(raw)
                    if p.is_file():
                        chosen = p
                        break
            if chosen is None:
                return
            fp = chosen
            self.after(10, lambda: self._load_from_path(fp, is_temp=False))

        try:
            windnd.hook_dropfiles(self, func=on_drop)  # type: ignore[union-attr]
        except Exception:
            pass

    def _ffmpeg_pair(self) -> tuple[str, str] | None:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            messagebox.showerror(
                "FFmpeg",
                "Не найдены ffmpeg/ffprobe в PATH.\nhttps://ffmpeg.org/download.html",
            )
            return None
        return ffmpeg, ffprobe

    def _cleanup_temp_source(self) -> None:
        if self._source_is_temp and self._source_path and self._source_path.is_file():
            try:
                self._source_path.unlink()
            except OSError:
                pass
        self._source_is_temp = False

    def _load_from_path(self, p: Path, *, display_label: str | None = None, is_temp: bool = False) -> bool:
        pair = self._ffmpeg_pair()
        if not pair:
            return False
        ffmpeg, ffprobe = pair

        dur = ffprobe_duration(p, ffprobe)
        if dur is None:
            messagebox.showerror("Ошибка", "Не удалось прочитать длительность (ffprobe).")
            return False

        peaks, wave_err = decode_mono_peaks(p, ffmpeg, ffprobe, target_buckets=8192)
        if not peaks:
            messagebox.showerror(
                "Волна",
                "Не удалось построить огибающую.\n\n"
                + (wave_err or "Проверьте, что в файле есть звук и ffmpeg из полной сборки."),
            )
            return False

        if is_temp or self._source_is_temp:
            self._cleanup_temp_source()

        self._source_path = p
        self._source_is_temp = is_temp
        self._duration_sec = dur
        self._peaks = peaks
        self._preview_end_sec = None
        self._music_loaded = None
        self._playing = False
        self._play_start_mono = 0.0
        if pygame and pygame.mixer.get_init():
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

        self._r1, self._r2 = self._default_ranges(dur)
        self._set_ranges_to_vars()

        self._path_var.set(display_label if display_label else str(p.resolve()))
        self._duration_var.set(f"Длительность: {format_duration_sec(dur)} ({dur:.2f} с)")

        stem = p.stem
        ext = p.suffix or ".wav"
        self._name1.set(f"{stem}{self._name1._default_suffix}{ext}")  # type: ignore[attr-defined]
        self._name2.set(f"{stem}{self._name2._default_suffix}{ext}")  # type: ignore[attr-defined]

        self._redraw_wave_bitmap()
        self._draw_regions()
        return True

    def _load_audio(self) -> None:
        if not self._ffmpeg_pair():
            return
        path = filedialog.askopenfilename(
            title="Выберите аудиофайл",
            filetypes=[
                ("Аудио", "*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return
        self._load_from_path(Path(path), is_temp=False)

    def _temp_download_dir(self) -> Path:
        d = Path(tempfile.gettempdir()) / "audioCuter_dl"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _style_dialog(self, win: tk.Toplevel) -> None:
        win.configure(bg=self._t()["bg"])
        for ch in win.winfo_children():
            self._paint_widget(ch, self._t())

    def _dialog_load_url(self) -> None:
        if not self._ffmpeg_pair():
            return
        tw = tk.Toplevel(self)
        tw.title("Загрузка по ссылке")
        tw.transient(self)
        tw.grab_set()
        th = self._t()
        tw.configure(bg=str(th["bg"]))
        tk.Label(
            tw,
            text="Прямая ссылка на аудиофайл (https://…). Подойдут CDN, api.telegram.org/file/… и др.",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", padx=12, pady=(10, 4))
        url_var = tk.StringVar()
        ent = tk.Entry(tw, textvariable=url_var, width=72)
        ent.pack(fill=tk.X, padx=12, pady=6)
        err_l = tk.Label(tw, text="", wraplength=520, justify=tk.LEFT, fg=_ERR_LABEL_FG)
        err_l.pack(anchor="w", padx=12, pady=4)

        def ok() -> None:
            u = url_var.get().strip()
            if not u:
                err_l.configure(text="Вставьте ссылку.")
                return
            suf = _suffix_from_url(u)
            dest = self._temp_download_dir() / f"download_{os.getpid()}{suf}"
            err_l.configure(text="Скачивание…")
            tw.update_idletasks()
            ok_dl, err = download_http_to_file(u, dest)
            if not ok_dl:
                err_l.configure(text=err)
                return
            short = u if len(u) < 72 else u[:35] + "…" + u[-30:]
            if self._load_from_path(dest, display_label=f"(ссылка) {short}", is_temp=True):
                tw.destroy()
            else:
                try:
                    if dest.is_file():
                        dest.unlink()
                except OSError:
                    pass

        bf = tk.Frame(tw)
        bf.pack(pady=10)
        tk.Button(bf, text="Скачать и открыть", command=ok).pack(side=tk.LEFT, padx=6)
        tk.Button(bf, text="Отмена", command=tw.destroy).pack(side=tk.LEFT, padx=6)
        self._style_dialog(tw)
        err_l.configure(fg=_ERR_LABEL_FG)
        ent.focus_set()

    def _dialog_load_telegram(self) -> None:
        if not self._ffmpeg_pair():
            return
        tw = tk.Toplevel(self)
        tw.title("Файл из Telegram")
        tw.transient(self)
        tw.grab_set()
        th = self._t()
        tw.configure(bg=str(th["bg"]))
        tk.Label(
            tw,
            text=(
                "Нужны токен бота (@BotFather) и file_id файла (voice/document/audio). "
                "Бот должен иметь доступ к файлу. Токен никуда не сохраняется."
            ),
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", padx=12, pady=(10, 4))
        tk.Label(tw, text="Токен бота").pack(anchor="w", padx=12)
        token_var = tk.StringVar()
        tk.Entry(tw, textvariable=token_var, width=64, show="•").pack(fill=tk.X, padx=12, pady=2)
        tk.Label(tw, text="file_id").pack(anchor="w", padx=12)
        fid_var = tk.StringVar()
        tk.Entry(tw, textvariable=fid_var, width=64).pack(fill=tk.X, padx=12, pady=2)
        err_l = tk.Label(tw, text="", wraplength=520, justify=tk.LEFT, fg=_ERR_LABEL_FG)
        err_l.pack(anchor="w", padx=12, pady=4)

        def ok() -> None:
            dl_url, err = telegram_file_download_url(token_var.get(), fid_var.get())
            if not dl_url:
                err_l.configure(text=err)
                return
            suf = _suffix_from_url(dl_url)
            dest = self._temp_download_dir() / f"tg_{os.getpid()}{suf}"
            err_l.configure(text="Скачивание из Telegram…")
            tw.update_idletasks()
            ok_dl, err2 = download_http_to_file(dl_url, dest)
            if not ok_dl:
                err_l.configure(text=err2)
                return
            fid = fid_var.get().strip()
            label = f"(Telegram) {fid[:24]}…" if len(fid) > 24 else f"(Telegram) {fid}"
            if self._load_from_path(dest, display_label=label, is_temp=True):
                tw.destroy()
            else:
                try:
                    if dest.is_file():
                        dest.unlink()
                except OSError:
                    pass

        bf = tk.Frame(tw)
        bf.pack(pady=10)
        tk.Button(bf, text="Скачать и открыть", command=ok).pack(side=tk.LEFT, padx=6)
        tk.Button(bf, text="Отмена", command=tw.destroy).pack(side=tk.LEFT, padx=6)
        self._style_dialog(tw)
        err_l.configure(fg=_ERR_LABEL_FG)

    def _parse_range(
        self, start_var: tk.StringVar, end_var: tk.StringVar, label: str
    ) -> tuple[float, float] | None:
        s_raw = start_var.get().strip()
        e_raw = end_var.get().strip()
        if not s_raw or not e_raw:
            messagebox.showwarning("Время", f"{label}: укажите начало и конец.")
            return None
        s_sec = parse_time_to_seconds(s_raw)
        e_sec = parse_time_to_seconds(e_raw)
        if s_sec is None or e_sec is None:
            messagebox.showwarning("Время", f"{label}: примеры: 10, 1:30, 0:01:05.")
            return None
        if s_sec < 0 or e_sec < 0:
            messagebox.showwarning("Время", f"{label}: время не может быть отрицательным.")
            return None
        if e_sec <= s_sec:
            messagebox.showwarning("Время", f"{label}: конец должен быть больше начала.")
            return None
        return s_sec, e_sec

    def _export_both(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            messagebox.showerror("FFmpeg", "ffmpeg не найден в PATH.")
            return
        if self._source_path is None or self._duration_sec is None:
            messagebox.showinfo("Файл", "Сначала загрузите аудио.")
            return

        r1 = self._parse_range(self._start1, self._end1, "Вырез 1")
        if r1 is None:
            return
        r2 = self._parse_range(self._start2, self._end2, "Вырез 2")
        if r2 is None:
            return

        total = self._duration_sec
        for label, (a, b) in (("Вырез 1", r1), ("Вырез 2", r2)):
            if b > total + 0.05:
                messagebox.showwarning(
                    "Длина",
                    f"{label}: конец ({format_duration_sec(b)}) длиннее файла ({format_duration_sec(total)}).",
                )
                return

        folder = filedialog.askdirectory(title="Папка для сохранения двух файлов")
        if not folder:
            return
        out_dir = Path(folder)

        specs = [
            (r1, self._name1.get().strip(), "Вырез 1"),
            (r2, self._name2.get().strip(), "Вырез 2"),
        ]
        paths_written: list[Path] = []
        for (s_sec, e_sec), name, label in specs:
            if not name:
                messagebox.showwarning("Имя", f"{label}: укажите имя файла.")
                return
            out_path = out_dir / name
            if out_path.suffix == "":
                out_path = out_path.with_suffix(self._source_path.suffix or ".wav")
            duration = e_sec - s_sec
            ok, err = ffmpeg_cut(ffmpeg, self._source_path, out_path, s_sec, duration)
            if not ok:
                messagebox.showerror("Ошибка экспорта", f"{label} → {out_path.name}:\n{err}")
                return
            paths_written.append(out_path)

        messagebox.showinfo("Готово", "Сохранено:\n" + "\n".join(str(p) for p in paths_written))

    def _on_close(self) -> None:
        self._cleanup_temp_source()
        if pygame and pygame.mixer.get_init():
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        self.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    app = AudioCutterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
