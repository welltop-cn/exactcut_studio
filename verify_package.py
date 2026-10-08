from pathlib import Path
import hashlib
root = Path(__file__).resolve().parent
count = 0
for line in (root / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
    expected, name = line.split("  ", 1)
    relative = Path(name)
    assert not relative.is_absolute() and ".." not in relative.parts, name
    path = root / relative
    assert path.is_file() and not path.is_symlink(), name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    count += 1
print(f"Verified {count} files")
