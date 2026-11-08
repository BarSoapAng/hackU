"""Explicit live speech/GPU coexistence probe; reports metadata, never transcripts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave


def credential(path, name):
    return next(line.partition('=')[2].strip() for line in path.read_text().splitlines()
                if line.startswith(name + '='))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', required=True, type=Path)
    parser.add_argument('--expected-word', default='bakery')
    parser.add_argument('--base-url', default='http://127.0.0.1:8002')
    parser.add_argument('--env-file', type=Path, default=Path(__file__).with_name('.env'))
    parser.add_argument('--builder-env', type=Path, default=Path.home() / 'hackutd-nemotron/.env')
    args = parser.parse_args()
    speech_key = credential(args.env_file, 'TRANSCRIPTION_API_KEY')
    builder_key = credential(args.builder_env, 'NEMOTRON_API_KEY')

    def request(url, token, body=None, content_type='application/json'):
        headers = {'Content-Type': content_type}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=45) as response:
            return json.load(response)

    for token in (None, 'invalid'):
        try:
            request(args.base_url + '/readyz', token)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError('Unauthenticated speech access accepted')
    assert request(args.base_url + '/readyz', speech_key)['ready']
    print('PASS: speech authentication and readiness', flush=True)

    def transcribe(audio, label):
        started = time.monotonic()
        result = request(args.base_url + '/transcribe', speech_key, audio, 'audio/wav')
        assert result['language'] == 'en' and args.expected_word.casefold() in result['text'].casefold()
        print(json.dumps({'check': label, 'seconds': round(time.monotonic() - started, 2),
                          'audio_seconds': result['duration_seconds'], 'matched': True}), flush=True)

    short = args.audio.read_bytes()
    transcribe(short, 'short_recording')
    with wave.open(io.BytesIO(short), 'rb') as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 16000)
        samples = source.readframes(source.getnframes())
    long = io.BytesIO()
    with wave.open(long, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes((samples * (120 * 32000 // len(samples) + 1))[:120 * 32000])

    def builder(digit):
        started = time.monotonic()
        result = request('http://127.0.0.1:8000/v1/chat/completions', builder_key, json.dumps({
            'model': 'nemotron-builder', 'temperature': 0, 'max_tokens': 128,
            'chat_template_kwargs': {'enable_thinking': False},
            'messages': [{'role': 'user', 'content': 'Ignore this padding text:\n' + 'blue sky. ' * 3800
                          + f'\nReply with only this digit: {digit}'}],
        }).encode())
        assert result['choices'][0]['message']['content'].strip() == str(digit)
        print(json.dumps({'check': 'concurrent_builder', 'seconds': round(time.monotonic() - started, 2)}), flush=True)

    stop, memory, monitoring_errors = threading.Event(), [], []
    def monitor():
        try:
            while not stop.is_set():
                row = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used,memory.free',
                                               '--format=csv,noheader,nounits'], text=True, timeout=5)
                memory.append(tuple(int(value.strip()) for value in row.strip().split(',')))
                stop.wait(.25)
        except Exception as error:
            monitoring_errors.append(type(error).__name__)
    watcher = threading.Thread(target=monitor)
    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(transcribe, long.getvalue(), 'two_minute_recording'),
                       pool.submit(builder, 7), pool.submit(builder, 9)]
            for future in futures:
                future.result()
    finally:
        stop.set()
        watcher.join()
        if memory:
            print(json.dumps({'peak_gpu_used_mib': max(row[0] for row in memory),
                              'minimum_gpu_free_mib': min(row[1] for row in memory)}), flush=True)
    if monitoring_errors or not memory:
        raise RuntimeError('GPU monitoring failed; capacity was not verified')
    print('PASS: speech and builder coexistence', flush=True)


if __name__ == '__main__':
    main()
