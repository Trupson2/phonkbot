"""
Video renderer for PhonkBot — FFmpeg-based audio visualizations.
Styles: phonk (Pexels video bg + logo), waveform, bars, scope.
Optimized for Raspberry Pi 5.
"""

import os
import json
import random
import subprocess
import requests as _requests
from modules.logger import log, log_error
from modules.database import get_db, get_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKGROUNDS_DIR = os.path.join(_APP_DIR, 'static', 'assets', 'backgrounds')
LOGO_PATH = os.path.join(_APP_DIR, 'static', 'assets', 'logo.png')
OUTPUT_DIR = os.path.join(_APP_DIR, 'output')
STOCK_CLIPS_DIR = os.path.join(OUTPUT_DIR, 'stock_clips')


# ═══════════════════════════════════════
# PEXELS STOCK VIDEO
# ═══════════════════════════════════════

# Curated phonk/drift video IDs from Pexels (free to use)
PEXELS_VIDEO_IDS = [
    5673624,   # drift car racing
    4488286,   # night city driving
    3571264,   # car headlights night
    4434242,   # city lights night
    5765017,   # underground tunnel
    3945204,   # neon city
    4169592,   # night highway
    2611199,   # dark smoke
    5538025,   # city night rain
    4763824,   # dark clouds storm
]


