from pathlib import Path

import yaml


def test_compose_initializes_data_before_starting_api() -> None:
    compose_path = Path(__file__).parents[2] / "docker-compose.yml"
    with compose_path.open(encoding="utf-8") as file:
        services = yaml.safe_load(file)["services"]

    data_init = services["data-init"]
    api = services["api"]

    assert data_init["command"].startswith("python -m data.generator")
    assert "duckdb_data:/workspace/data" in data_init["volumes"]
    assert api["depends_on"]["data-init"]["condition"] == "service_completed_successfully"
    assert "duckdb_data:/workspace/data" in api["volumes"]


def test_docker_image_includes_canonical_config() -> None:
    project_root = Path(__file__).parents[2]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (project_root / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY config ./config" in dockerfile
    assert not any(line.strip() == "config" for line in dockerignore.splitlines())
