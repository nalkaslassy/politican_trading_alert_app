"""SQLite storage layer for Capitol Radar — no ORM, raw sqlite3."""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    """Wraps a SQLite connection and exposes all storage operations."""

    def __init__(self, db_path: str):
        """Initialise with path; does not open connection until init_db() is called."""
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        """Return a new connection with row_factory set to dict-like rows."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        """Create all tables if they do not already exist."""
        ddl = """
        CREATE TABLE IF NOT EXISTS seen_trades (
            trade_id TEXT PRIMARY KEY,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS all_trades (
            trade_id TEXT PRIMARY KEY,
            politician_name TEXT,
            party TEXT,
            chamber TEXT,
            ticker TEXT,
            trade_type TEXT,
            trade_size TEXT,
            trade_date TEXT,
            filing_date TEXT,
            source_url TEXT,
            alerted INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS trade_outcomes (
            trade_id TEXT PRIMARY KEY,
            ticker TEXT,
            politician_name TEXT,
            trade_date TEXT,
            price_at_trade REAL,
            price_30d REAL,
            price_60d REAL,
            return_30d REAL,
            return_60d REAL,
            outcome_30d TEXT,
            outcome_60d TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS politician_stats (
            politician_name TEXT PRIMARY KEY,
            party TEXT,
            chamber TEXT,
            total_buys INTEGER DEFAULT 0,
            wins_30d INTEGER DEFAULT 0,
            losses_30d INTEGER DEFAULT 0,
            win_rate_30d REAL DEFAULT 0.0,
            avg_return_30d REAL DEFAULT 0.0,
            total_buys_alerted INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        with self._connect() as conn:
            conn.executescript(ddl)
        logger.info("Database initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Seen-trades deduplication
    # ------------------------------------------------------------------

    def is_seen(self, trade_id: str) -> bool:
        """Return True if trade_id has already been processed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_trades WHERE trade_id = ?", (trade_id,)
            ).fetchone()
        return row is not None

    def mark_seen(self, trade_id: str) -> None:
        """Insert trade_id into seen_trades; ignore if already present."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_trades (trade_id) VALUES (?)",
                (trade_id,),
            )

    # ------------------------------------------------------------------
    # Trade storage
    # ------------------------------------------------------------------

    def insert_trade(self, trade: dict, alerted: bool) -> None:
        """Insert a trade into all_trades; ignore if already stored."""
        sql = """
        INSERT OR IGNORE INTO all_trades
            (trade_id, politician_name, party, chamber, ticker,
             trade_type, trade_size, trade_date, filing_date, source_url, alerted)
        VALUES
            (:trade_id, :politician_name, :party, :chamber, :ticker,
             :trade_type, :trade_size, :trade_date, :filing_date, :source_url, :alerted)
        """
        row = dict(trade)
        row["alerted"] = 1 if alerted else 0
        with self._connect() as conn:
            conn.execute(sql, row)

    # ------------------------------------------------------------------
    # Performance tracking
    # ------------------------------------------------------------------

    def get_pending_outcomes(self) -> list[dict]:
        """Return Buy trades whose 30-day or 60-day outcome is still pending or missing."""
        sql = """
        SELECT
            at.trade_id,
            at.ticker,
            at.politician_name,
            at.party,
            at.chamber,
            at.trade_date,
            to2.price_at_trade,
            to2.price_30d,
            to2.price_60d,
            to2.outcome_30d,
            to2.outcome_60d
        FROM all_trades at
        LEFT JOIN trade_outcomes to2 ON at.trade_id = to2.trade_id
        WHERE at.trade_type = 'Buy'
          AND (
              to2.outcome_30d IS NULL
              OR to2.outcome_30d = 'pending'
              OR to2.outcome_60d IS NULL
              OR to2.outcome_60d = 'pending'
          )
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def upsert_outcome(self, trade_id: str, fields: dict) -> None:
        """Insert or update a row in trade_outcomes."""
        fields["trade_id"] = trade_id
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(f":{k}" for k in fields)
        updates = ", ".join(
            f"{k} = :{k}" for k in fields if k != "trade_id"
        )
        sql = f"""
        INSERT INTO trade_outcomes ({cols})
        VALUES ({placeholders})
        ON CONFLICT(trade_id) DO UPDATE SET {updates},
            updated_at = CURRENT_TIMESTAMP
        """
        with self._connect() as conn:
            conn.execute(sql, fields)

    # ------------------------------------------------------------------
    # Politician stats
    # ------------------------------------------------------------------

    def upsert_politician_stats(self, politician_name: str, fields: dict) -> None:
        """Insert or update a row in politician_stats."""
        fields["politician_name"] = politician_name
        cols = ", ".join(fields.keys())
        placeholders = ", ".join(f":{k}" for k in fields)
        updates = ", ".join(
            f"{k} = :{k}" for k in fields if k != "politician_name"
        )
        sql = f"""
        INSERT INTO politician_stats ({cols})
        VALUES ({placeholders})
        ON CONFLICT(politician_name) DO UPDATE SET {updates},
            last_updated = CURRENT_TIMESTAMP
        """
        with self._connect() as conn:
            conn.execute(sql, fields)

    def get_politician_stats(self, politician_name: str) -> dict | None:
        """Return a politician's stats row or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM politician_stats WHERE politician_name = ?",
                (politician_name,),
            ).fetchone()
        return dict(row) if row else None

    def get_leaderboard(self, min_trades: int = 5) -> list[dict]:
        """Return politician_stats rows ordered by win_rate_30d desc, filtered by min_trades."""
        sql = """
        SELECT * FROM politician_stats
        WHERE total_buys >= ?
        ORDER BY win_rate_30d DESC, avg_return_30d DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (min_trades,)).fetchall()
        return [dict(r) for r in rows]

    def get_trade_outcomes_for_politician(self, politician_name: str) -> list[dict]:
        """Return all settled trade outcomes for a given politician."""
        sql = """
        SELECT * FROM trade_outcomes
        WHERE politician_name = ?
          AND outcome_30d IS NOT NULL
          AND outcome_30d != 'pending'
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (politician_name,)).fetchall()
        return [dict(r) for r in rows]
