"""
vaultpath.py — locate the AutoNotes vault and required tools without hardcoding a platform.

The pipeline scripts previously each carried `Path(r"D:\\Clod\\AutoNotes\\Reference Material")`,
which made them unusable anywhere else. Resolution order, first hit wins:

  1. an explicit `--vault` argument
  2. the `AUTONOTES_VAULT` environment variable
  3. a `.autonotes-vault` file next to these scripts, containing the path on its first line
  4. a short list of conventional locations for the current platform

Failure is explicit: a message naming all four mechanisms, rather than a stack trace from a path
that does not exist.
"""
import os
import shutil
import sys
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / ".autonotes-vault"
ENV_VAR = "AUTONOTES_VAULT"
VAULT_LEAF = Path("AutoNotes") / "Reference Material"

# Conventional spots to try, in order. Windows drive letters are only probed on Windows.
def _candidates():
    home = Path.home()
    if sys.platform == "win32":
        for drive in ("D:", "C:", "E:"):
            yield Path(drive + "\\") / "Clod" / VAULT_LEAF
    yield home / "Clod" / VAULT_LEAF
    yield home / "Documents" / "Clod" / VAULT_LEAF
    yield home / VAULT_LEAF
    # Syncthing / cloud-sync layouts people actually use
    yield home / "Sync" / "Clod" / VAULT_LEAF
    yield home / "obsidian" / VAULT_LEAF


def _looks_like_vault(p: Path):
    """Cheap shape check so a wrong path fails loudly instead of producing an empty run."""
    if not p.is_dir():
        return False
    return (p / "Component Index.md").exists() or any(
        d.name.lower() == "attachments" for d in p.rglob("attachments") if d.is_dir())


def _resolve_root(p: Path):
    """Accept the vault root, its parent, or Reference Material itself.

    Deepest candidate first: given `<...>/AutoNotes`, the answer is its `Reference Material`
    subfolder, not the folder itself — checking the bare path first returned the wrong level.
    """
    for cand in (p / VAULT_LEAF, p / "Reference Material", p):
        if _looks_like_vault(cand):
            return cand.resolve()
    return None


def find_vault(explicit=None, required=True):
    """-> Path to '<...>/AutoNotes/Reference Material'."""
    if explicit:
        p = Path(explicit).expanduser()
        found = _resolve_root(p)
        if found:
            return found
        if required:
            sys.exit(f"--vault does not look like the vault: {p}\n"
                     "  expected a folder containing 'Component Index.md' or 'attachments/' dirs")
        return p

    env = os.environ.get(ENV_VAR)
    if env:
        found = _resolve_root(Path(env).expanduser())
        if found:
            return found
        if required:
            sys.exit(f"{ENV_VAR} is set to {env}, which does not look like the vault")

    if CONFIG_FILE.exists():
        line = CONFIG_FILE.read_text(encoding="utf-8").strip().splitlines()
        if line:
            found = _resolve_root(Path(line[0].strip()).expanduser())
            if found:
                return found
            if required:
                sys.exit(f"{CONFIG_FILE.name} points at {line[0]}, "
                         "which does not look like the vault")

    for cand in _candidates():
        if _looks_like_vault(cand):
            return cand.resolve()

    if not required:
        return None
    sys.exit(
        "Cannot locate the AutoNotes vault. Set it one of these ways:\n"
        f"  --vault /path/to/AutoNotes/Reference Material\n"
        f"  export {ENV_VAR}=/path/to/AutoNotes        (setx on Windows)\n"
        f"  echo /path/to/AutoNotes > {CONFIG_FILE.name}\n"
        "  or place the vault at ~/Clod/AutoNotes/Reference Material"
    )


def require_tool(name, purpose=""):
    """Exit with an installable hint if an external binary is missing."""
    found = shutil.which(name)
    if found:
        return found
    hints = {
        "pdftotext": {
            "win32": "ships with Git for Windows (mingw64/bin), or "
                     "`choco install poppler` / `scoop install poppler`",
            "darwin": "brew install poppler",
            "linux": "apt install poppler-utils   (or dnf install poppler-utils)",
        },
    }.get(name, {})
    key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    hint = hints.get(key, "install it and ensure it is on PATH")
    sys.exit(f"required tool '{name}' not found on PATH"
             + (f" (needed for {purpose})" if purpose else "")
             + f".\n  {hint}")


def have_tool(name):
    return shutil.which(name) is not None


def path_key(p):
    """A comparison key that matches the filesystem's own case behaviour.

    `os.path.normcase` lowercases on Windows and is the identity on POSIX — which is exactly right.
    Blanket `.lower()` would merge `Foo.pdf` and `foo.pdf` on Linux, where they are two files.
    """
    return os.path.normcase(str(Path(p)))


def dedupe(paths):
    """Preserve order, drop filesystem-equivalent duplicates.

    Needed because `rglob('*.pdf')` and `rglob('*.PDF')` both match every file on Windows (its
    globbing is case-insensitive) but only their own case on Linux — so both patterns are required
    for portability, and deduping is required to stop Windows listing everything twice.
    """
    seen, out = set(), []
    for p in paths:
        key = path_key(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_pdfs(vault: Path, folder_name="attachments"):
    """Every PDF inside a `<folder_name>/` directory anywhere under the vault, deduped."""
    found = []
    for pattern in ("*.pdf", "*.PDF"):
        found.extend(sorted(vault.rglob(pattern)))
    return [p for p in dedupe(found) if p.parent.name.lower() == folder_name]


def write_text(path: Path, text: str):
    """Write UTF-8 with LF endings on every platform.

    Left to Python's defaults this emits CRLF on Windows and LF elsewhere, so the same vault
    synced between machines churns every generated file on regeneration.
    """
    path.write_text(text, encoding="utf-8", newline="\n")
