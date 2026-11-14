"""Conformance fixture runner across discovered packrat symbol parsers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from models import CODE_EXTENSIONS
from parsers.stamps import read_stamp

if TYPE_CHECKING:
    from symbol_parsers import SymbolParsers


@dataclass(frozen=True)
class ConformanceOutcome:
    """Result of running conformance checks for one parser directory."""

    parser: str
    status: str  # "passed" | "skipped" | "failed"
    detail: str = ""
    fixtures_checked: int = 0


@dataclass(frozen=True)
class ConformanceReport:
    """Aggregated conformance report across parser directories."""

    outcomes: tuple[ConformanceOutcome, ...]

    @property
    def ok(self) -> bool:
        """True when zero parsers failed conformance (skips and passes both count as ok)."""
        return all(item.status != "failed" for item in self.outcomes)


def _check_deep_nesting_fixture(
    parser_name: str,
    fixture_name: str,
    rel_name: str,
    text: str,
    scripts_dir: Path,
    parsers: SymbolParsers,
    checked: int,
) -> ConformanceOutcome | None:
    """Checks that a deep-nesting fixture hits per-file deadline without poisoning the worker."""
    from symbol_parsers import REGEX_PARSER_ID, SymbolParsers

    with SymbolParsers(
        scripts_dir=scripts_dir,
        overrides={},
        deadline_ms=50,
        spawn=parsers.spawn_factory,
    ) as deadline_parsers:
        res = deadline_parsers.parse_batch({rel_name: text})[rel_name]
        unavail = deadline_parsers.unavailable()
    if parser_name in unavail:
        return ConformanceOutcome(
            parser=parser_name,
            status="failed",
            detail=f"{fixture_name} made parser unavailable: {unavail[parser_name]}",
            fixtures_checked=checked,
        )
    if res.parser_id != REGEX_PARSER_ID:
        return ConformanceOutcome(
            parser=parser_name,
            status="failed",
            detail=f"{fixture_name} did not time out at 50 ms (got {res.parser_id})",
            fixtures_checked=checked,
        )
    return None


def _check_regular_fixture(
    parser_name: str,
    fixture: Path,
    rel_name: str,
    text: str,
    parsers: SymbolParsers,
    checked: int,
) -> ConformanceOutcome | None:
    """Checks syntax-error recovery or clean parsing for a single conformance fixture."""
    res = parsers.parse_batch({rel_name: text})[rel_name]
    unavail = parsers.unavailable()
    if parser_name in unavail:
        return ConformanceOutcome(
            parser=parser_name,
            status="failed",
            detail=f"{fixture.name} triggered unavailable: {unavail[parser_name]}",
            fixtures_checked=checked,
        )
    if fixture.stem == "syntax_error" and res.syntax_error_count < 1:
        return ConformanceOutcome(
            parser=parser_name,
            status="failed",
            detail=f"{fixture.name} reported syntax_error_count={res.syntax_error_count}",
            fixtures_checked=checked,
        )
    return None


def _check_single_parser(
    parser_dir: Path,
    scripts_dir: Path,
    conformance_dir: Path,
    parsers: SymbolParsers,
) -> ConformanceOutcome:
    """Runs deep-nesting deadline and syntax-error fixtures for one parser directory."""
    from symbol_parsers import ROUTING

    stamp = read_stamp(parser_dir)
    if stamp is None:
        return ConformanceOutcome(
            parser=parser_dir.name, status="failed", detail="missing or invalid stamp.json"
        )
    if not parsers.is_parser_active(stamp.parser):
        reason = parsers.unavailable().get(stamp.parser) or "missing-binary"
        return ConformanceOutcome(parser=stamp.parser, status="skipped", detail=reason)

    files = [
        p
        for p in sorted(conformance_dir.iterdir())
        if p.is_file()
        and p.suffix.lower() in CODE_EXTENSIONS
        and ROUTING.get(p.suffix.lower()) == stamp.parser
    ]
    if not files:
        return ConformanceOutcome(
            parser=stamp.parser, status="skipped", detail="no conformance fixtures"
        )

    for checked, fixture in enumerate(files, start=1):
        text = fixture.read_text(encoding="utf-8")
        rel_name = f"{parser_dir.name}/fixtures/conformance/{fixture.name}"
        if fixture.stem == "deep_nesting":
            failure = _check_deep_nesting_fixture(
                stamp.parser, fixture.name, rel_name, text, scripts_dir, parsers, checked
            )
        else:
            failure = _check_regular_fixture(
                stamp.parser, fixture, rel_name, text, parsers, checked
            )
        if failure is not None:
            return failure
    return ConformanceOutcome(
        parser=stamp.parser,
        status="passed",
        detail=f"{len(files)} conformance fixture(s) passed",
        fixtures_checked=len(files),
    )


def run_conformance(parsers: SymbolParsers, parsers_dir: Path) -> ConformanceReport:
    """Runs conformance checks for every parser subdirectory with `fixtures/conformance/`."""
    root = Path(parsers_dir)
    scripts_dir = root.parent
    outcomes: list[ConformanceOutcome] = []
    if not root.is_dir():
        return ConformanceReport(outcomes=())
    for parser_dir in sorted(root.iterdir()):
        if not parser_dir.is_dir() or parser_dir.name.startswith((".", "__")):
            continue
        conf_dir = parser_dir / "fixtures" / "conformance"
        if not conf_dir.is_dir():
            continue
        outcomes.append(_check_single_parser(parser_dir, scripts_dir, conf_dir, parsers))
    return ConformanceReport(outcomes=tuple(outcomes))
