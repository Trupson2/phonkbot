"""Phonk prompt + style templates for Suno generation.

Picked at random per pipeline tick. Rejection feedback (from training_data)
is appended via Gemini if available — closes the loop on what NOT to make.
"""

import random

from modules.database import get_config, get_db
from modules.logger import log, log_error


PHONK_PROMPTS = [
    # Drift Phonk
    "dark aggressive drift phonk beat, heavy distorted 808 bass, Memphis rap vocal chops, cowbell pattern, high energy racing vibe",
    "hard drift phonk, deep sub bass, chopped soul samples, aggressive cowbell, dark atmosphere, night driving energy",
    "phonk drift beat, distorted 808s, dark piano melody, Memphis vocal samples, cowbell hits, underground racing mood",
    "aggressive phonk with heavy bass drops, cowbell rhythm, pitched down vocals, dark synth melody, drift car energy",
    "drift phonk instrumental, hard hitting 808 bass, fast cowbell pattern, dark orchestral samples, midnight racing aesthetic",

    # Dark Phonk
    "dark phonk beat, evil piano melody, heavy 808 bass, horror movie atmosphere, Memphis rap style, deep and menacing",
    "sinister dark phonk, deep bass, eerie choir samples, slow heavy drums, haunted atmosphere, underground vibe",
    "dark atmospheric phonk, distorted bass, reversed vocals, horror synth pads, slow grinding beat, evil energy",

    # Aggressive Phonk
    "aggressive gym phonk, hard 808 bass, fast tempo, motivational dark energy, cowbell hits, intense workout beat",
    "hard phonk beat, maximum distortion, aggressive energy, fast cowbell, screaming vocal chops, fight music vibe",
    "intense aggressive phonk, heavy bass drops, rapid cowbell, distorted kicks, raw underground energy, no mercy",
    "brutal phonk instrumental, crushing 808s, relentless cowbell, dark vocal samples, combat sports energy",

    # Chill Phonk / Phonk House
    "chill phonk beat, smooth bass line, lo-fi atmosphere, relaxed cowbell, night city driving, aesthetic vibe",
    "phonk house instrumental, groovy bass, funky samples, cowbell groove, late night cruising mood, smooth energy",
    "laid back phonk, deep bass, vintage soul samples, relaxed tempo, night drive aesthetic, smooth and dark",

    # Memphis Phonk
    "Memphis phonk revival, triple six style, dark trap beats, heavy 808, chopped vocals, underground Memphis sound",
    "old school Memphis phonk, lo-fi production, dark samples, heavy bass, classic phonk cowbell, raw underground",

    # Brazilian Phonk
    "Brazilian phonk funk, heavy bass, rave energy, MC vocal chops, aggressive beat, high tempo party phonk",
    "hard Brazilian phonk, funk bass line, rave synths, cowbell, high energy dance phonk, club banger",

    # Experimental
    "experimental phonk, orchestral strings mixed with heavy 808 bass, cinematic dark atmosphere, epic cowbell drops",
    "phonk with guitar riffs, heavy metal meets 808 bass, aggressive energy, cowbell, dark rock phonk fusion",
]

PHONK_STYLES = [
    "phonk, drift phonk, dark, aggressive, 808 bass, cowbell",
    "phonk, dark phonk, Memphis, underground, heavy bass",
    "drift phonk, racing, aggressive, hard bass, cowbell",
    "phonk, gym music, workout, aggressive, motivational",
    "chill phonk, night drive, aesthetic, lo-fi, smooth bass",
    "Memphis phonk, triple six, dark trap, underground",
    "Brazilian phonk, funk, rave, high energy, party",
    "phonk, experimental, cinematic, orchestral, dark",
]

_MODIFIERS = [
    ", 150 BPM", ", 140 BPM", ", 160 BPM",
    ", hard hitting", ", maximum energy", ", dark vibes",
    ", street racing", ", underground", ", raw and unfiltered",
    "", "", "",
]


def generate_suno_prompt():
    """Return (prompt, style) — random picks plus learned reject feedback."""
    prompt = random.choice(PHONK_PROMPTS) + random.choice(_MODIFIERS)
    style = random.choice(PHONK_STYLES)

    feedback = _get_reject_feedback()
    if feedback:
        prompt += f", {feedback}"

    return prompt, style


def _get_reject_feedback():
    """Ask Gemini to summarize patterns from recent rejections (max 30 words)."""
    try:
        conn = get_db()
        rejects = conn.execute('''
            SELECT t.suno_prompt, td.reject_reason
            FROM training_data td
            JOIN tracks t ON t.id = td.track_id
            WHERE td.rating = 0 AND td.reject_reason IS NOT NULL
            ORDER BY td.created_at DESC LIMIT 10
        ''').fetchall()

        if not rejects:
            return None

        gemini_key = get_config('gemini_api_key')
        if not gemini_key:
            return None

        from google import genai

        client = genai.Client(api_key=gemini_key)
        feedback_lines = [
            f"- Prompt: {r['suno_prompt'][:100]} | Reason: {r['reject_reason']}"
            for r in rejects
        ]
        gemini_prompt = (
            "You help improve AI music generation. Below are recently REJECTED phonk tracks "
            "with reasons (in Polish).\n\n"
            + "\n".join(feedback_lines)
            + "\n\nBased on these rejections, write a SHORT (max 30 words) English instruction "
            "for the music AI about what to AVOID or IMPROVE.\n"
            'Example: "avoid slow tempo, use more aggressive 808 bass, less piano"\n'
            "Return ONLY the instruction, no explanation."
        )

        response = client.models.generate_content(
            model=get_config('gemini_model', 'gemini-2.0-flash'),
            contents=gemini_prompt,
        )
        result = response.text.strip().strip('"')
        log(f"Reject feedback applied: {result}")
        return result

    except Exception as e:
        log_error(f"Reject feedback analysis failed: {e}")
        return None
