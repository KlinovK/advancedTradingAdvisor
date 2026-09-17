from advanced_trading_advisor.__main__ import main


def test_entrypoint_reports_help_without_creating_database(capsys):
    assert main([]) == 0
    assert "setup" in capsys.readouterr().out
