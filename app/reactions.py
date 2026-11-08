"""Jev's language judgments and the application's reaction policy."""
import math

import httpx

from app.service import redact_links


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self.client, self.api_key = client, api_key

    def payload(self, message, history, agent):
        # This pure method exposes the exact request for inspection without logging private text.
        return {
            'model': 'jev-1.14.0',
            'state': {'message': redact_links(message),
                      'history': [{**m, 'content': redact_links(m['content'])} for m in history],
                      'agent': {k: redact_links(v) for k, v in agent.items()}},
            'questions': {
                'fits': {'type': 'noul',
                         'instructions': 'Does a brief emoji acknowledgment fit the tone of `message` in this conversation?',
                         'criteria': {'true': 'An acknowledgment of attention, agreement, or warmth feels natural.',
                                      'false': 'A reaction would feel dismissive, confusing, or inappropriate.'}},
                'meaning': {'type': 'choice',
                            'instructions': 'Which acknowledgment best fits `message` from this agent?',
                            'criteria': {'taking_a_look': 'Taking a look or working on the request before answering.',
                                         'acknowledgment': 'Understood, agreed, or received.',
                                         'warmth': 'Warm appreciation or affection.'}},
                'social': {'type': 'noul',
                           'instructions': 'Is `message` purely a social acknowledgment?',
                           'criteria': {'true': 'Thanks, praise, or acknowledgment with no question or requested action.',
                                        'false': 'A question, request, instruction, or agreement to a proposed action that still needs doing.'}},
            },
        }

    async def decide(self, message, history, agent):
        try:
            response = await self.client.post('https://api.typesafe.ai/v1/systemone',
                headers={'Authorization': f'Bearer {self.api_key}'},
                json=self.payload(message, history, agent), timeout=3, follow_redirects=False)
            if not response.is_success:
                raise JevError(f'JEV_HTTP_{response.status_code}')
            answers = response.json()['answers']
            fits, social = (answers[name]['noul'] for name in ('fits', 'social'))
            if any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in (fits, social)):
                raise ValueError()
            emoji = {'taking_a_look': '👀', 'acknowledgment': '👍', 'warmth': '❤️'}[answers['meaning']['choice']]
            return {'status': 'decided', 'emoji': emoji if fits > .7 else None,
                    'only': fits > .7 and social > .7 and emoji != '👀'}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise JevError('JEV_REQUEST_FAILED') from None
