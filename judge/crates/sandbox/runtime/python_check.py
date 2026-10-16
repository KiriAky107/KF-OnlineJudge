"""Trusted, isolated syntax checker; never evaluates submitted code or writes pyc."""

import sys


def main():
    try:
        with open("/work/main.py", "rb") as source:
            text = source.read(262145)
        if len(text) > 262144:
            print("source exceeds syntax checker byte ceiling", file=sys.stderr)
            return 65
        # compile(bytes) honors Python's BOM/encoding declaration without
        # interpreting any top-level statement or importing user modules.
        compile(text, "/work/main.py", "exec", dont_inherit=True)
    except (SyntaxError, UnicodeError, ValueError) as error:
        # Source belongs to the submitting user. Hidden test input is never
        # supplied to this stage; diagnostic capture remains OS bounded.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("trusted syntax checker input unavailable", file=sys.stderr)
        return 70
    return 0


if __name__ == "__main__":
    sys.exit(main())
