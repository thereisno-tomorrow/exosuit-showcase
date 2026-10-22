import asyncio
import hashlib
import json

import pytest

from gremlin_town.contracts import Actor
from gremlin_town.mcp.brain_tools import build_brain_tools


class Runtime:
    async def save_memory(self, caller, selector, request):
        return {'scope': selector.kind, 'source': request.source_reference}

    async def recover_continuity(self, caller, selector, request):
        return {'scope': selector.kind, 'project_id': selector.project_id}

    async def search_memory(self, caller, selector, query):
        return {'scope': selector.kind, 'query': query.query}


def definitions():
    async def allow():
        pass
    return {t.name: t for t in build_brain_tools(Runtime(), lambda: Actor('chatgpt-web'), scopes_provider=lambda: {'bc:read','bc:write'}, authorize=allow)}


def call(name, args):
    return asyncio.run(definitions()[name].run(args))


def test_save_current_user_message_needs_no_magic_reference():
    result = call('bc_memory_save', dict(operation_id='wombat-save', scope='personal', kind='item', level='possible', said_by='owner', came_by='direct', words='I wanna raise a wombat', title='Wombat', body='I wanna raise a wombat'))
    assert result.structuredContent == {'scope':'personal','source':'mcp-request:'+hashlib.sha256(b'wombat-save').hexdigest()}


def test_general_continuity_defaults_to_brain_home():
    result = call('bc_continuity_recover', {})
    assert result.structuredContent == {'scope':'personal','project_id':None}


def test_existing_explicit_project_continuity_still_routes_to_project():
    result = call('bc_continuity_recover', {'project_id':'vault_metavault'})
    assert result.structuredContent == {'scope':'project','project_id':'vault_metavault'}


def test_memory_search_is_available_without_a_record_id():
    result = call('bc_memory_search', {'query':'clanker'})
    assert result.structuredContent == {'scope':'personal','query':'clanker'}


def test_explicit_source_reference_is_not_silently_rewritten():
    result = call('bc_memory_save', dict(operation_id='document-save', scope='personal', kind='item', level='possible', said_by='owner', came_by='direct', words='Body', title='Title', body='Body', source_reference='enrolled-document'))
    assert result.structuredContent['source'] == 'enrolled-document'
