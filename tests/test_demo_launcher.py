from pathlib import Path


def test_demo_launcher_defaults_to_local_bind_and_confirms_lan_exposure():
    script = Path(__file__).resolve().parents[1] / "demo" / "demo-start.sh"
    text = script.read_text(encoding="utf-8")

    assert 'export SIM_HOST_BIND="${SIM_HOST_BIND:-127.0.0.1}"' in text
    assert "Type trusted-lan to confirm LAN exposure" in text
    assert 'SIM_HOST_BIND="$SIM_HOST_BIND" docker compose up -d' in text
