"""Validate application assets without traversing filesystem redirects."""

from pathlib import Path
import stat


def _checked_path(root: Path, path: Path):
    # Check every component: an explicitly included file can itself have a
    # junction/symlink parent. st_file_attributes works on Python 3.11 too.
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Included symlinks and reparse points are unsupported: {current}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Included files must be inside the application directory: {path}")
    return path.lstat(), resolved


def include_files(root: Path, item, output: Path):
    """Return a validated include root and its regular files, in stable order.

    Inputs must remain unchanged while a build runs. This is a packaging
    boundary, not a sandbox against concurrent hostile filesystem mutation.
    """
    relative = Path(item)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Included files must be inside the application directory, using relative paths")
    candidate = root / relative
    try:
        info, resolved = _checked_path(root, candidate)
        # Windows accepts aliases such as a trailing dot or an 8.3 short name.
        # Compare canonical paths only after checking lexical redirect parents.
        if output == resolved or stat.S_ISDIR(info.st_mode) and output.is_relative_to(resolved):
            raise ValueError("The generated output cannot be inside an included path")
        pending, files = [candidate], []
        while pending:
            path = pending.pop()
            info, _ = _checked_path(root, path)
            if stat.S_ISDIR(info.st_mode):
                # Validate before descending, so junction cycles cannot recurse.
                pending.extend(reversed(sorted(path.iterdir())))
            elif stat.S_ISREG(info.st_mode):
                files.append(path)
            else:
                raise ValueError(f"Included path must be a regular file or directory: {path}")
    except OSError as error:
        raise ValueError(f"Cannot read included path {item}: {error}") from error
    return candidate, files
