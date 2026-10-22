from datetime import timedelta
import pytest
from gremlin_town.mcp.brain_runtime import WorkRequest
from .test_card import setup,tap,content,controls,NOW,WORK

@pytest.mark.anyio
async def test_previous_review_item_shows_saved_answer_without_reapplying_it(tmp_path):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        day=(NOW.date()-timedelta(days=1)).isoformat()
        await engine.backend.change(WorkRequest('previous-answer',day,'owner','direct',(
            {'move':'new','title':'First fictional answer'}, {'move':'date','thing':'#1','kind':'planned','when':day},
            {'move':'new','title':'Second fictional answer'}, {'move':'date','thing':'#2','kind':'planned','when':day})),owner_id=WORK.owner_id)
        result=await tap(engine,await engine.open(),'Start review')
        reviewed=store.session(engine._day())['review_title']
        result=await tap(engine,result,'Made progress',{'value':'Finished the fictional outline; remaining examples are unfinished','remaining':'10'})
        next_title=store.session(engine._day())['review_title']
        before=counts['changes']
        result=await tap(engine,result,'\u2190 Back')
        assert 'Previous item' in content(result)
        assert reviewed in content(result)
        assert 'Finished the fictional outline; remaining examples are unfinished' in content(result)
        assert counts['changes']==before
        assert 'Made progress' not in controls(result) and 'Finished' not in controls(result)
        result=await tap(engine,result,'Continue review')
        assert next_title in content(result)
        assert '2 of 2' in content(result)
        assert counts['changes']==before
    finally:
        store.close();log.close()
