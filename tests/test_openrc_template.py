from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "openrc" / "nighty"


def test_openrc_reference_service_has_supervision_and_dependencies():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.startswith("#!/sbin/openrc-run\n")
    assert 'supervisor="supervise-daemon"' in text
    assert "respawn_delay=5" in text
    assert "respawn_max=0" in text
    assert "need net" in text
    assert "after firewall" in text
    assert 'command_user="<USER>"' in text
    assert 'directory="<REPO_DIR>"' in text


def test_openrc_reference_service_prepares_diagnostics_directory():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "start_pre()" in text
    assert "checkpath -d" in text
    assert "<REPO_DIR>/diagnostics" in text
