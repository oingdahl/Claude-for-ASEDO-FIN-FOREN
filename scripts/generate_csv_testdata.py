"""Genererar test_data/Sophone_2025_transar.csv (UTF-8 BOM, 263 rader)."""
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)


def fmt_kr(oren):
    neg = oren < 0
    kr = abs(oren) / 100
    s = "{:,.2f}".format(kr).replace(",", "X").replace(".", ",").replace("X", " ")
    return ("-" if neg else "") + s


def fmt_balance(oren):
    kr = oren / 100
    s = "{:,.2f}".format(kr).replace(",", "X").replace(".", ",").replace("X", " ")
    return s


TARGET_COUNT = 263
END_BALANCE_OREN = 61199605

# Rader: (date, text, type, amount_oren)
rows = [
    (date(2025, 12, 30), "BG 5801-7161 ", "Bankgirobetalning", 3103600),
    (date(2025, 12, 30), "LBE5801-7161", "Annan", -8776800),
]

start = date(2025, 1, 3)
end = date(2025, 12, 29)
days_span = (end - start).days

raw_dates = [start + timedelta(days=random.randint(0, days_span)) for _ in range(259)]
raw_dates_sorted = sorted(raw_dates, reverse=True)
dates_extra = [end] + raw_dates_sorted + [start]  # 261 datum

LONER_IDX = 100

extra_rows = []
for i, d in enumerate(dates_extra):
    if i == LONER_IDX:
        extra_rows.append((d, "LÖNER JUNI", "Lön", -4500000))
    elif i % 7 == 0:
        amt = int(random.choice([500000, 1000000, 2000000, 3500000, 5000000]))
        extra_rows.append((
            d,
            "BG {}-{}".format(random.randint(1000, 9999), random.randint(1000, 9999)),
            "Bankgirobetalning",
            amt,
        ))
    else:
        amt = -int(random.choice([50000, 100000, 250000, 500000, 750000, 1000000, 1500000]))
        extra_rows.append((d, "Leverantör {}".format(i), "Annan", amt))

rows.extend(extra_rows)
assert len(rows) == TARGET_COUNT, "Fel antal rader: {}".format(len(rows))

# Löpande balans (filen är i omvänd kronologisk ordning)
# Rad 1: balance = END_BALANCE_OREN
# Rad n+1: balance = balance[n] - amount[n]
balances = [END_BALANCE_OREN]
for i in range(len(rows) - 1):
    amt_i = rows[i][3]
    assert isinstance(amt_i, int), "Belopp är inte int vid rad {}: {}".format(i, type(amt_i))
    balances.append(balances[-1] - amt_i)

# Bygg CSV
header = "Bokförd;Valutadatum;Text;Typ;Insättningar;Uttag;Bokfört saldo"
csv_lines = [header]
for (d, text, typ, amt), bal in zip(rows, balances):
    date_str = d.strftime("%Y-%m-%d")
    if amt >= 0:
        ins = fmt_kr(amt)
        uttag = ""
    else:
        ins = ""
        uttag = fmt_kr(amt)
    bal_str = fmt_balance(bal)
    csv_lines.append("{};{};{};{};{};{};{}".format(
        date_str, date_str, text, typ, ins, uttag, bal_str
    ))

content = "\n".join(csv_lines) + "\n"

out = Path("test_data/Sophone_2025_transar.csv")
out.write_bytes(content.encode("utf-8-sig"))

data_rows = [l for l in content.splitlines()[1:] if l.strip()]
print("Skriven: {} ({} bytes)".format(out, out.stat().st_size))
print("Antal datarader: {} (ska vara 263)".format(len(data_rows)))

r2 = csv_lines[1].split(";")
print("\nRad 2: {}".format(csv_lines[1]))
print("Rad 3: {}".format(csv_lines[2]))
print("\nKontroller:")
print("  Datum rad 2:  {}  (ska vara 2025-12-30)".format(r2[0]))
print("  Insattning:   {}  (ska vara '31 036,00')".format(repr(r2[4])))
print("  Saldo:        {}  (ska vara '611 996,05')".format(repr(r2[6])))

loen_rows = [l for l in csv_lines[1:] if ";Lön;" in l]
print("\nLöneposter (typ=Lön): {}".format(len(loen_rows)))
if loen_rows:
    print("  {}".format(loen_rows[0]))

print("\nDatum nyast: {}".format(csv_lines[1].split(";")[0]))
print("Datum äldst: {}  (ska vara 2025-01-03)".format(csv_lines[-1].split(";")[0]))
