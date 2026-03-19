"""Diagnostikskript – kör detta för att hitta exakt vilken rad i SIE-filen som orsakar felet.

Användning:
    python diagnostik_sie.py din_sie_fil.SE
"""

import sys
import re

def main():
    if len(sys.argv) < 2:
        print("Användning: python diagnostik_sie.py <sie-fil>")
        sys.exit(1)

    filepath = sys.argv[1]

    # Försök läsa filen med olika encodings
    raw = open(filepath, "rb").read()
    for enc in ["cp437", "latin-1", "utf-8-sig", "utf-8"]:
        try:
            text = raw.decode(enc)
            print(f"Encoding: {enc}")
            break
        except UnicodeDecodeError:
            continue

    lines = text.splitlines()
    print(f"Antal rader: {len(lines)}\n")

    # Visa alla #TRANS-rader och tokenisera dem
    problem_found = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped.startswith("#TRANS"):
            continue

        # Enkel tokenisering
        tokens = []
        j = 0
        n = len(stripped)
        while j < n:
            ch = stripped[j]
            if ch.isspace():
                j += 1
            elif ch == '"':
                k = j + 1
                while k < n and stripped[k] != '"':
                    k += 1
                tokens.append(stripped[j+1:k])
                j = k + 1
            elif ch == '{':
                k = j + 1
                while k < n and stripped[k] != '}':
                    k += 1
                tokens.append(stripped[j:k+1])
                j = k + 1
            else:
                k = j
                while k < n and not stripped[k].isspace():
                    k += 1
                tokens.append(stripped[j:k])
                j = k

        args = tokens[1:]  # utan #TRANS

        if len(args) < 3:
            print(f"RAD {i}: FEL – för få argument ({len(args)}): {stripped!r}")
            problem_found = True
            continue

        # Kontrollera belopp (args[2])
        try:
            from decimal import Decimal
            Decimal(args[2])
        except Exception as e:
            print(f"RAD {i}: OGILTIGT BELOPP {args[2]!r}: {e}")
            print(f"  Hela raden: {stripped!r}")
            print(f"  Tokens: {args}")
            problem_found = True
            continue

        # Kontrollera kvantitet (args[4] om det finns)
        if len(args) > 4 and args[4]:
            try:
                float(args[4])
            except ValueError:
                print(f"RAD {i}: OGILTIGT KVANTITETSFÄLT args[4]={args[4]!r}")
                print(f"  Hela raden: {stripped!r}")
                print(f"  Tokens: {args}")
                problem_found = True

    if not problem_found:
        print("Inga uppenbara problem hittades i #TRANS-raderna.")
        print("\nKontrollerar #VER-rader...")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#VER"):
                print(f"  RAD {i}: {stripped!r}")

if __name__ == "__main__":
    main()
