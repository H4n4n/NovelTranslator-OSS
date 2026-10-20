import json
import re
from pathlib import Path

CHAPTERS_FILE = Path("data/chapters.json")
OUTPUT_DIR = Path("output/inspection")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

with CHAPTERS_FILE.open("r", encoding="utf-8") as f:
    chapters = json.load(f)

chapter_number = 1

chapter = next(
    (ch for ch in chapters if ch["chapter"] == chapter_number),
    None,
)

if chapter is None:
    raise RuntimeError(f"Chapter {chapter_number} not found.")

text = chapter["text"]

hangul_re = re.compile(
    r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]"
)

hangul = hangul_re.findall(text)

output_file = OUTPUT_DIR / f"chapter_{chapter_number:04d}_chinese.txt"

output_file.write_text(text, encoding="utf-8")

print("===== CHAPTER INSPECTION =====")
print(f"Chapter: {chapter_number}")
print(f"Source file: {chapter['source_file']}")
print(f"Paragraph count: {chapter['paragraph_count']}")
print(f"Characters: {len(text):,}")
print(f"Suspicious Hangul characters: {len(hangul):,}")
print(f"Saved to: {output_file}")
print("==============================")

print()
print("FIRST 1200 CHARACTERS:")
print()
print(text[:1200])