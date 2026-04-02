"""
Metadata generator for PhonkBot — uses Gemini AI for YouTube SEO.
Generates titles, descriptions, and tags optimized for phonk audience.
Falls back to templates if Gemini is not configured.
"""

import json
import random
from modules.logger import log, log_error
from modules.database import get_db, get_config


def generate_metadata(track_id):
    """
    Generate YouTube metadata for a track.
    Uses Gemini if configured, otherwise template fallback.
    Returns dict: {title, description, tags, language}
    """
    conn = get_db()
    track = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not track:
        return _template_metadata(track_id, '', '')

    prompt = track['suno_prompt'] or ''
    style = track['suno_style'] or 'phonk'
    language = get_config('default_language', 'en')

    # Try Gemini first
    gemini_key = get_config('gemini_api_key')
    if gemini_key:
        try:
            metadata = _gemini_metadata(prompt, style, track_id, language, gemini_key)
            if metadata:
                log(f"Metadata: Gemini generated for track #{track_id}")
                return metadata
        except Exception as e:
            log_error(f"Metadata: Gemini error — {e}")

    # Fallback to templates
    log(f"Metadata: using template for track #{track_id}")
    return _template_metadata(track_id, prompt, style, language)


def _gemini_metadata(prompt, style, track_id, language, api_key):
    """Generate metadata using Gemini AI."""
    try:
        from google import genai

        client = genai.Client(api_key=api_key)

        lang_instruction = "in Polish" if language == 'pl' else "in English"

        gemini_prompt = f"""Generate YouTube video metadata for a phonk music track {lang_instruction}.

Track info:
- Music prompt: {prompt}
- Style: {style}
- Track number: {track_id}

Generate a JSON object with:
1. "title" - catchy YouTube title (max 80 chars), use phonk keywords, emojis allowed
2. "description" - YouTube description (200-400 chars) with hashtags, include phonk/drift phonk keywords
3. "tags" - list of 15-20 relevant tags for YouTube SEO (phonk, drift, 808, bass, etc.)

Return ONLY valid JSON, no markdown formatting."""

        response = client.models.generate_content(
            model=get_config('gemini_model', 'gemini-2.0-flash'),
            contents=gemini_prompt,
        )

        text = response.text.strip()
        # Clean markdown formatting if present
        if text.startswith('```'):
            text = text.split('\n', 1)[1] if '\n' in text else text[3:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()
        if text.startswith('json'):
            text = text[4:].strip()

        data = json.loads(text)

        return {
            'title': str(data.get('title', ''))[:100],
            'description': str(data.get('description', '')),
            'tags': data.get('tags', []),
            'language': language,
        }

    except json.JSONDecodeError:
        log_error("Metadata: Gemini returned invalid JSON")
        return None
    except Exception as e:
        log_error(f"Metadata: Gemini API error — {e}")
        return None


def _template_metadata(track_id, prompt='', style='', language='en'):
    """Fallback: generate metadata from templates."""

    if language == 'pl':
        titles = [
            f"PHONK BEAT #{track_id} | Dark Drift Phonk Mix",
            f"AGGRESSIVE PHONK #{track_id} | Heavy 808 Bass",
            f"DRIFT PHONK #{track_id} | Night Drive Mix",
            f"DARK PHONK #{track_id} | Underground Beat",
            f"HARD PHONK #{track_id} | Gym Workout Mix",
        ]
        desc_template = (
            "Phonk beat wygenerowany przez AI\n\n"
            "Subskrybuj po wiecej phonk muzyki!\n\n"
            "#phonk #driftphonk #808bass #darkphonk #phonkmusic "
            "#phonkbeat #underground #drift #nightdrive #aggressive"
        )
    else:
        titles = [
            f"PHONK BEAT #{track_id} | Dark Drift Phonk Mix",
            f"AGGRESSIVE PHONK #{track_id} | Heavy 808 Bass Drop",
            f"DRIFT PHONK #{track_id} | Night Drive Music",
            f"DARK PHONK #{track_id} | Underground Beat",
            f"HARD PHONK #{track_id} | Gym Motivation Mix",
            f"PHONK #{track_id} | Cowbell & 808 Bass",
            f"MIDNIGHT PHONK #{track_id} | Dark Racing Vibes",
        ]
        desc_template = (
            "AI Generated Phonk Music\n\n"
            "Subscribe for daily phonk beats!\n"
            "Like & share if you vibe with this!\n\n"
            "#phonk #driftphonk #808bass #darkphonk #phonkmusic "
            "#phonkbeat #underground #drift #nightdrive #aggressive "
            "#memphisphonk #cowbell #hardphonk #gymmusic #workout"
        )

    # Add prompt-based keywords to description
    if prompt:
        desc_template += f"\n\nStyle: {style}"

    tags = [
        'phonk', 'drift phonk', 'dark phonk', 'aggressive phonk',
        '808 bass', 'cowbell', 'Memphis phonk', 'phonk music',
        'phonk beat', 'underground phonk', 'hard phonk',
        'drift music', 'night drive', 'gym music', 'workout phonk',
        'phonk 2026', 'best phonk', 'new phonk', 'phonk mix',
    ]

    return {
        'title': random.choice(titles),
        'description': desc_template,
        'tags': tags,
        'language': language,
    }
