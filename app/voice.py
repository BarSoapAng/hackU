"""Bounded audio preparation and the private NVIDIA speech client. No content logs."""
import asyncio
import io
import json
import math
import os
from pathlib import Path
import tempfile
import wave

import httpx

from app.service import DomainError


class VoiceError(DomainError):
    def __init__(self, code):
        self.code = code
        super().__init__({
            'oversized': 'That voice note is too large. Please send one under 10 MiB and two minutes, or type your message.',
            'too_long': 'That voice note is too long. Please keep it under two minutes, or type your message.',
            'unreadable': 'I couldn’t read that recording. Please send a new voice note, or type your message.',
            'silence': 'I couldn’t hear any words. Please try recording again, or type your message.',
            'unavailable': 'Voice notes are unavailable right now. Please try again shortly, or type your message.',
            'timeout': 'That voice note took too long to process. Please try a shorter recording, or type your message.',
        }[code])


def audio_directory():
    directory = Path(tempfile.gettempdir()) / 'acorn-voice'
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def clean_audio():
    """Single-process startup only; remove our abandoned files, never directories."""
    for path in audio_directory().glob('note-*'):
        if path.is_file() and path.suffix in ('.audio', '.wav'):
            path.unlink()


def wav_duration(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise VoiceError('unreadable')
            frames = audio.getnframes()
            if frames > 120 * 16000:
                raise VoiceError('too_long')
            samples = audio.readframes(frames)
            if not frames or len(samples) != frames * 2:
                raise VoiceError('unreadable')
            if not any(samples):
                raise VoiceError('silence')
            return frames / 16000
    except (wave.Error, EOFError):
        raise VoiceError('unreadable') from None


async def normalize_audio(source, target):
    try:
        process = await asyncio.create_subprocess_exec(
            os.environ.get('FFMPEG_BINARY', 'ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
            '-max_alloc', '67108864',
            '-protocol_whitelist', 'file,pipe',
            '-format_whitelist', 'aac,amr,caf,flac,mov,mp3,ogg,wav',
            '-threads', '1', '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
            '-t', '120.001', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
            '-threads', '1', '-f', 'wav', str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        raise VoiceError('unavailable') from None
    try:
        async with asyncio.timeout(10):
            if await process.wait():
                raise VoiceError('unreadable')
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if target.stat().st_size > 120 * 16000 * 2 + 4096:
        raise VoiceError('too_long')
    data = target.read_bytes()
    return data, wav_duration(data)


class Transcriber:
    def __init__(self, client, url, credential):
        self.client, self.url, self.credential = client, url.rstrip('/'), credential

    async def transcribe(self, audio):
        async with self.client.stream('POST', self.url + '/transcribe', content=audio,
                headers={'Authorization': 'Bearer ' + self.credential, 'Content-Type': 'audio/wav'},
                timeout=40, follow_redirects=False) as response:
            if response.status_code in (408, 429) or response.status_code >= 500:
                raise httpx.HTTPStatusError('Transcription unavailable', request=response.request, response=response)
            if not response.is_success:
                raise VoiceError('unavailable')
            body = bytearray()
            async for chunk in response.aiter_bytes(65536):
                if len(body) + len(chunk) > 65536:
                    raise VoiceError('unreadable')
                body.extend(chunk)
        try:
            result = json.loads(body)
            text, language, duration = result['text'], result['language'], result['duration_seconds']
            if (not isinstance(text, str) or language != 'en' or type(duration) not in (int, float)
                    or not math.isfinite(duration) or not 0 < duration <= 120):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise VoiceError('unreadable') from None
        if not text.strip():
            raise VoiceError('silence')
        return text.strip()


async def prepare_audio(photon, attachment_id, transcriber):
    source = target = None
    try:
        with tempfile.NamedTemporaryFile(prefix='note-', suffix='.audio', dir=audio_directory(), delete=False) as file:
            source = Path(file.name)
        target = source.with_suffix('.wav')
        metadata = await photon.download_audio(attachment_id, source)
        audio, duration = await normalize_audio(source, target)
        text = await transcriber.transcribe(audio)
        return text, {**metadata, 'duration_seconds': duration, 'language': 'en'}
    finally:
        for path in (source, target):
            if path is not None:
                path.unlink(missing_ok=True)
