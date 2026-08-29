from pathlib import Path


def read_master_file(path: Path) -> dict[str, set[str]]:
    subjects: dict[str, set[str]] = {}
    current: str | None = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.endswith(":"):
            current = line[:-1].strip()
            if not current:
                raise ValueError(f"Line {line_number}: subject name is empty")
            subjects.setdefault(current, set())
        elif current is None:
            raise ValueError(f"Line {line_number}: URL appears before a subject header")
        elif not line.startswith(("http://", "https://")):
            raise ValueError(f"Line {line_number}: expected an HTTP(S) URL")
        else:
            subjects[current].add(line)
    if not subjects:
        raise ValueError("No subjects found in the master file")
    return subjects
