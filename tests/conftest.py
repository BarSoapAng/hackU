import os
import pytest

# Tests use mocked transports; importing Haystack must not send telemetry or
# create a profile outside the workspace.
os.environ['HAYSTACK_TELEMETRY_ENABLED'] = 'false'


@pytest.fixture(autouse=True)
def isolated_voice_directory(monkeypatch, tmp_path):
    # A sandbox-created mode-700 folder can be inaccessible to the real backend.
    # Recovery tests must never create or clean the application's runtime folder.
    def directory():
        path = tmp_path / 'voice'
        path.mkdir(exist_ok=True)
        return path
    monkeypatch.setattr('app.voice.audio_directory', directory)
