from gremlin_town.review_card.run import main


def test_disabled_listener_reads_no_credentials_and_starts_no_runtime(tmp_path, monkeypatch, capsys):
    config=tmp_path/'config.toml'
    config.write_text('')
    def forbidden(*args,**kwargs):
        raise AssertionError('disabled listener built a runtime')
    monkeypatch.setattr('gremlin_town.runtime.build_runtime',forbidden)
    assert main(['serve','--config',str(config)])==2
    assert 'off' in capsys.readouterr().err
    assert list(tmp_path.iterdir())==[config]


def test_listener_bad_config_never_reflects_private_value(tmp_path,capsys):
    config=tmp_path/'config.toml'
    config.write_text('[discord]\nprivate = "fictional-do-not-log"')
    assert main(['serve','--config',str(config)])==2
    assert 'fictional-do-not-log' not in capsys.readouterr().err
