from pathlib import Path
import subprocess
import sys


def test_saas_generator_creates_expected_outputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "processed"

    result = subprocess.run(
        [
            sys.executable,
            "src/data/generator.py",
            "--users",
            "100",
            "--days",
            "7",
            "--events",
            "500",
            "--seed",
            "42",
            "--output-dir",
            str(output_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "SaaS mock data generated successfully." in result.stdout
    assert (output_dir / "users.parquet").exists()
    assert (output_dir / "events.parquet").exists()
    assert (output_dir / "subscriptions.parquet").exists()
    assert (output_dir / "experiment_assignments.parquet").exists()
    assert (output_dir / "daily_metrics.parquet").exists()
    assert (output_dir / "datasherlock.duckdb").exists()