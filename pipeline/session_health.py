"""Pure completed-session checks, shared by the scanner's regression tests."""
import pandas as pd


def validate_session(rows, completed):
    if not rows:
        raise RuntimeError("No valid daily histories; retaining last good output")
    session = max(frame.index[-1] for _, frame in rows)
    if session.date() > completed or (completed - session.date()).days > 4:
        raise RuntimeError("Price session is future/incomplete or stale by over four days")
    current = [(symbol, frame) for symbol, frame in rows if frame.index[-1] == session]
    coverage = len(current) / len(rows) * 100
    if coverage < 95:
        raise RuntimeError(f"Same-session coverage below 95% ({coverage:.1f}%)")
    return pd.Timestamp(session), current, coverage