def _download_pexels_clip(video_id=None):
    """Download a stock video clip from Pexels for background."""
    os.makedirs(STOCK_CLIPS_DIR, exist_ok=True)

    if not video_id:
        video_id = random.choice(PEXELS_VIDEO_IDS)

    clip_path = os.path.join(STOCK_CLIPS_DIR, f'pexels_{video_id}.mp4')

    # Return cached clip if exists
    if os.path.exists(clip_path) and os.path.getsize(clip_path) > 100 * 1024:
        log(f"Pexels: using cached clip {video_id}")
        return clip_path

    pexels_key = get_config('pexels_api_key', '')
    if not pexels_key:
        log("Pexels: no API key configured, using existing clips or solid bg")
        # Try to use any existing clip
        existing = [f for f in os.listdir(STOCK_CLIPS_DIR)
                    if f.endswith('.mp4') and os.path.getsize(os.path.join(STOCK_CLIPS_DIR, f)) > 100 * 1024]
        if existing:
            return os.path.join(STOCK_CLIPS_DIR, random.choice(existing))
        return None

    try:
        # Get video details from Pexels API
        resp = _requests.get(
            f'https://api.pexels.com/videos/videos/{video_id}',
            headers={'Authorization': pexels_key},
            timeout=15,
        )
        if resp.status_code != 200:
            log_error(f"Pexels: API error {resp.status_code}")
            return None

        data = resp.json()
        video_files = data.get('video_files', [])

        # Pick best quality HD file (prefer 1920x1080)
        best = None
        for vf in video_files:
            w = vf.get('width', 0)
            h = vf.get('height', 0)
            if w >= 1280 and h >= 720:
                if not best or (w == 1920 and h == 1080):
                    best = vf

        if not best and video_files:
            best = video_files[0]

        if not best:
            log_error("Pexels: no suitable video file found")
            return None

        download_url = best.get('link', '')
        width = best.get('width', 0)
        height = best.get('height', 0)
        log(f"Pexels: downloading clip {video_id} ({width}x{height})")

        # Download video
        resp = _requests.get(download_url, timeout=120, stream=True)
        if resp.status_code != 200:
            log_error(f"Pexels: download failed — HTTP {resp.status_code}")
            return None

        with open(clip_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        size_mb = os.path.getsize(clip_path) / (1024 * 1024)
        log(f"Pexels: downloaded {size_mb:.1f}MB -> {clip_path}")
        return clip_path

    except Exception as e:
        log_error(f"Pexels: error — {e}")
        return None


# ═══════════════════════════════════════
# AUDIO HELPERS
# ═══════════════════════════════════════

def get_audio_duration(audio_path):
    """Get audio duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', audio_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except:
        return 0.0


def get_audio_features(audio_path):
    """Extract audio features for quality model training. Returns JSON string."""
    features = {}
    try:
        features['duration'] = get_audio_duration(audio_path)

        result = subprocess.run(
            ['ffmpeg', '-i', audio_path, '-af', 'loudnorm=print_format=json', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=30,
        )
        stderr = result.stderr
        json_start = stderr.rfind('{')
        json_end = stderr.rfind('}') + 1
        if json_start > 0 and json_end > json_start:
            loudnorm = json.loads(stderr[json_start:json_end])
            features['input_i'] = float(loudnorm.get('input_i', 0))
            features['input_tp'] = float(loudnorm.get('input_tp', 0))
            features['input_lra'] = float(loudnorm.get('input_lra', 0))

        features['file_size_kb'] = os.path.getsize(audio_path) // 1024

    except Exception as e:
        features['error'] = str(e)

    return json.dumps(features)


# ═══════════════════════════════════════
# BACKGROUND HELPERS
# ═══════════════════════════════════════

def _get_background():
    """Pick a random background image, or generate a black frame if none exist."""
    if os.path.isdir(BACKGROUNDS_DIR):
        imgs = [f for f in os.listdir(BACKGROUNDS_DIR)
                if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        if imgs:
            return os.path.join(BACKGROUNDS_DIR, random.choice(imgs))
    return None


# ═══════════════════════════════════════
# FFMPEG COMMAND BUILDERS
# ═══════════════════════════════════════

def _build_ffmpeg_cmd(audio_path, output_path, style, background_path, duration, track_id=0):
    """Build FFmpeg command based on visualization style."""

    # Base encoding settings (optimized for Pi 5, smaller files)
    encode = [
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30',
        '-tune', 'stillimage', '-pix_fmt', 'yuv420p', '-threads', '4',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest', '-y',
    ]

    if style == 'phonk':
        return _build_phonk_cmd(audio_path, output_path, background_path, duration, track_id, encode)
    elif style == 'bars':
        return _build_bars_cmd(audio_path, output_path, background_path, duration, track_id, encode)
    elif style == 'scope':
        return _build_scope_cmd(audio_path, output_path, background_path, duration, track_id, encode)
    elif style == 'aurora':
        return _build_aurora_cmd(audio_path, output_path, duration, track_id, encode)
    else:
        # Default: waveform
        return _build_waveform_cmd(audio_path, output_path, background_path, duration, track_id, encode)


def _build_phonk_cmd(audio_path, output_path, background_path, duration, track_id, encode):
    """
    PHONK style — Pexels video background + logo + frequency bars + vignette.
    Best looking but heaviest on CPU.
    """
    # Try to get a stock video clip for background
    stock_clip = _download_pexels_clip()

    has_logo = os.path.exists(LOGO_PATH)

    if stock_clip:
        # ═══ PHONK with VIDEO background ═══
        inputs = [
            'ffmpeg',
            '-stream_loop', '-1', '-i', stock_clip,   # [0] video bg (looping)
        ]
        if has_logo:
            inputs += ['-loop', '1', '-i', LOGO_PATH]  # [1] logo
        inputs += ['-i', audio_path]                    # [2] or [1] audio

        audio_idx = 2 if has_logo else 1

        # Build filter chain
        filters = []

        # Background: scale, crop, darken, vignette
        filters.append(
            f'[0:v]scale=1920:1080:force_original_aspect_ratio=increase,'
            f'crop=1920:1080,setpts=PTS-STARTPTS,'
            f'eq=brightness=-0.2:saturation=0.6[bg];'
            f'[bg]vignette=PI/3[vig]'
        )

        if has_logo:
            # Logo with subtle glow
            filters.append(
                f'[1:v]scale=200:200[logoscaled];'
                f'[logoscaled]boxblur=15:15[glow];'
                f'[glow]colorbalance=rs=0.3:gs=-0.1:bs=0.4[glowcolor];'
                f'[vig][glowcolor]overlay=(W-200)/2:(H-200)/2-40:format=auto[withglow];'
                f'[withglow][logoscaled]overlay=(W-200)/2:(H-200)/2-40:format=auto[withlogo]'
            )
            prev_label = 'withlogo'
        else:
            prev_label = 'vig'

        # Frequency bars at bottom
        filters.append(
            f'[{audio_idx}:a]showfreqs=s=1920x150:mode=bar:ascale=log:fscale=lin:'
            f'win_size=2048:colors=0xff2222|0xff6600|0xffaa00:averaging=6[freq];'
            f'[freq]colorkey=black:0.12:0.1[freqt];'
            f'[{prev_label}][freqt]overlay=0:H-150:format=auto[withfreq]'
        )

        # Track number text
        filters.append(
            f'[withfreq]drawtext=text=\'Track \\#{track_id}\':'
            f'fontsize=28:fontcolor=white@0.6:'
            f'x=(w-text_w)/2:y=h-180:font=DejaVu Sans Bold[out]'
        )

        filter_str = ';'.join(filters)

        return [
            *inputs,
            '-filter_complex', filter_str,
            '-map', '[out]', '-map', f'{audio_idx}:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]

    else:
        # ═══ PHONK with static/black background (no Pexels) ═══
        bg = background_path or None
        if bg:
            inputs = ['ffmpeg', '-loop', '1', '-i', bg]
        else:
            inputs = ['ffmpeg', '-f', 'lavfi', '-i',
                      f'color=c=0x0a0a12:s=1920x1080:d={int(duration + 1)}']

        if has_logo:
            inputs += ['-loop', '1', '-i', LOGO_PATH]
        inputs += ['-i', audio_path]

        audio_idx = (1 if bg else 1) + (1 if has_logo else 0)

        filters = []
        if bg:
            filters.append('[0:v]scale=1920:1080,eq=brightness=-0.15:saturation=0.5[bg];[bg]vignette=PI/3[vig]')
        else:
            filters.append('[0:v]vignette=PI/4[vig]')

        if has_logo:
            logo_idx = 1
            filters.append(
                f'[{logo_idx}:v]scale=200:200[logoscaled];'
                f'[logoscaled]boxblur=15:15[glow];'
                f'[glow]colorbalance=rs=0.3:gs=-0.1:bs=0.4[glowcolor];'
                f'[vig][glowcolor]overlay=(W-200)/2:(H-200)/2-40:format=auto[withglow];'
                f'[withglow][logoscaled]overlay=(W-200)/2:(H-200)/2-40:format=auto[withlogo]'
            )
            prev_label = 'withlogo'
        else:
            prev_label = 'vig'

        filters.append(
            f'[{audio_idx}:a]showfreqs=s=1920x150:mode=bar:ascale=log:fscale=lin:'
            f'win_size=2048:colors=0xff2222|0xff6600|0xffaa00:averaging=6[freq];'
            f'[freq]colorkey=black:0.12:0.1[freqt];'
            f'[{prev_label}][freqt]overlay=0:H-150:format=auto[withfreq]'
        )

        filters.append(
            f'[withfreq]drawtext=text=\'Track \\#{track_id}\':'
            f'fontsize=28:fontcolor=white@0.6:'
            f'x=(w-text_w)/2:y=h-180:font=DejaVu Sans Bold[out]'
        )

        filter_str = ';'.join(filters)

        return [
            *inputs,
            '-filter_complex', filter_str,
            '-map', '[out]', '-map', f'{audio_idx}:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]


def _build_waveform_cmd(audio_path, output_path, background_path, duration, track_id, encode):
    """WAVEFORM — light CPU, wave bars at bottom + logo."""
    # Get track title
    try:
        from modules.database import get_db as _gdb
        _c = _gdb()
        _t = _c.execute('SELECT title FROM tracks WHERE id = ?', (track_id,)).fetchone()
        title = _t['title'] if _t and _t['title'] else f'Track \\#{track_id}'
    except Exception:
        title = f'Track \\#{track_id}'
    # Escape special chars for ffmpeg drawtext
    title = title.replace("'", "'\\''").replace(":", "\\:")

    has_logo = os.path.exists(LOGO_PATH)

    if background_path:
        inputs = ['-loop', '1', '-i', background_path, '-i', audio_path]
        if has_logo:
            inputs += ['-loop', '1', '-i', LOGO_PATH]

        if has_logo:
            filters = (
                '[2:a]showwaves=s=1920x200:mode=cline:rate=30:colors=0xff4444|0xff6666:scale=sqrt[waves];'
                '[0:v]scale=1920:1080[bg];'
                '[2:v]scale=250:250[logo];'
                f'[bg][logo]overlay=(W-250)/2:(H-250)/2-60:format=auto[bglogo];'
                f'[bglogo][waves]overlay=0:H-220:format=auto,'
                f'drawtext=text=\'{title}\':fontsize=32:fontcolor=white@0.8:'
                f'x=(w-text_w)/2:y=(h/2)+90:shadowx=2:shadowy=2:shadowcolor=black@0.7[out]'
            )
            # audio is input [1] when we have bg[0], audio[1], logo[2]
            filters = filters.replace('[2:a]', '[1:a]')
        else:
            filters = (
                '[1:a]showwaves=s=1920x200:mode=cline:rate=30:colors=0xff4444|0xff6666:scale=sqrt[waves];'
                '[0:v]scale=1920:1080[bg];'
                f'[bg][waves]overlay=0:H-220:format=auto,'
                f'drawtext=text=\'{title}\':fontsize=32:fontcolor=white@0.8:'
                f'x=(w-text_w)/2:y=30:shadowx=2:shadowy=2:shadowcolor=black@0.7[out]'
            )

        return [
            'ffmpeg', *inputs,
            '-filter_complex', filters,
            '-map', '[out]', '-map', '1:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]
    else:
        inputs = ['-f', 'lavfi', '-i', f'color=c=0x0a0a0f:s=1920x1080:d={int(duration + 1)}', '-i', audio_path]
        if has_logo:
            inputs += ['-loop', '1', '-i', LOGO_PATH]

        if has_logo:
            filters = (
                '[1:a]showwaves=s=1920x200:mode=cline:rate=30:colors=0xff4444|0xff6666:scale=sqrt[waves];'
                '[2:v]scale=250:250[logo];'
                f'[0:v][logo]overlay=(W-250)/2:(H-250)/2-60:format=auto[bglogo];'
                f'[bglogo][waves]overlay=0:H-220:format=auto,'
                f'drawtext=text=\'{title}\':fontsize=32:fontcolor=white@0.8:'
                f'x=(w-text_w)/2:y=(h/2)+90:shadowx=2:shadowy=2:shadowcolor=black@0.7[out]'
            )
        else:
            filters = (
                '[1:a]showwaves=s=1920x200:mode=cline:rate=30:colors=0xff4444|0xff6666:scale=sqrt[waves];'
                f'[0:v][waves]overlay=0:H-220:format=auto,'
                f'drawtext=text=\'{title}\':fontsize=32:fontcolor=white@0.8:'
                f'x=(w-text_w)/2:y=30:shadowx=2:shadowy=2:shadowcolor=black@0.7[out]'
            )

        return [
            'ffmpeg', *inputs,
            '-filter_complex', filters,
            '-map', '[out]', '-map', '1:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]


def _build_bars_cmd(audio_path, output_path, background_path, duration, track_id, encode):
    """FREQUENCY BARS — medium CPU."""
    if background_path:
        return [
            'ffmpeg',
            '-loop', '1', '-i', background_path,
            '-i', audio_path,
            '-filter_complex',
            '[1:a]showfreqs=s=1920x300:mode=bar:ascale=log:fscale=lin:win_size=2048:'
            'colors=0xff4444|0xff8800[freq];'
            '[freq]colorkey=black:0.1:0.2[freqt];'
            '[0:v]scale=1920:1080[bg];'
            f'[bg][freqt]overlay=0:H-320:format=auto,'
            f'drawtext=text=\'Track \\#{track_id}\':fontsize=36:fontcolor=white@0.3:'
            f'x=(w-text_w)/2:y=30[out]',
            '-map', '[out]', '-map', '1:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]
    else:
        return [
            'ffmpeg',
            '-f', 'lavfi', '-i', f'color=c=0x0a0a0f:s=1920x1080:d={int(duration + 1)}',
            '-i', audio_path,
            '-filter_complex',
            '[1:a]showfreqs=s=1920x300:mode=bar:ascale=log:fscale=lin:win_size=2048:'
            'colors=0xff4444|0xff8800[freq];'
            '[freq]colorkey=black:0.1:0.2[freqt];'
            f'[0:v][freqt]overlay=0:H-320:format=auto,'
            f'drawtext=text=\'Track \\#{track_id}\':fontsize=36:fontcolor=white@0.3:'
            f'x=(w-text_w)/2:y=30[out]',
            '-map', '[out]', '-map', '1:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]


def _build_scope_cmd(audio_path, output_path, background_path, duration, track_id, encode):
    """VECTOR SCOPE — heavy CPU, most visual."""
    if background_path:
        return [
            'ffmpeg',
            '-loop', '1', '-i', background_path,
            '-i', audio_path,
            '-filter_complex',
            '[1:a]avectorscope=s=600x600:mode=lissajous_xy:rate=30:rc=255:gc=68:bc=68[scope];'
            '[scope]colorkey=black:0.15:0.1[scopet];'
            '[0:v]scale=1920:1080[bg];'
            f'[bg][scopet]overlay=(W-600)/2:(H-600)/2:format=auto,'
            f'drawtext=text=\'Track \\#{track_id}\':fontsize=24:fontcolor=white@0.3:'
            f'x=30:y=H-50[out]',
            '-map', '[out]', '-map', '1:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]
    else:
        return [
            'ffmpeg',
            '-f', 'lavfi', '-i', f'color=c=0x0a0a0f:s=1920x1080:d={int(duration + 1)}',
            '-i', audio_path,
            '-filter_complex',
            '[1:a]avectorscope=s=600x600:mode=lissajous_xy:rate=30:rc=255:gc=68:bc=68[scope];'
            '[scope]colorkey=black:0.15:0.1[scopet];'
            f'[0:v][scopet]overlay=(W-600)/2:(H-600)/2:format=auto,'
            f'drawtext=text=\'Track \\#{track_id}\':fontsize=24:fontcolor=white@0.3:'
            f'x=30:y=H-50[out]',
            '-map', '[out]', '-map', '1:a',
            '-t', str(int(duration)),
            *encode, output_path,
        ]


def _build_aurora_cmd(audio_path, output_path, duration, track_id, encode):
    """
    AURORA style — neon glowing waves on black background.
    Multiple showwaves layers with blur/glow, vignette, particles-like noise.
    Looks like EMPIRE PHONK / aura visualizations.
    """
    # Pick random aurora color theme
    aurora_themes = [
        # (wave_color, glow_color, accent) — magenta/purple
        ('0xff00ff', '0xcc44ff', '0xff66ff'),
        # cyan/blue
        ('0x00ffff', '0x3399ff', '0x66ffff'),
        # fire/orange
        ('0xff5500', '0xffcc00', '0xff8800'),
        # toxic green
        ('0x00ff66', '0x66ff00', '0x44ffaa'),
        # pink/rose
        ('0xff0088', '0xff44aa', '0xff66cc'),
    ]
    colors = random.choice(aurora_themes)

    has_logo = os.path.exists(LOGO_PATH)

    inputs = [
        'ffmpeg',
        '-f', 'lavfi', '-i', f'color=c=0x020204:s=1920x1080:d={int(duration + 1)}:rate=30',
        '-i', audio_path,
    ]
    if has_logo:
        inputs += ['-loop', '1', '-i', LOGO_PATH]

    audio_idx = 1
    logo_idx = 2 if has_logo else None

    filters = []

    # Layer 1: Main wave (thick, centered)
    filters.append(
        f'[{audio_idx}:a]showwaves=s=1920x400:mode=cline:rate=30:'
        f'colors={colors[0]}:scale=sqrt[wave1]'
    )

    # Layer 2: Secondary wave (offset phase via different size)
    filters.append(
        f'[{audio_idx}:a]showwaves=s=1920x300:mode=p2p:rate=30:'
        f'colors={colors[1]}:scale=cbrt[wave2]'
    )

    # Layer 3: Thin accent wave
    filters.append(
        f'[{audio_idx}:a]showwaves=s=1920x200:mode=cline:rate=30:'
        f'colors={colors[2]}:scale=lin[wave3]'
    )

    # Glow layer: blur the main wave heavily for glow effect
    filters.append(
        '[wave1]split[w1a][w1b];'
        '[w1b]gblur=sigma=25[glow1]'
    )

    # Composite: black bg + glow (additive-like via overlay)
    filters.append(
        '[glow1]colorkey=black:0.05:0.15[glow1t];'
        '[0:v][glow1t]overlay=0:(H-400)/2:format=auto[bg_glow]'
    )

    # Add main wave on top
    filters.append(
        '[w1a]colorkey=black:0.05:0.1[w1t];'
        '[bg_glow][w1t]overlay=0:(H-400)/2:format=auto[bg_w1]'
    )

    # Add secondary wave (slightly offset)
    filters.append(
        '[wave2]colorkey=black:0.05:0.1[w2t];'
        '[bg_w1][w2t]overlay=0:(H-300)/2+30:format=auto[bg_w2]'
    )

    # Add accent wave
    filters.append(
        '[wave3]colorkey=black:0.06:0.1[w3t];'
        '[bg_w2][w3t]overlay=0:(H-200)/2-20:format=auto[bg_w3]'
    )

    # Add second glow pass (softer, wider) for depth
    filters.append(
        f'[{audio_idx}:a]showwaves=s=1920x500:mode=cline:rate=30:'
        f'colors={colors[0]}:scale=sqrt[waveglow2];'
        '[waveglow2]gblur=sigma=50,colorkey=black:0.08:0.2[glow2t];'
        '[bg_w3][glow2t]overlay=0:(H-500)/2:format=auto[bg_glow2]'
    )

    # Vignette for dark edges
    prev_label = 'bg_glow2'
    filters.append(f'[{prev_label}]vignette=PI/2.5[vig]')
    prev_label = 'vig'

    # Logo (small, centered, semi-transparent)
    if has_logo:
        filters.append(
            f'[{logo_idx}:v]scale=180:180,format=rgba,'
            f'colorchannelmixer=aa=0.4[logofade];'
            f'[{prev_label}][logofade]overlay=(W-180)/2:40:format=auto[withlogo]'
        )
        prev_label = 'withlogo'

    # Track title text
    try:
        from modules.database import get_db as _gdb
        _c = _gdb()
        _t = _c.execute('SELECT title FROM tracks WHERE id = ?', (track_id,)).fetchone()
        title = _t['title'] if _t and _t['title'] else f'Track \\#{track_id}'
    except Exception:
        title = f'Track \\#{track_id}'
    title = title.replace("'", "'\\''").replace(":", "\\:")

    filters.append(
        f'[{prev_label}]drawtext=text=\'{title}\':'
        f'fontsize=30:fontcolor=white@0.5:'
        f'x=(w-text_w)/2:y=h-60:font=DejaVu Sans Bold[out]'
    )

    filter_str = ';'.join(filters)

    return [
        *inputs,
        '-filter_complex', filter_str,
        '-map', '[out]', '-map', f'{audio_idx}:a',
        '-t', str(int(duration)),
        *encode, output_path,
    ]


# ═══════════════════════════════════════
# MAIN RENDER FUNCTION
# ═══════════════════════════════════════

def render_video(track_id):
    """
    Render video for a track. Returns video_path on success, None on failure.
    Creates a record in the videos table.
    """
    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        log_error(f"Render: track #{track_id} not found")
        return None

    audio_path = track['audio_path']
    if not audio_path or not os.path.exists(audio_path):
        log_error(f"Render: audio not found — {audio_path}")
        return None

    # Create video record
    cursor = conn.execute(
        "INSERT INTO videos (track_id, status) VALUES (?, 'rendering')",
        (track_id,)
    )
    conn.commit()
    video_id = cursor.lastrowid

    # Prepare paths
    video_path = os.path.join(OUTPUT_DIR, 'videos', f'video_{track_id}.mp4')
    os.makedirs(os.path.dirname(video_path), exist_ok=True)

    # Get config
    style = get_config('visualization_style', 'phonk')
    background = _get_background()
    duration = track['duration_seconds'] or get_audio_duration(audio_path)

    log(f"Render: track #{track_id}, style={style}, bg={'yes' if background else 'none'}, duration={duration:.0f}s")

    # Build and run FFmpeg
    cmd = _build_ffmpeg_cmd(audio_path, video_path, style, background, duration, track_id)

    try:
        # Timeout: duration * 20 (generous for Pi — encoding is slow)
        timeout = max(int(duration * 20), 600)
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            error_msg = result.stderr[-500:] if result.stderr else 'Unknown FFmpeg error'
            log_error(f"Render: FFmpeg failed — {error_msg}")
            conn.execute(
                "UPDATE videos SET status = 'failed', error_message = ? WHERE id = ?",
                (error_msg[:1000], video_id)
            )
            conn.commit()
            return None

        # Verify output
        if not os.path.exists(video_path) or os.path.getsize(video_path) < 100 * 1024:
            log_error("Render: output file missing or too small")
            conn.execute("UPDATE videos SET status = 'failed', error_message = 'Output too small' WHERE id = ?", (video_id,))
            conn.commit()
            return None

        video_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        log(f"Render: success — {video_size_mb:.1f}MB -> {video_path}")

        conn.execute(
            "UPDATE videos SET status = 'ready', video_path = ?, duration_seconds = ? WHERE id = ?",
            (video_path, duration, video_id)
        )
        conn.commit()

        # Generate thumbnail
        thumb_path = generate_thumbnail(track_id, video_path)
        if thumb_path:
            conn.execute("UPDATE videos SET thumbnail_path = ? WHERE id = ?", (thumb_path, video_id))
            conn.commit()

        return video_path

    except subprocess.TimeoutExpired:
        log_error(f"Render: FFmpeg timeout after {timeout}s")
        conn.execute("UPDATE videos SET status = 'failed', error_message = 'FFmpeg timeout' WHERE id = ?", (video_id,))
        conn.commit()
        try:
            subprocess.run(['pkill', '-f', f'video_{track_id}.mp4'], timeout=5)
        except:
            pass
        return None

    except Exception as e:
        log_error(f"Render: unexpected error — {e}")
        conn.execute("UPDATE videos SET status = 'failed', error_message = ? WHERE id = ?", (str(e)[:1000], video_id))
        conn.commit()
        return None


def generate_thumbnail(track_id, video_path):
    """Generate thumbnail — try AI (Gemini) first, fallback to video frame."""
    thumb_path = os.path.join(OUTPUT_DIR, 'thumbnails', f'thumb_{track_id}.jpg')
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)

    # Try AI-generated thumbnail first
    ai_thumb = _generate_ai_thumbnail(track_id, thumb_path)
    if ai_thumb:
        return ai_thumb

    # Fallback: extract frame from video
    try:
        duration = get_audio_duration(video_path)
        seek_time = max(duration * 0.1, 1)

        result = subprocess.run([
            'ffmpeg', '-ss', str(int(seek_time)),
            '-i', video_path,
            '-vframes', '1',
            '-vf', 'scale=1280:720',
            '-q:v', '2',
            '-y', thumb_path,
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and os.path.exists(thumb_path):
            log(f"Thumbnail: fallback frame for track #{track_id}")
            _add_thumbnail_text(thumb_path, track_id)
            return thumb_path

    except Exception as e:
        log_error(f"Thumbnail: error — {e}")

    return None


def _generate_ai_thumbnail(track_id, thumb_path):
    """Generate a phonk-style thumbnail using Gemini image generation."""
    from modules.database import get_db as _get_db, get_config as _get_config

    api_key = _get_config('gemini_api_key')
    if not api_key:
        return None

    try:
        conn = _get_db()
        track = conn.execute('SELECT title, suno_prompt, suno_style FROM tracks WHERE id = ?', (track_id,)).fetchone()
        title = track['title'] if track and track['title'] else f'Phonk Beat #{track_id}'
        style = track['suno_style'] if track else 'phonk'
        prompt_info = track['suno_prompt'] if track else ''
    except Exception:
        title = f'Phonk Beat #{track_id}'
        style = 'phonk'
        prompt_info = ''

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        thumb_prompt = (
            f"Create a YouTube thumbnail for a phonk music track. "
            f"Style: dark, aggressive, neon colors (purple, cyan, red), urban/racing aesthetic. "
            f"Include visual elements related to: {style}. "
            f"Dark background with glowing neon effects, maybe a car silhouette, city skyline, or abstract bass waveforms. "
            f"The image should look like a professional phonk/drift music YouTube thumbnail. "
            f"Bold cinematic composition, high contrast, 1280x720 resolution. "
            f"DO NOT include any text or letters in the image."
        )

        response = client.models.generate_content(
            model='gemini-2.5-flash-image',
            contents=thumb_prompt,
            config=types.GenerateContentConfig(
                response_modalities=['IMAGE', 'TEXT'],
            ),
        )

        # Extract image from response
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith('image/'):
                image_data = part.inline_data.data

                # Save raw image
                raw_path = thumb_path.replace('.jpg', '_raw.png')
                with open(raw_path, 'wb') as f:
                    f.write(image_data)

                # Resize to 1280x720 + dark overlay + neon title + logo
                from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

                img = Image.open(raw_path).convert('RGBA')
                img = img.resize((1280, 720), Image.LANCZOS)

                # 1. Darken background (40% opacity dark overlay)
                dark_overlay = Image.new('RGBA', (1280, 720), (0, 0, 0, 120))
                img = Image.alpha_composite(img, dark_overlay)

                # 2. Add gradient vignette (darker edges)
                vignette = Image.new('RGBA', (1280, 720), (0, 0, 0, 0))
                vdraw = ImageDraw.Draw(vignette)
                for i in range(80):
                    alpha = int(180 * (1 - i / 80))
                    vdraw.rectangle([i, i, 1280 - i, 720 - i], outline=(0, 0, 0, alpha))
                img = Image.alpha_composite(img, vignette)

                img = img.convert('RGB')
                draw = ImageDraw.Draw(img)

                # 3. Add logo centered
                if os.path.exists(LOGO_PATH):
                    try:
                        logo = Image.open(LOGO_PATH).convert('RGBA')
                        logo.thumbnail((280, 280), Image.LANCZOS)
                        img_rgba = img.convert('RGBA')
                        lx = (1280 - logo.width) // 2
                        ly = 160
                        img_rgba.paste(logo, (lx, ly), logo)
                        img = img_rgba.convert('RGB')
                        draw = ImageDraw.Draw(img)
                    except Exception:
                        pass

                # 4. Big catchy title (neon style)
                display_title = title[:35].upper() if len(title) > 35 else title.upper()

                try:
                    font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
                except Exception:
                    font_big = ImageFont.load_default()

                bbox = draw.textbbox((0, 0), display_title, font=font_big)
                text_w = bbox[2] - bbox[0]
                text_x = (1280 - text_w) // 2
                text_y = 480

                # Glow effect (multiple shadow layers)
                for dx, dy in [(-3,-3),(3,-3),(-3,3),(3,3),(0,-4),(0,4),(-4,0),(4,0)]:
                    draw.text((text_x+dx, text_y+dy), display_title, fill=(0, 80, 80), font=font_big)
                # Shadow
                draw.text((text_x+2, text_y+2), display_title, fill=(0, 0, 0), font=font_big)
                # Main text (neon cyan)
                draw.text((text_x, text_y), display_title, fill=(0, 255, 255), font=font_big)

                # 5. Subtitle (style tags)
                try:
                    font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
                except Exception:
                    font_small = ImageFont.load_default()

                subtitle = style[:60] if style else 'PHONK'
                sbbox = draw.textbbox((0, 0), subtitle, font=font_small)
                sw = sbbox[2] - sbbox[0]
                draw.text(((1280 - sw) // 2, 560), subtitle, fill=(200, 200, 200, 180), font=font_small)

                img.save(thumb_path, quality=92)

                # Clean up raw
                try:
                    os.remove(raw_path)
                except Exception:
                    pass

                log(f"Thumbnail: AI generated for track #{track_id}")
                return thumb_path

        log_warning("Thumbnail: Gemini returned no image")
        return None

    except Exception as e:
        log_error(f"Thumbnail: AI generation error — {e}")
        return None


def _add_thumbnail_text(thumb_path, track_id):
    """Add logo + track number overlay to thumbnail using Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(thumb_path).convert('RGBA')

        # Try logo first
        if os.path.exists(LOGO_PATH):
            try:
                logo = Image.open(LOGO_PATH).convert('RGBA')
                # Resize logo to fit (max 300px)
                logo_size = 300
                logo.thumbnail((logo_size, logo_size), Image.LANCZOS)
                # Center logo
                lx = (1280 - logo.width) // 2
                ly = (720 - logo.height) // 2 - 40
                img.paste(logo, (lx, ly), logo)
            except Exception:
                pass

        # Track number below logo
        draw = ImageDraw.Draw(img)
        try:
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except:
            font_small = ImageFont.load_default()

        # Use track title from DB if available, fallback to Track #ID
        try:
            from modules.database import get_db as _get_db
            _conn = _get_db()
            _track = _conn.execute('SELECT title FROM tracks WHERE id = ?', (track_id,)).fetchone()
            sub = _track['title'] if _track and _track['title'] else f"Track #{track_id}"
        except Exception:
            sub = f"Track #{track_id}"
        bbox2 = draw.textbbox((0, 0), sub, font=font_small)
        sub_w = bbox2[2] - bbox2[0]
        ty = (720 + 300) // 2 - 20  # Below logo
        draw.text(((1280 - sub_w) // 2, ty), sub, fill=(200, 200, 200), font=font_small)

        img = img.convert('RGB')
        img.save(thumb_path, quality=90)

    except ImportError:
        pass  # No Pillow — use raw frame
    except Exception:
        pass
