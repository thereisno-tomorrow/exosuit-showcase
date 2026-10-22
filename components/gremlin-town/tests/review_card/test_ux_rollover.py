from datetime import timedelta
import pytest
from .test_card import setup,NOW

@pytest.mark.parametrize('unfinished',[False,True])
def test_new_day_resets_completed_progress_but_keeps_unfinished_review(tmp_path,unfinished):
    engine,counts,fake,log,store=setup(tmp_path,initial=False)
    try:
        old=store.session(NOW.date())
        old.update(screen='day' if unfinished else 'settled',stage='day',
            queue_snapshots={'yesterday':['old-question']},queue_additions={'yesterday':['late-question']},
            skipped=['old-question'],history=[{'screen':'yesterday','item':'old-question'}],
            picks={'fictional-focus':False})
        if unfinished: old['interrupted_review']={'screen':'yesterday','stage':'yesterday','item':'old-question'}
        store.save(NOW.date(),old)
        fresh=store.session(NOW.date()+timedelta(days=1))
        assert fresh['picks']==old['picks']
        if unfinished:
            assert fresh['interrupted_review']==old['interrupted_review']
            assert fresh['queue_snapshots']==old['queue_snapshots']
            assert fresh['queue_additions']==old['queue_additions']
        else:
            assert fresh.get('queue_snapshots',{})=={}
            assert fresh.get('queue_additions',{})=={}
            assert fresh['screen']=='open'
            assert fresh['skipped']==[] and fresh['history']==[]
    finally:
        store.close();log.close()
