#!/usr/bin/env python3
"""
udemy_archivist.py — Udemy course downloader, CLI edition.

Dependencies
------------
  pip install requests
  ffmpeg  (system package — https://ffmpeg.org/download.html)

Quick start
-----------
  python udemy_archivist.py                        # interactive wizard
  python udemy_archivist.py --token YOUR_TOKEN     # save token and launch wizard
  python udemy_archivist.py --list                 # print subscribed courses
  python udemy_archivist.py --course 1234567       # download a specific course
  python udemy_archivist.py --course 1234567 \\
      --quality 720 --no-subs --no-assets   # non-interactive, minimal output

Getting your token
------------------
  1. Log in to udemy.com in your browser.
  2. Open DevTools → Application → Cookies → udemy.com.
  3. Copy the value of the "access_token" cookie.
  4. Pass it with --token, or paste it when prompted.
  The token is stored in settings.ini next to this script.

Output layout
-------------
  downloads/
    <course-title>-<course-id>/
      01 - <section-title>/
        001 - <lecture-title>.mp4
        001 - <lecture-title>.es_LA.vtt   # subtitle (if any)
        001 - <lecture-title> - slide.pdf # supplementary asset (if any)
      02 - …
"""

from __future__ import annotations

import argparse
import configparser
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests

# ──────────────────────────────────────────────────────────────────────────────
#  Global constants
# ──────────────────────────────────────────────────────────────────────────────

#: Default Udemy API base — override via settings.ini for Business subdomains.
API_BASE = "https://www.udemy.com"

#: Chrome UA that Udemy's CDN accepts without extra challenges.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

#: Path to the INI config file (created automatically on first run).
SETTINGS_FILE = Path("settings.ini")

#: Root folder for all downloaded courses.
DOWNLOADS_DIR = Path("downloads")

#: Browser-like security headers sent with every API request.
#: Mirrors get_browser_headers() in RequestHandler.cpp.
BROWSER_HEADERS: dict[str, str] = {
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
}

# ──────────────────────────────────────────────────────────────────────────────
#  Terminal colour helpers  (zero external deps)
# ──────────────────────────────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_GREEN = "\033[32m"
_YELLOW= "\033[33m"
_CYAN  = "\033[36m"
_RED   = "\033[31m"


def _col(text: object, *codes: str) -> str:
    """Wrap *text* in ANSI escape *codes*, then reset."""
    return "".join(codes) + str(text) + _RESET


def _ok(msg: str)   -> None: print(_col("  ✓ ", _GREEN, _BOLD) + msg)
def _err(msg: str)  -> None: print(_col("  ✗ ", _RED,   _BOLD) + msg)
def _info(msg: str) -> None: print(_col("  → ", _CYAN)  + msg)
def _warn(msg: str) -> None: print(_col("  ! ", _YELLOW) + msg)
def _head(msg: str) -> None: print(f"\n{_col(msg, _BOLD, _CYAN)}")


# ──────────────────────────────────────────────────────────────────────────────
#  Path / string helpers  (mirrors Helper.h)
# ──────────────────────────────────────────────────────────────────────────────

def slugify(s: str) -> str:
    """
    Strip filesystem-unsafe characters from *s*.

    Replaces ``< > : " / \\ | ? *`` with a single dash and trims leading /
    trailing dashes and spaces.  Empty result falls back to ``"item"``.

    Mirrors ``Helper::slugify()`` in Helper.h.
    """
    UNSAFE = set('<>:"/\\|?*')
    out: list[str] = []
    last_was_dash = False
    for ch in s:
        if ch in UNSAFE:
            if not last_was_dash:
                out.append("-")
                last_was_dash = True
        elif ord(ch) < 32 or ord(ch) == 127:
            # strip control characters
            continue
        else:
            out.append(ch)
            last_was_dash = False
    result = "".join(out).strip("- ")
    return result or "item"


def _zpad(n: int, width: int = 2) -> str:
    """Zero-pad integer *n* to *width* digits."""
    return str(n).zfill(width)


def course_dir(course_id: int, title: str) -> Path:
    """
    Return the local download directory for a course.

    Format: ``downloads/<safe-title-50chars>-<course_id>``
    Mirrors ``Helper::course_dir()``.
    """
    safe = slugify(title)[:50]
    return DOWNLOADS_DIR / f"{safe}-{course_id}"


def section_dir(idx: int, title: str) -> Path:
    """
    Return the relative sub-directory name for a course section.

    Format: ``<zero-padded-index> - <safe-title>``
    Mirrors ``Helper::section_dir()``.
    """
    safe = slugify(title)[:40]
    return Path(f"{_zpad(idx)} - {safe}")


def extract_quality_value(label: str) -> int:
    """
    Parse the numeric quality (height in pixels) from a label such as
    ``"720p"``, ``"1080"``, or ``"Auto"``.  Returns 0 if none found.

    Mirrors ``Helper::extract_quality_value()``.
    """
    digits = "".join(ch for ch in label if ch.isdigit())
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _fmt_bytes(b: float) -> str:
    """Human-readable byte count: ``"12.3 MB"``."""
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _fmt_speed(bps: float) -> str:
    """Human-readable transfer speed: ``"5.2 MB/s"``."""
    return _fmt_bytes(bps) + "/s"


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    """Render an ASCII progress bar, e.g. ``[████████░░░░] 62.3%``."""
    if total <= 0:
        filled = 0
        pct    = "?%"
    else:
        filled = int(width * done / total)
        pct    = f"{100 * done / total:.1f}%"
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}"


