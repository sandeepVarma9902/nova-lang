# modpath.py — where `import x` / `import std.web` resolves to a file.
#
# Resolution order for `import a.b`:
#   1. <basedir>/a/b.nova            (project-local, incl. dotted paths)
#   2. <nova_lang>/std/a/b.nova      (the standard library)
# Plain `import foo` keeps working exactly as before.

import os

_STD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "std")


def resolve(name, basedir):
    """Return the .nova path for an import name, or None."""
    rel = name + ".nova"
    local = os.path.join(basedir, rel)
    if os.path.isfile(local):
        return local
    std = os.path.join(_STD_DIR, rel)
    if os.path.isfile(std):
        return std
    return None


def std_dir():
    return _STD_DIR
