from pathlib import Path


def test_dockerignore_excludes_non_runtime_and_secret_artifacts():
    dockerignore = Path(__file__).resolve().parents[1] / ".dockerignore"
    patterns = set(dockerignore.read_text(encoding="utf-8").splitlines())

    expected_patterns = {
        "generator_runbooks.json",
        "generator_state.json",
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "QAQC/",
        "marketing/",
        "demo/",
        "*.mp4",
        "*.webm",
        "*.mov",
    }

    assert expected_patterns <= patterns
