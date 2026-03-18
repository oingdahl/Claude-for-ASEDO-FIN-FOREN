"""Tester för src/core/utils.py."""

from datetime import date

from src.core.utils import swedish_holidays


def test_langfredag_2024() -> None:
    assert date(2024, 3, 29) in swedish_holidays(2024)


def test_midsommarafton_2024() -> None:
    assert date(2024, 6, 21) in swedish_holidays(2024)


def test_langfredag_2025() -> None:
    assert date(2025, 4, 18) in swedish_holidays(2025)


def test_midsommarafton_2025() -> None:
    assert date(2025, 6, 20) in swedish_holidays(2025)


def test_juldagen_2024() -> None:
    assert date(2024, 12, 25) in swedish_holidays(2024)


def test_nyrsdagen_ingår() -> None:
    assert date(2025, 1, 1) in swedish_holidays(2025)


def test_vanlig_dag_ingår_inte() -> None:
    assert date(2025, 3, 10) not in swedish_holidays(2025)
