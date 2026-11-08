import json

from tools.local import read_environment, update_environment


def test_environment_round_trip_preserves_json_quotes_and_existing_credentials(tmp_path):
    path = tmp_path / '.env'
    path.write_text('# deployment\nOPENAI_API_KEY=existing-key\nENCRYPTION_KEYS=placeholder\n')
    keys = json.dumps({'v1': 'example-base64=='})
    update_environment({'ENCRYPTION_KEYS': keys, 'PHOTON_API_KEY': "a'quoted$value"}, path)
    values = read_environment(path)
    assert json.loads(values['ENCRYPTION_KEYS']) == {'v1': 'example-base64=='}
    assert values['PHOTON_API_KEY'] == "a'quoted$value"
    assert values['OPENAI_API_KEY'] == 'existing-key'
    assert path.read_text().startswith('# deployment\n')


def test_unquoted_json_and_blank_values_do_not_expand_shell_expressions(tmp_path):
    path = tmp_path / '.env'
    path.write_text('EMPTY=\nENCRYPTION_KEYS={"v1":"abc"}\nLITERAL=$(do-not-run)\n')
    values = read_environment(path)
    assert values['EMPTY'] == ''
    assert json.loads(values['ENCRYPTION_KEYS']) == {'v1': 'abc'}
    assert values['LITERAL'] == '$(do-not-run)'
