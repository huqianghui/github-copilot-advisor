"""Explicit valid dataset bytes for deployment artifact contracts."""

from hashlib import sha256
import json
from pathlib import Path
import zipfile


DATASET = "runtime-config/benchmark-cases.json"


def add_approved_dataset(case, target):
    artifact = Path(case.plans[target][1]["artifact"])
    content = json.dumps({
        "version": 1,
        "cases": [{"case_id": "fixture-01", "question": "Explicit fixture question."}],
        "seeded_history": [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"Fixture history {index}"}
            for index in range(20)
        ],
    }).encode()
    if artifact.is_dir():
        files = {path.relative_to(artifact).as_posix(): path.read_bytes() for path in artifact.rglob("*") if path.is_file()}
    else:
        with zipfile.ZipFile(artifact) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["package-manifest.json"])
    manifest["files"][DATASET] = sha256(content).hexdigest()
    files[DATASET] = content
    files["package-manifest.json"] = json.dumps(manifest).encode()
    if artifact.is_dir():
        for name, data in files.items():
            (artifact / name).write_bytes(data)
    else:
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
    return artifact
