from pathlib import Path

import pytest

import app.main


@pytest.mark.parametrize('filename', ['notes.html', 'vault.html'])
def test_notes_template_remains_available_to_running_servers(filename):
    # Processes started before the rename still read vault.html on every request.
    page = Path(app.main.__file__).with_name('static').joinpath(filename).read_text(encoding='utf-8')
    assert 'nonce="__NONCE__"' in page
    assert "fetch('/api/notes'" in page
    assert 'location.hash' in page
    assert 'history.replaceState' in page
