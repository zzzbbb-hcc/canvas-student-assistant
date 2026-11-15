"""Install the fixed, public Argos English-to-Chinese model (no study data)."""

from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile
import argparse
import shutil
import tempfile

import httpx


MODEL_URL = "https://argos-net.com/v1/translate-en_zh-1_9.argosmodel"
MODEL_SHA256 = "433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab"
MODEL_FILES = (
    "metadata.json", "sentencepiece.model", "README.md",
    "model/config.json", "model/shared_vocabulary.json", "model/model.bin",
)


def install(archive: Path, destination: Path) -> None:
    if sha256(archive.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("Translation model checksum did not match the official pinned package.")
    destination.mkdir(parents=True, exist_ok=True)
    # Never extract arbitrary archive paths or executable/tokenizer pickle files.
    with ZipFile(archive) as package:
        for relative in MODEL_FILES:
            member = package.getinfo("translate-en_zh-1_9/" + relative)
            if member.file_size > 100_000_000:
                raise ValueError("Unexpected translation model file size.")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / "models" / "en_zh"
    if args.archive is not None:
        install(args.archive, destination)
    else:
        with tempfile.TemporaryDirectory(prefix="student-assistant-model-") as temporary:
            archive = Path(temporary) / "model.argosmodel"
            # Deliberately separate from Canvas: no credentials or assignment text.
            with httpx.stream("GET", MODEL_URL, timeout=60.0, follow_redirects=False) as response:
                response.raise_for_status()
                total = 0
                with archive.open("wb") as output:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > 100_000_000:
                            raise ValueError("Translation model download exceeded its size limit.")
                        output.write(chunk)
            install(archive, destination)
    print("Offline English-to-Chinese model installed. No study content was transmitted.")


if __name__ == "__main__":
    main()
