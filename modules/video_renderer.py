"""
Video renderer for PhonkBot — FFmpeg-based audio visualizations.
Three styles: waveform (light CPU), bars (medium), scope (heavy).
Optimized for Raspberry Pi 5.
"""

import os
import json
import random
import subprocess
from modules.logger import log, log_error
from modules.database import get_db, get_config

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKGROUNDS_DIR = os.path.join(_APP_DIR, 'static', 'assets', 'backgrounds')
OUTPUT_DIR = os.path.join(_APP_DIR, 'output')


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
        # Duration
        features['duration'] = get_audio_duration(audio_path)

        # Loudness (LUFS) + peak
        result = subprocess.run(
            ['ffmpeg', '-i', audio_path, '-af', 'loudnorm=print_format=json', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=30,
        )
        stderr = result.stderr
        # Parse loudnorm JSON from stderr
        json_start = stderr.rfind('{')
        json_end = stderr.rfind('}') + 1
        if json_start > 0 and json_end > json_start:
            loudnorm = json.loads(stderr[json_start:json_end])
            features['input_i'] = float(loudnorm.get('input_i', 0))
            features['input_tp'] = float(loudnorm.get('input_tp', 0))
            features['input_lra'] = float(loudnorm.get('input_lra', 0))

        # File size
        features['file_size_kb'] = os.path.getsize(audio_path) // 1024

    except Exception as e:
        features['error'] = str(e)

    return json.dumps(features)


def _get_background():
    """Pick a random background image, or generate a black frame if none exist."""
    if os.path.isdir(BACKGROUNDS_DIR):
        imgs = [f for f in os.listdir(BACKGROUNDS_DIR)
                if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        if imgs:
            return os.path.join(BACKGROUNDS_DIR, random.choice(imgs))

    # No backgrounds — use solid black
    return None


def _build_ffmpeg_cmd(audio_path, output_path, style, background_path, duration):
    """Build FFmpeg command based on visualization style."""

    # Base encoding settings (optimized for Pi 5)
    encode = [
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
        '-tune', 'stillimage', '-threads', '4',
        '-c:a', 'aac', '-b:a', '192k',
        '-shortest', '-y',
    ]

    if style == 'waveform':
        # ═══ WAVEFORM — light CPU, bars at bottom ═══
        if background_path:
            return [
                'ffmpeg',
                '-loop', '1', '-i', background_path,
                '-i', audio_path,
                '-filter_complex',
                '[1:a]showwaves=s=1920x200:mode=cline:rate=30:colors=0xff4444|0xff6666:scale=sqrt[waves];'
                '[0:v]scale=1920:1080[bg];'
                '[bg][waves]overlay=0:H-220:format=auto[out]',
                '-map', '[out]', '-map', '1:a',
                *encode, output_path,
            ]
        else:
            return [
                'ffmpeg',
                '-f', 'lavfi', '-i', 'color=c=black:s=1920x1080:d={}'.format(int(duration + 1)),
                '-i', audio_path,
                '-filter_complex',
                '[1:a]showwaves=s=1920x200:mode=cline:rate=30:colors=0xff4444|0xff6666:scale=sqrt[waves];'
                '[0:v][waves]overlay=0:H-220:format=auto[out]',
                '-map', '[out]', '-map', '1:a',
                *encode, output_path,
            ]

    elif style == 'bars':
        # ═══ FREQUENCY BARS — medium CPU ═══
        if background_path:
            return [
                'ffmpeg',
                '-loop', '1', '-i', background_path,
                '-i', audio_path,
                '-filter_complex',
                '[1:a]showfreqs=s=1920x300:mode=bar:ascale=log:fscale=lin:win_size=2048:colors=0xff4444|0xff8800[freq];'
                '[freq]colorkey=black:0.1:0.2[freqt];'
                '[0:v]scale=1920:1080[bg];'
                '[bg][freqt]overlay=0:H-320:format=auto,'
                'drawtext=text=PHONKBOT:fontsize=42:fontcolor=white@0.15:x=(w-text_w)/2:y=30[out]',
                '-map', '[out]', '-map', '1:a',
                *encode, output_path,
            ]
        else:
            return [
                'ffmpeg',
                '-f', 'lavfi', '-i', 'color=c=0x0a0a0f:s=1920x1080:d={}'.format(int(duration + 1)),
                '-i', audio_path,
                '-filter_complex',
                '[1:a]showfreqs=s=1920x300:mode=bar:ascale=log:fscale=lin:win_size=2048:colors=0xff4444|0xff8800[freq];'
                '[freq]colorkey=black:0.1:0.2[freqt];'
                '[0:v][freqt]overlay=0:H-320:format=auto,'
                'drawtext=text=PHONKBOT:fontsize=42:fontcolor=white@0.15:x=(w-text_w)/2:y=30[out]',
                '-map', '[out]', '-map', '1:a',
                *encode, output_path,
            ]

    elif style == 'scope':
        # ═══ VECTOR SCOPE — heavy CPU, most visual ═══
        if background_path:
            return [
                'ffmpeg',
                '-loop', '1', '-i', background_path,
                '-i', audio_path,
                '-filter_complex',
                '[1:a]avectorscope=s=600x600:mode=lissajous_xy:rate=30:rc=255:gc=68:bc=68[scope];'
                '[scope]colorkey=black:0.15:0.1[scopet];'
                '[0:v]scale=1920:1080[bg];'
                '[bg][scopet]overlay=(W-600)/2:(H-600)/2:format=auto,'
                'drawtext=text=%{{pts\\:hms}}:fontsize=24:fontcolor=white@0.3:x=30:y=H-50[out]',
                '-map', '[out]', '-map', '1:a',
                *encode, output_path,
            ]
        else:
            return [
                'ffmpeg',
                '-f', 'lavfi', '-i', 'color=c=0x0a0a0f:s=1920x1080:d={}'.format(int(duration + 1)),
                '-i', audio_path,
                '-filter_complex',
                '[1:a]avectorscope=s=600x600:mode=lissajous_xy:rate=30:rc=255:gc=68:bc=68[scope];'
                '[scope]colorkey=black:0.15:0.1[scopet];'
                '[0:v][scopet]overlay=(W-600)/2:(H-600)/2:format=auto,'
                'drawtext=text=%{{pts\\:hms}}:fontsize=24:fontcolor=white@0.3:x=30:y=H-50[out]',
                '-map', '[out]', '-map', '1:a',
                *encode, output_path,
            ]

    # Fallback to waveform
    return _build_ffmpeg_cmd(audio_path, output_path, 'waveform', background_path, duration)


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
    style = get_config('visualization_style', 'waveform')
    background = _get_background()
    duration = track['duration_seconds'] or get_audio_duration(audio_path)

    log(f"Render: track #{track_id}, style={style}, bg={'yes' if background else 'black'}, duration={duration:.0f}s")

    # Build and run FFmpeg
    cmd = _build_ffmpeg_cmd(audio_path, video_path, style, background, duration)

    try:
        # Timeout: duration * 15 (generous for Pi — encoding is slow)
        timeout = max(int(duration * 15), 300)
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
        log(f"Render: success — {video_size_mb:.1f}MB → {video_path}")

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
        # Kill stuck FFmpeg process
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
    """Extract a frame from the video as thumbnail (1280x720 JPEG)."""
    thumb_path = os.path.join(OUTPUT_DIR, 'thumbnails', f'thumb_{track_id}.jpg')
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)

    try:
        # Get duration and extract frame at 10%
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
            log(f"Thumbnail: generated for track #{track_id}")

            # Overlay text with Pillow if available
            _add_thumbnail_text(thumb_path, track_id)
            return thumb_path

    except Exception as e:
        log_error(f"Thumbnail: error — {e}")

    return None


def _add_thumbnail_text(thumb_path, track_id):
    """Add PHONKBOT text overlay to thumbnail using Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(thumb_path)
        draw = ImageDraw.Draw(img)

        # Try to use a bold font, fall back to default
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except:
            font = ImageFont.load_default()
            font_small = font

        # PHONKBOT title
        text = "PHONKBOT"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        x = (1280 - text_w) // 2
        y = 280

        # Shadow
        draw.text((x + 3, y + 3), text, fill=(0, 0, 0, 180), font=font)
        # Main text
        draw.text((x, y), text, fill=(255, 68, 68), font=font)

        # Track number
        sub = f"Track #{track_id}"
        bbox2 = draw.textbbox((0, 0), sub, font=font_small)
        sub_w = bbox2[2] - bbox2[0]
        draw.text(((1280 - sub_w) // 2, y + 90), sub, fill=(200, 200, 200), font=font_small)

        img.save(thumb_path, quality=90)

    except ImportError:
        pass  # No Pillow — use raw frame
    except Exception:
        pass