# ──────────────────────────────────────────────────────────────────────────────
#  Settings  (mirrors load_settings / handleSettingsUpdate in RequestHandler.cpp)
# ──────────────────────────────────────────────────────────────────────────────

class Settings:
    """
    Persistent configuration backed by ``settings.ini``.

    All fields map 1-to-1 with the keys recognised by the C++ ``load_settings``
    function, so the same ``settings.ini`` works with both tools.
    """

    def __init__(self) -> None:
        self.token:              str  = ""       # udemy_access_token
        self.client_id:          str  = ""       # udemy_client_id (optional)
        self.api_base:           str  = API_BASE # udemy_api_base
        self.proxy:              str  = ""       # http_proxy (optional)
        self.download_subtitles: bool = True     # download_subtitles
        self.download_assets:    bool = True     # download_assets

    # ------------------------------------------------------------------
    def load(self) -> None:
        """Read settings from ``settings.ini``.  Creates defaults if missing."""
        if not SETTINGS_FILE.exists():
            self._write_defaults()
            return

        cp = configparser.RawConfigParser()
        # configparser requires a [section] header; fake one
        cp.read_string("[main]\n" + SETTINGS_FILE.read_text(encoding="utf-8"))
        get = lambda k, d="": cp.get("main", k, fallback=d)

        self.token     = get("udemy_access_token") or get("access_token")
        self.client_id = get("udemy_client_id")    or get("client_id")
        self.api_base  = get("udemy_api_base") or get("api_base") or API_BASE
        self.proxy     = get("http_proxy") or get("proxy")

        def _bool(key: str, default: bool) -> bool:
            val = get(key, "true" if default else "false").lower()
            return val in ("1", "true", "yes", "on")

        self.download_subtitles = _bool("download_subtitles", True)
        self.download_assets    = _bool("download_assets",    True)

    # ------------------------------------------------------------------
    def save(self) -> None:
        """Persist current settings to ``settings.ini``."""
        lines = [
            "# udemy_archivist settings — generated automatically\n",
            f"udemy_access_token={self.token}\n",
            f"udemy_api_base={self.api_base}\n",
        ]
        if self.proxy:
            lines.append(f"http_proxy={self.proxy}\n")
        lines.append(f"download_subtitles={'true' if self.download_subtitles else 'false'}\n")
        lines.append(f"download_assets={'true' if self.download_assets else 'false'}\n")
        SETTINGS_FILE.write_text("".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------
    def _write_defaults(self) -> None:
        SETTINGS_FILE.write_text(
            "# udemy_archivist settings\n"
            "udemy_access_token=\n"
            "udemy_api_base=https://www.udemy.com\n"
            "# http_proxy=http://127.0.0.1:8888\n"
            "download_subtitles=true\n"
            "download_assets=true\n",
            encoding="utf-8",
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Udemy REST API client  (mirrors udemy_get + handleX in RequestHandler.cpp)
# ──────────────────────────────────────────────────────────────────────────────

class UdemyClient:
    """
    Thin wrapper around ``requests.Session`` for Udemy's private API.

    Every request is authenticated with a Bearer token and a matching Cookie
    header, plus the browser-spoofing headers from ``BROWSER_HEADERS``.
    Mirrors the header construction in ``udemy_get()`` and
    ``append_auth_headers_for_url()`` in RequestHandler.cpp.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, **BROWSER_HEADERS})

    # ------------------------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        """Build ``Authorization`` + ``Cookie`` headers for API requests."""
        cookie = f"access_token={self._s.token}"
        if self._s.client_id:
            cookie += f"; client_id={self._s.client_id}"
        return {
            "Authorization": f"Bearer {self._s.token}",
            "Referer": "https://www.udemy.com/",
            "Origin":  "https://www.udemy.com",
            "Cookie":  cookie,
        }

    def _proxies(self) -> dict[str, str]:
        if self._s.proxy:
            return {"http": self._s.proxy, "https": self._s.proxy}
        return {}

    def _verify_ssl(self) -> bool:
        # When routing through a local proxy (e.g. mitmproxy) SSL verification
        # will fail because the proxy re-signs certificates.
        return not bool(self._s.proxy)

    # ------------------------------------------------------------------
    def get(self, url: str, timeout: int = 20) -> dict:
        """
        Authenticated GET that returns parsed JSON.

        Raises ``RuntimeError`` if no token is configured, or re-raises
        ``requests.HTTPError`` on a non-2xx response.

        Mirrors ``udemy_get()`` in RequestHandler.cpp.
        """
        if not self._s.token:
            raise RuntimeError("No access token configured. Run with --token.")
        resp = self._session.get(
            url,
            headers=self._auth_headers(),
            proxies=self._proxies(),
            timeout=timeout,
            verify=self._verify_ssl(),
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    def whoami(self) -> dict:
        """``GET /api-2.0/users/me`` — verify token and fetch user info."""
        return self.get(f"{self._s.api_base}/api-2.0/users/me/?fields[user]=@default")

    def courses(self, page: int = 1, page_size: int = 20,
                query: str = "") -> dict:
        """
        ``GET /api-2.0/users/me/subscribed-courses`` — paginated course list.

        Mirrors ``handleCourses()`` in RequestHandler.cpp.

        Parameters
        ----------
        page:       1-based page number.
        page_size:  Results per page (max 100).
        query:      Optional search string; omit for recency ordering.
        """
        url = (
            f"{self._s.api_base}/api-2.0/users/me/subscribed-courses/"
            f"?page={page}&page_size={page_size}"
            "&fields[course]=@min,title,headline,url,image_480x270,visible_instructors"
        )
        url += f"&search={urllib.parse.quote(query)}" if query else "&ordering=-last_accessed"
        return self.get(url)

    def curriculum(self, course_id: int, page: int = 1,
                   page_size: int = 200) -> dict:
        """
        ``GET /api-2.0/courses/{id}/subscriber-curriculum-items`` — full
        chapter + lecture + asset tree for a course page.

        Mirrors ``handleLectures()`` in RequestHandler.cpp.
        """
        url = (
            f"{self._s.api_base}/api-2.0/courses/{course_id}"
            f"/subscriber-curriculum-items/?page={page}&page_size={page_size}"
            "&fields[lecture]=asset,title,object_index,asset_type,"
            "supplementary_assets,download_url"
            "&fields[asset]=stream_urls,download_urls,download_url,"
            "filename,asset_type,hls_url,media_sources"
            "&fields[chapter]=title,object_index"
            "&fields[supplementary_asset]=id,title,asset_type,"
            "download_urls,download_url,external_url,filename"
        )
        return self.get(url, timeout=30)

    def lecture_detail(self, course_id: int, lecture_id: int) -> dict:
        """
        ``GET /api-2.0/users/me/subscribed-courses/{cid}/lectures/{lid}`` —
        richer stream / caption data than the curriculum endpoint.

        Mirrors the API call inside ``resolve_lecture_stream()`` in
        RequestHandler.cpp.
        """
        url = (
            f"{self._s.api_base}/api-2.0/users/me/subscribed-courses/"
            f"{course_id}/lectures/{lecture_id}"
            "?fields[asset]=stream_urls,download_urls,download_url,"
            "captions,title,filename,hls_url,media_sources,asset_type,"
            "length,media_license_token,course_is_drmed"
            "&fields[lecture]=asset,supplementary_assets,description,"
            "download_url,is_free,last_watched_second"
        )
        return self.get(url, timeout=20)

    def asset_detail(self, asset_id: int) -> dict:
        """
        ``GET /api-2.0/assets/{id}`` — download URL for a supplementary file.

        Mirrors ``resolve_supplementary_asset()`` in RequestHandler.cpp.
        """
        url = (
            f"{self._s.api_base}/api-2.0/assets/{asset_id}"
            "/?fields[asset]=download_urls,download_url,external_url,filename,asset_type"
        )
        return self.get(url, timeout=15)

    def captions(self, asset: dict) -> list[dict]:
        """
        Extract caption entries from an *asset* dict.

        Returns a list of ``{"locale": str, "title": str, "url": str}``.
        """
        result = []
        for cap in asset.get("captions", []):
            url = cap.get("url") or cap.get("file_name")
            if url:
                result.append({
                    "locale": cap.get("locale_id", cap.get("language_code", "unknown")),
                    "title":  cap.get("title", ""),
                    "url":    url,
                })
        return result


# ──────────────────────────────────────────────────────────────────────────────
#  Stream-URL resolution  (mirrors resolve_lecture_stream in RequestHandler.cpp)
# ──────────────────────────────────────────────────────────────────────────────

def pick_mp4_url(entries: list[dict], prefer_quality: str) -> Optional[str]:
    """
    Choose the best direct MP4 URL from a ``stream_urls`` or
    ``download_urls`` Video array.

    Selection strategy (mirrors the C++ qmap logic):
      - ``"Highest"`` or quality ≥ 1080 → highest available resolution.
      - ``"Lowest"``                     → lowest available resolution.
      - Numeric string (e.g. ``"720"``)  → exact match, then nearest higher,
                                           then highest available.

    HLS / Auto entries are skipped; those are handled by ``pick_hls_url``.
    """
    qmap: dict[int, str] = {}   # {height_px: url}
    fallback: Optional[str] = None

    for v in entries:
        url   = v.get("file", "")
        label = v.get("label", "")
        vtype = v.get("type",  "")
        if not url:
            continue
        # skip HLS/Auto entries
        if vtype == "application/x-mpegURL" or label == "Auto":
            continue
        q = extract_quality_value(label)
        if q > 0:
            qmap[q] = url
        elif fallback is None:
            fallback = url

    if not qmap:
        return fallback

    wanted = extract_quality_value(prefer_quality)
    if prefer_quality == "Highest" or wanted >= 1080:
        return qmap[max(qmap)]
    if prefer_quality == "Lowest":
        return qmap[min(qmap)]
    if wanted in qmap:
        return qmap[wanted]
    # nearest higher resolution
    higher = [q for q in sorted(qmap) if q >= wanted]
    return qmap[higher[0]] if higher else qmap[max(qmap)]


def pick_hls_url(stream_urls: list[dict], media_sources: list[dict],
                 asset: dict) -> Optional[str]:
    """
    Return the best HLS master-playlist URL from an asset, checking
    ``stream_urls``, ``media_sources``, and the ``hls_url`` fallback field
    in that order.
    """
    for v in stream_urls:
        if v.get("type") == "application/x-mpegURL" or v.get("label") == "Auto":
            url = v.get("file", "")
            if url:
                return url
    for m in media_sources:
        src = m.get("src", "")
        if src:
            return src
    return asset.get("hls_url") or None


def resolve_stream(asset: dict, prefer_quality: str) -> Optional[str]:
    """
    Choose the best playable URL for a lecture *asset* dict.

    Decision tree (mirrors ``resolve_lecture_stream()`` in RequestHandler.cpp):

    1. For quality ≥ 1080 or ``"Highest"``, prefer HLS because Udemy rarely
       offers a direct 1080p MP4 — it's usually only available via the adaptive
       HLS manifest.
    2. If the requested quality exceeds the best available MP4, also force HLS.
    3. Otherwise return the best direct MP4.
    4. Fall back to HLS if no MP4 is found.

    Parameters
    ----------
    asset:           The ``asset`` sub-object from the lecture detail API.
    prefer_quality:  One of ``"1080"``, ``"720"``, ``"480"``, ``"360"``,
                     ``"Highest"``, or ``"Lowest"``.
    """
    stream_urls   = (asset.get("stream_urls")  or {}).get("Video", [])
    download_urls = (asset.get("download_urls") or {}).get("Video", [])
    media_sources =  asset.get("media_sources") or []

    wanted    = extract_quality_value(prefer_quality)
    # Prefer HLS for high-res requests
    prefer_hls = prefer_quality == "Highest" or wanted >= 1080

    all_entries = download_urls or stream_urls
    mp4 = pick_mp4_url(all_entries, prefer_quality)

    # Also prefer HLS if the best MP4 is lower than what was asked for
    highest_mp4_q = max(
        (extract_quality_value(v.get("label", "")) for v in all_entries),
        default=0,
    )
    if wanted > 0 and highest_mp4_q > 0 and wanted > highest_mp4_q:
        prefer_hls = True

    hls = pick_hls_url(stream_urls, media_sources, asset)

    if prefer_hls and hls:
        return hls
    if mp4 and ".m3u8" not in mp4.lower():
        return mp4
    return hls  # None if neither found


def resolve_supplementary_url(asset: dict) -> Optional[str]:
    """
    Return a direct download URL from a supplementary asset dict, trying
    ``download_urls``, ``download_url``, and ``external_url`` in that order.

    Mirrors ``resolve_supplementary_asset()`` in RequestHandler.cpp.
    """
    for arr in (asset.get("download_urls") or {}).values():
        if isinstance(arr, list) and arr:
            url = arr[0].get("file", "")
            if url:
                return url
    return asset.get("download_url") or asset.get("external_url") or None


# ──────────────────────────────────────────────────────────────────────────────
#  File downloader  (mirrors curl_download_file + FFmpegHelper in the C++)
# ──────────────────────────────────────────────────────────────────────────────

class Downloader:
    """
    Downloads files (plain HTTP) and HLS streams (via ffmpeg mux).

    Plain downloads use ``requests`` with streaming and write to a ``.part``
    temp file that is atomically renamed on completion — same pattern as
    ``curl_download_file()`` in RequestHandler.cpp.

    HLS downloads mirror the ``worker_loop`` technique:
      1. Fetch the playlist (following redirects to get the effective URL).
      2. If it is a master playlist, pick the highest-resolution variant.
      3. Rewrite all relative segment URLs to absolute HTTPS URLs so the
         local ``.m3u8`` is self-contained.
      4. Write the rewritten playlist to a temp ``.local.m3u8`` file.
      5. Call ``ffmpeg -c copy -f mp4`` to remux the TS segments into MP4
         without re-encoding (``FFmpegHelper::convert_m3u8_to_ts``).
      6. Delete the temp playlist; rename the ``.part`` output to final path.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    # ------------------------------------------------------------------
    def _auth_headers(self, url: str) -> dict[str, str]:
        """
        Return auth headers for download requests.

        Skips auth for pre-signed CDN URLs that already carry credentials
        in query parameters (CloudFront ``Signature=`` / ``token=``).
        Mirrors ``append_auth_headers_for_url()`` in RequestHandler.cpp.
        """
        if "Signature=" in url or "token=" in url:
            return {}
        cookie = f"access_token={self._s.token}"
        if self._s.client_id:
            cookie += f"; client_id={self._s.client_id}"
        return {
            "Authorization": f"Bearer {self._s.token}",
            "Referer": "https://www.udemy.com/",
            "Origin":  "https://www.udemy.com",
            "Cookie":  cookie,
        }

    def _proxies(self) -> dict[str, str]:
        if self._s.proxy:
            return {"http": self._s.proxy, "https": self._s.proxy}
        return {}

    def _verify_ssl(self) -> bool:
        return not bool(self._s.proxy)

    # ------------------------------------------------------------------
    def download_file(self, url: str, dest: Path, label: str = "") -> bool:
        """
        Download *url* to *dest* with a live progress bar.

        Uses a ``.part`` temp file for atomic writes; removes it on failure.
        ``timeout=(10, None)`` means 10 s connect timeout, no read timeout —
        appropriate for large files on slow connections.

        Returns ``True`` on success.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers = {**BROWSER_HEADERS, **self._auth_headers(url)}

        try:
            with self._session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(10, None),    # connect=10s, read=unlimited
                proxies=self._proxies(),
                verify=self._verify_ssl(),
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                done  = 0
                t0    = time.time()

                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(65_536):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done   += len(chunk)
                        elapsed = max(0.1, time.time() - t0)
                        bar     = _progress_bar(done, total)
                        tag     = _col(label[:40], _DIM) if label else ""
                        print(
                            f"\r  {bar}  {_col(_fmt_speed(done / elapsed), _CYAN)}  {tag}",
                            end="", flush=True,
                        )

            tmp.rename(dest)
            print()   # newline after the \r progress line
            return True

        except Exception as exc:
            print()
            _err(f"Download failed: {exc}")
            tmp.unlink(missing_ok=True)
            return False

    # ------------------------------------------------------------------
    def download_hls(self, url: str, dest: Path, label: str = "") -> bool:
        """
        Download an HLS stream to *dest* (always ``.mp4``).

        Steps mirror the HLS branch of ``worker_loop`` in RequestHandler.cpp:

        1. Fetch the initial playlist URL (may be a master or a variant).
        2. If master (``#EXT-X-STREAM-INF`` present), pick the best variant
           via :meth:`_pick_best_variant`.
        3. Bail out if the playlist is DRM-encrypted (``#EXT-X-KEY``).
        4. Rewrite relative segment/key URLs to absolute HTTPS, preserving the
           CloudFront signed-URL query string from the effective (post-redirect)
           URL.
        5. Write the rewritten playlist to a sibling ``.local.m3u8`` file.
        6. Invoke ``ffmpeg`` with ``-c copy -f mp4`` to remux without
           re-encoding.  ``-f mp4`` is required because ffmpeg cannot infer
           the muxer from the ``.part`` output extension.
        7. Delete the temp playlist; rename ``.part`` → final path.

        Returns ``True`` on success.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest_mp4   = dest.with_suffix(".mp4")
        tmp        = dest_mp4.with_suffix(".mp4.part")
        local_m3u8 = dest_mp4.with_suffix(".local.m3u8")

        # ── Step 1: fetch playlist ────────────────────────────────────────
        auth_h = self._auth_headers(url)
        try:
            r = self._session.get(
                url,
                headers={**BROWSER_HEADERS, **auth_h},
                timeout=20,
                proxies=self._proxies(),
                verify=self._verify_ssl(),
            )
            r.raise_for_status()
            body      = r.text
            effective = r.url   # actual URL after any redirects
        except Exception as exc:
            _err(f"Failed to fetch playlist: {exc}")
            return False

        # ── Step 2: master → variant ──────────────────────────────────────
        if "#EXT-X-STREAM-INF" in body:
            body, effective = self._pick_best_variant(body, effective)
            if body is None:
                _err("Could not fetch HLS variant playlist.")
                return False

        # ── Step 3: DRM check ─────────────────────────────────────────────
        if "#EXT-X-KEY" in body:
            _err("Encrypted HLS stream (DRM) — cannot download.")
            return False

        # ── Step 4: rewrite relative segment URLs to absolute HTTPS ───────
        #
        # The effective URL looks like:
        #   https://hls-c.udemycdn.com/.../index.m3u8?Policy=…&Signature=…
        #
        # Segment lines in the playlist are relative paths like:
        #   AVC_1920x1080_1200k/aa00cda3…0.ts
        #
        # We need to prepend the base directory of the effective URL, plus
        # the original query string (which holds the CloudFront credentials).
        base  = effective.split("?")[0].rsplit("/", 1)[0] + "/"
        query = ("?" + effective.split("?")[1]) if "?" in effective else ""

        rewritten: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip("\r\n'\"")   # strip CR + stray quotes
            if not line:
                continue
            if line.startswith("#"):
                rewritten.append(line)
                continue
            # Non-comment line → segment or key URI
            if "://" not in line:
                if line.startswith("/"):
                    # absolute path — prepend scheme + host only
                    scheme_host = "/".join(effective.split("/")[:3])
                    abs_url = scheme_host + line
                else:
                    abs_url = base + line
                # Append signed-URL credentials if not already present
                if "?" not in abs_url:
                    abs_url += query
                rewritten.append(abs_url)
            else:
                rewritten.append(line)   # already absolute

        local_m3u8.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

        # ── Step 5 & 6: ffmpeg mux ────────────────────────────────────────
        #
        # Notes on flags:
        #   -allowed_extensions ALL   — required for ffmpeg to open .m3u8
        #   -protocol_whitelist …     — allow file + HTTPS segment fetching
        #   -f mp4                    — explicit muxer; ffmpeg cannot infer it
        #                               from ".part" extension
        #   -movflags +faststart      — move moov atom to start for streaming
        #   -c copy                   — remux only, no re-encoding
        cmd = [
            "ffmpeg", "-y",
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
            "-i", str(local_m3u8),
            "-c", "copy",
            "-f", "mp4",
            "-movflags", "+faststart",
            str(tmp),
        ]
        _info(f"ffmpeg → {dest_mp4.name}")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            local_m3u8.unlink(missing_ok=True)   # always clean up temp playlist

            if proc.returncode != 0:
                _err(f"ffmpeg failed:\n{proc.stderr.strip()}")
                tmp.unlink(missing_ok=True)
                return False

            tmp.rename(dest_mp4)
            return True

        except FileNotFoundError:
            _err("ffmpeg not found. Install it: https://ffmpeg.org/download.html")
            local_m3u8.unlink(missing_ok=True)
            return False

    # ------------------------------------------------------------------
    def _pick_best_variant(self, master: str,
                           master_url: str) -> tuple[Optional[str], str]:
        """
        Parse an HLS master playlist and fetch the highest-resolution variant.

        Returns ``(playlist_body, effective_url)`` of the chosen variant,
        or ``(None, master_url)`` on failure.
        """
        best_h, best_bw, best_uri = -1, -1, None
        lines = master.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_m  = re.search(r"BANDWIDTH=(\d+)", line)
                res_m = re.search(r"RESOLUTION=\d+x(\d+)", line)
                bw = int(bw_m.group(1))  if bw_m  else 0
                h  = int(res_m.group(1)) if res_m else 0

                # skip blank / comment lines to get the URI
                i += 1
                while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#")):
                    i += 1
                if i < len(lines):
                    uri = lines[i].strip()
                    if h > best_h or (h == best_h and bw > best_bw):
                        best_h, best_bw, best_uri = h, bw, uri
            i += 1

        if not best_uri:
            return None, master_url

        # make URI absolute
        if "://" not in best_uri:
            base     = master_url.split("?")[0].rsplit("/", 1)[0] + "/"
            best_uri = base + best_uri

        auth_h = self._auth_headers(best_uri)
        try:
            r = self._session.get(
                best_uri,
                headers={**BROWSER_HEADERS, **auth_h},
                timeout=20,
                proxies=self._proxies(),
                verify=self._verify_ssl(),
            )
            r.raise_for_status()
            return r.text, r.url
        except Exception as exc:
            _err(f"Variant playlist fetch failed: {exc}")
            return None, best_uri

    # ------------------------------------------------------------------
    def download_subtitle(self, url: str, dest: Path) -> bool:
        """Download a subtitle/caption file (VTT/SRT) to *dest*."""
        return self.download_file(url, dest, label=dest.name)


# ──────────────────────────────────────────────────────────────────────────────
#  Course orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class CourseDownloader:
    """
    High-level orchestrator that iterates a course's curriculum and dispatches
    each item to :class:`Downloader`.

    Mirrors the ``worker_loop`` + ``download_course`` logic spread across
    RequestHandler.cpp and the frontend JavaScript.
    """

    def __init__(self, client: UdemyClient, dl: Downloader,
                 settings: Settings, quality: str = "720") -> None:
        self.client   = client
        self.dl       = dl
        self.settings = settings
        self.quality  = quality

    # ------------------------------------------------------------------
    def _all_curriculum(self, course_id: int) -> list[dict]:
        """
        Fetch every page of curriculum items and return them concatenated.

        Mirrors the paging loop implicit in ``handleLectures``.
        """
        results: list[dict] = []
        page = 1
        while True:
            data = self.client.curriculum(course_id, page=page, page_size=200)
            results.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
            time.sleep(0.5)   # be respectful to the API
        return results

    # ------------------------------------------------------------------
    def download_course(self, course_id: int, course_title: str) -> None:
        """
        Download all lectures (and optionally subtitles + assets) for a course.

        Creates the output directory tree under ``downloads/`` and skips any
        file that already exists (safe to re-run after interruption).
        """
        _head(f"Downloading: {course_title}")
        out_root = course_dir(course_id, course_title)
        out_root.mkdir(parents=True, exist_ok=True)

        items = self._all_curriculum(course_id)
        _info(f"Fetched {len(items)} curriculum items")

        cur_section     = 0
        cur_section_ttl = ""
        lecture_idx     = 0

        for item in items:
            klass = item.get("_class") or item.get("type", "")

            # ── chapter / section header ──────────────────────────────────
            if klass == "chapter":
                cur_section    += 1
                cur_section_ttl = item.get("title", "")
                sec_path = out_root / section_dir(cur_section, cur_section_ttl)
                sec_path.mkdir(parents=True, exist_ok=True)
                print(f"\n  {_col(f'Section {cur_section}: {cur_section_ttl}', _BOLD, _YELLOW)}")
                continue

            if klass != "lecture":
                continue   # skip quiz/practice nodes

            # ── lecture ───────────────────────────────────────────────────
            lecture_idx += 1
            lec_id     = item.get("id", 0)
            lec_title  = item.get("title", "")
            asset      = item.get("asset") or {}
            asset_type = asset.get("asset_type", "")

            sec_path = out_root / section_dir(cur_section, cur_section_ttl)
            sec_path.mkdir(parents=True, exist_ok=True)

            # Zero-padded prefix shared by video, subtitle and asset filenames
            prefix = f"{_zpad(lecture_idx, 3)} - {slugify(lec_title)}"

            if asset_type in ("Video", ""):
                print(f"\n  {_col(prefix, _BOLD)}")
                self._download_lecture_video(course_id, lec_id, asset, sec_path, prefix)

            elif asset_type == "Article":
                # Save article body as HTML
                body = asset.get("body") or asset.get("data", {}).get("body", "")
                if body:
                    out_f = sec_path / f"{prefix}.html"
                    if not out_f.exists():
                        out_f.write_text(body, encoding="utf-8")
                        _ok(f"Saved article: {out_f.name}")

            if self.settings.download_assets:
                for sa in item.get("supplementary_assets", []):
                    self._download_supplementary(sa, sec_path, prefix)

            time.sleep(0.3)   # polite API pacing

    # ------------------------------------------------------------------
    def _download_lecture_video(self, course_id: int, lec_id: int,
                                asset: dict, dest_dir: Path,
                                prefix: str) -> None:
        """
        Resolve and download the video for a single lecture.

        Fetches the richer per-lecture detail endpoint first (it has captions
        and better stream_urls), then falls back to the curriculum asset on
        error.  Subtitles are downloaded alongside the video when enabled.
        """
        # Fetch full detail for richer stream_urls + captions
        try:
            detail = self.client.lecture_detail(course_id, lec_id)
            asset  = detail.get("asset", asset)
        except Exception as exc:
            _warn(f"Could not fetch lecture detail ({exc}); using curriculum asset.")

        url = resolve_stream(asset, self.quality)
        if not url:
            _warn(f"No stream URL for lecture {lec_id} — skipping.")
            return

        is_hls = ".m3u8" in url.lower()
        dest   = dest_dir / f"{prefix}.mp4"

        if dest.exists():
            _ok(f"Already exists: {dest.name}")
        else:
            if is_hls:
                _info(f"HLS stream → {dest.name}")
                self.dl.download_hls(url, dest, label=prefix)
            else:
                _info(f"MP4 → {dest.name}")
                self.dl.download_file(url, dest, label=prefix)

        # Download subtitles / captions
        if self.settings.download_subtitles:
            for cap in self.client.captions(asset):
                locale  = cap["locale"].replace("-", "_")
                srt_dst = dest_dir / f"{prefix}.{locale}.vtt"
                if not srt_dst.exists():
                    self.dl.download_subtitle(cap["url"], srt_dst)

    # ------------------------------------------------------------------
    def _download_supplementary(self, sa: dict, dest_dir: Path,
                                prefix: str) -> None:
        """
        Resolve and download one supplementary asset (PDF, zip, code, etc.).

        Fetches the asset detail endpoint to get a fresh signed download URL,
        then appends the correct extension without double-suffixing
        (e.g. avoids ``file.pdf.pdf``).
        """
        sa_title = slugify(sa.get("title") or sa.get("filename") or "file")
        asset_id = sa.get("id")

        # Refresh to get the signed download URL
        if asset_id:
            try:
                fresh = self.client.asset_detail(asset_id)
                sa = fresh.get("asset", fresh)
            except Exception:
                pass   # fall through and try the curriculum asset data

        url = resolve_supplementary_url(sa)
        if not url:
            return

        # Determine extension from filename field, avoiding double suffix
        fname = sa.get("filename") or url.split("?")[0].rsplit("/", 1)[-1]
        ext   = Path(fname).suffix or ".bin"
        dest_name = f"{prefix} - {sa_title}"
        if not dest_name.lower().endswith(ext.lower()):
            dest_name += ext
        dest = dest_dir / dest_name

        if dest.exists():
            return

        _info(f"Asset: {dest.name}")
        self.dl.download_file(url, dest, label=dest.name)


# ──────────────────────────────────────────────────────────────────────────────
#  Interactive UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def _prompt(msg: str, default: str = "") -> str:
    """Print a prompt and return stripped user input (or *default* on empty)."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {_col('?', _CYAN, _BOLD)} {msg}{suffix}: ").strip()
        return val or default
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def _choose(options: list[tuple[str, object]], title: str = "") -> int:
    """
    Display a numbered menu and return the 0-based index of the chosen item.
    Loops until a valid number is entered.
    """
    if title:
        _head(title)
    for i, (label, _) in enumerate(options, 1):
        print(f"  {_col(str(i), _CYAN, _BOLD)}. {label}")
    while True:
        raw = _prompt("Enter number")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        _err(f"Enter a number between 1 and {len(options)}")


def _setup_token(settings: Settings) -> None:
    """Interactive first-run flow to collect and save the access token."""
    _head("Authentication")
    print(f"  Get your token from {_col('udemy.com', _CYAN)} cookies → {_col('access_token', _BOLD)}")
    print("  (Browser DevTools → Application → Cookies → udemy.com)\n")
    token = _prompt("Paste your access_token")
    if not token:
        _err("Token cannot be empty.")
        sys.exit(1)
    settings.token = token
    settings.save()
    _ok("Token saved to settings.ini")


def _interactive_pick_course(client: UdemyClient) -> Optional[dict]:
    """
    Browse / search subscribed courses interactively.
    Returns the chosen course dict, or ``None`` if the user quits.
    """
    _head("Your Courses")
    query = _prompt("Search (leave blank for recent)", "")
    page  = 1

    while True:
        data    = client.courses(page=page, page_size=12, query=query)
        courses = data.get("results", [])
        total   = data.get("count", 0)

        if not courses:
            _warn("No courses found.")
            return None

        print(f"\n  {_col(f'Page {page}  —  {total} courses total', _DIM)}\n")
        for i, c in enumerate(courses, 1):
            inst = (c.get("visible_instructors") or [{}])[0].get("title", "")
            print(
                f"  {_col(str(i), _CYAN, _BOLD)}. {c['title']}"
                + (f"  {_col(inst, _DIM)}" if inst else "")
            )

        # Navigation options appear after the course list
        nav: list[tuple[str, Optional[str]]] = [("Select a course above", None)]
        if page > 1:
            nav.append(("← Previous page", "prev"))
        if total > page * 12:
            nav.append(("Next page →", "next"))
        nav.append(("Search again", "search"))
        nav.append(("Quit", "quit"))

        print()
        for i, (label, _) in enumerate(nav, 1):
            print(f"  {_col(len(courses) + i, _CYAN)}. {label}")

        choice = _prompt("Choice")
        if not choice.isdigit():
            continue

        n = int(choice)
        if 1 <= n <= len(courses):
            return courses[n - 1]

        extra = n - len(courses) - 1
        if 0 <= extra < len(nav):
            action = nav[extra][1]
            if action == "prev":
                page -= 1
            elif action == "next":
                page += 1
            elif action == "search":
                query = _prompt("Search query", "")
                page  = 1
            elif action == "quit":
                sys.exit(0)


def _pick_quality() -> str:
    """Present a quality-selection menu and return the chosen quality string."""
    opts = [
        ("1080p — HLS adaptive stream (highest quality)", "1080"),
        ("720p  — direct MP4",                            "720"),
        ("480p  — direct MP4",                            "480"),
        ("360p  — direct MP4",                            "360"),
        ("Highest available MP4",                         "Highest"),
        ("Lowest  available MP4 (smallest file)",         "Lowest"),
    ]
    return opts[_choose(opts, "Video quality")][1]


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="udemy_archivist",
        description="Download Udemy courses you are enrolled in.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python udemy_archivist.py\n"
            "  python udemy_archivist.py --token YOUR_TOKEN\n"
            "  python udemy_archivist.py --list\n"
            "  python udemy_archivist.py --course 1234567 --quality 720\n"
            "  python udemy_archivist.py --course 1234567 --no-subs --no-assets\n"
        ),
    )
    parser.add_argument("--token",      metavar="TOKEN",
                        help="Save Udemy access_token to settings.ini and exit.")
    parser.add_argument("--course",     type=int, metavar="ID",
                        help="Course ID to download (skip interactive picker).")
    parser.add_argument("--quality",    default="", metavar="Q",
                        help="Video quality: 360 | 480 | 720 | 1080 | Highest | Lowest.")
    parser.add_argument("--list",       action="store_true",
                        help="Print all subscribed courses and exit.")
    parser.add_argument("--no-assets",  action="store_true",
                        help="Skip supplementary files (PDFs, ZIPs, …).")
    parser.add_argument("--no-subs",    action="store_true",
                        help="Skip subtitle / caption files.")
    args = parser.parse_args()

    # ── Settings ────────────────────────────────────────────────────────────
    settings = Settings()
    settings.load()

    if args.token:
        settings.token = args.token.strip()
        settings.save()
        _ok("Token saved to settings.ini")

    if args.no_assets:
        settings.download_assets = False
    if args.no_subs:
        settings.download_subtitles = False

    # ── Ensure token ────────────────────────────────────────────────────────
    if not settings.token:
        _setup_token(settings)

    client = UdemyClient(settings)

    # ── Verify token ────────────────────────────────────────────────────────
    try:
        me   = client.whoami()
        name = me.get("display_name") or me.get("name") or "Unknown"
        _ok(f"Logged in as: {_col(name, _BOLD, _GREEN)}")
    except Exception as exc:
        _err(f"Authentication failed: {exc}")
        _warn("Check your access_token in settings.ini")
        sys.exit(1)

    dl = Downloader(settings)

    # ── --list ───────────────────────────────────────────────────────────────
    if args.list:
        _head("Subscribed Courses")
        page, page_size = 1, 20
        while True:
            data    = client.courses(page=page, page_size=page_size)
            courses = data.get("results", [])
            total   = data.get("count", 0)
            for c in courses:
                print(f"  {_col(c['id'], _CYAN)}  {c['title']}")
            shown = (page - 1) * page_size + len(courses)
            if shown >= total:
                break
            if _prompt(f"Showing {shown}/{total}. Load more? [Y/n]", "y").lower() == "n":
                break
            page += 1
        return

    # ── --course ID ──────────────────────────────────────────────────────────
    if args.course:
        # Try to resolve the human-readable title for the directory name
        try:
            data    = client.courses(page=1, page_size=100)
            courses = data.get("results", [])
            course  = next((c for c in courses if c["id"] == args.course), None)
            if not course:
                course = {"id": args.course, "title": f"course_{args.course}"}
        except Exception:
            course = {"id": args.course, "title": f"course_{args.course}"}

        quality = args.quality or _pick_quality()
        CourseDownloader(client, dl, settings, quality=quality).download_course(
            args.course, course["title"]
        )
        return

    # ── Interactive wizard ───────────────────────────────────────────────────
    course = _interactive_pick_course(client)
    if not course:
        sys.exit(0)

    quality = args.quality or _pick_quality()

    _head("Ready to download")
    print(f"  Course  : {_col(course['title'], _BOLD)}")
    print(f"  Quality : {_col(quality, _CYAN)}")
    print(f"  Output  : {_col(str(course_dir(course['id'], course['title'])), _DIM)}")
    print(f"  Subs    : {_col('yes', _GREEN) if settings.download_subtitles else _col('no', _RED)}")
    print(f"  Assets  : {_col('yes', _GREEN) if settings.download_assets    else _col('no', _RED)}")

    if _prompt("\nStart download? [Y/n]", "y").lower() == "n":
        _info("Aborted.")
        sys.exit(0)

    try:
        CourseDownloader(client, dl, settings, quality=quality).download_course(
            course["id"], course["title"]
        )
        _head("All done!")
    except KeyboardInterrupt:
        print()
        _warn("Interrupted. Partial files kept — re-run to resume.")


if __name__ == "__main__":
    main()
