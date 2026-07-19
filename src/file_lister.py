import os

SKIP_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    "build",
    "node_modules",
    ".venv",
    "__pycache__",
    ".kotlin",
    ".cxx",
    "bin",
    "obj",
    "target",
    "dist"
}

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp4", ".mp3", ".wav",
    ".zip", ".rar", ".7z",
    ".apk", ".aab",
    ".jar", ".class",
    ".db", ".wal", ".shm",
    ".so", ".dll", ".exe", ".o", ".pyc"
}

MAX_SIZE = 5 * 1024 * 1024  # 5 MB


def process_files(root_dir, output_file):
    output_abs = os.path.abspath(output_file)

    with open(output_file, "w", encoding="utf-8") as outfile:
        for root, dirs, files in os.walk(root_dir):

            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

            files.sort()

            for filename in files:
                filepath = os.path.join(root, filename)

                if os.path.abspath(filepath) == output_abs:
                    continue

                ext = os.path.splitext(filename)[1].lower()

                if ext in SKIP_EXTENSIONS:
                    continue

                try:
                    if os.path.getsize(filepath) > MAX_SIZE:
                        outfile.write(
                            f"\n{'='*120}\n"
                            f"FILE: {os.path.relpath(filepath, root_dir)}\n"
                            f"{'='*120}\n"
                            "[SKIPPED: FILE TOO LARGE]\n"
                        )
                        continue
                except OSError:
                    continue

                outfile.write(
                    f"\n{'='*120}\n"
                    f"FILE: {os.path.relpath(filepath, root_dir)}\n"
                    f"{'='*120}\n\n"
                )

                try:
                    with open(filepath, "r", encoding="utf-8") as infile:
                        outfile.write(infile.read())
                except UnicodeDecodeError:
                    outfile.write("[BINARY FILE]\n")
                except Exception as e:
                    outfile.write(f"[ERROR READING FILE: {e}]\n")

                outfile.write("\n\n")


if __name__ == "__main__":
    target_dir = input("Directory (Enter = current): ").strip() or "."
    output = input("Output (Enter = all_files.txt): ").strip() or "all_files.txt"

    process_files(target_dir, output)

    print(f"\nDone -> {output}")