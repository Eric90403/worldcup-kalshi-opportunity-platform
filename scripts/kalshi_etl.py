"""
ETL pipeline: MongoDB raw landing zone → PostgreSQL structured tables.

Reads raw JSON API responses from MongoDB and upserts them into the
PostgreSQL schema. Handles type conversion (strings → decimals, timestamps,
etc.) and deduplication via upsert (INSERT ... ON CONFLICT UPDATE).

Usage:
  from kalshi_etl import KalshiETL
  etl = KalshiETL()
  etl.sync_series()
  etl.sync_markets()
  etl.sync_events()
  etl.run_full_sync()
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import psycopg2
import psycopg2.extras

from kalshi_mongo import KalshiMongo

logger = logging.getLogger(__name__)

PG_HOST = "localhost"
PG_PORT = 5432
PG_DB = "kalshi_warehouse"
PG_USER = "kalshi"
PG_PASS = os.environ.get("KALSHI_PG_PASS", "kalshi_local_dev")


def to_decimal(val, default="0"):
    """Safely convert string/float/int to Decimal."""
    if val is None or val == "":
        return Decimal(default)
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def to_timestamp(val):
    """Convert Kalshi timestamp strings to datetime."""
    if not val or val == "":
        return None
    # Kalshi uses ISO 8601 strings like "2026-06-20T19:40:00Z"
    try:
        if isinstance(val, str):
            # Handle Z suffix
            if val.endswith("Z"):
                val = val[:-1] + "+00:00"
            return datetime.fromisoformat(val)
        return val
    except (ValueError, TypeError):
        return None


def to_int(val, default=0):
    """Safely convert to int."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def clean_str(val, max_len=None):
    """Truncate string to fit column."""
    if val is None:
        return None
    s = str(val)
    if max_len and len(s) > max_len:
        s = s[:max_len]
    return s


class KalshiETL:
    """ETL from MongoDB raw landing zone to PostgreSQL structured tables."""

    def __init__(self):
        self.mongo = KalshiMongo()
        self.pg = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASS,
        )
        self.pg.autocommit = False
        self.stats = {"series": 0, "events": 0, "markets": 0, "errors": 0}

    def _upsert_series(self, cur, s: dict):
        """Upsert a single series record."""
        cur.execute(
            """
            INSERT INTO series (
                ticker, title, category, frequency, tags, settlement_sources,
                contract_url, contract_terms_url, fee_type, fee_multiplier,
                volume_fp, last_updated_ts, first_seen_ts, scraped_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, NOW(), NOW()
            )
            ON CONFLICT (ticker) DO UPDATE SET
                title = EXCLUDED.title,
                category = EXCLUDED.category,
                frequency = EXCLUDED.frequency,
                tags = EXCLUDED.tags,
                settlement_sources = EXCLUDED.settlement_sources,
                fee_type = EXCLUDED.fee_type,
                fee_multiplier = EXCLUDED.fee_multiplier,
                volume_fp = EXCLUDED.volume_fp,
                last_updated_ts = EXCLUDED.last_updated_ts,
                scraped_at = NOW()
            """,
            (
                clean_str(s.get("ticker"), 100),
                s.get("title", ""),
                clean_str(s.get("category"), 100),
                s.get("frequency"),
                json.dumps(s.get("tags", [])) if s.get("tags") else None,
                json.dumps(s.get("settlement_sources", [])) if s.get("settlement_sources") else None,
                s.get("contract_url"),
                s.get("contract_terms_url"),
                clean_str(str(s.get("fee_type", "")) if s.get("fee_type") else None, 50),
                to_decimal(s.get("fee_multiplier")),
                to_decimal(s.get("volume_fp")),
                to_timestamp(s.get("last_updated_ts")),
            ),
        )

    def _upsert_market(self, cur, m: dict):
        """Upsert a single market record.

        Note: The /markets API does not return series_ticker. We derive it
        from the ticker prefix (everything before the first '-').
        """
        ticker = m.get("ticker", "")
        # Derive series_ticker from ticker prefix if not provided by API
        series_ticker = m.get("series_ticker")
        if not series_ticker and ticker and "-" in ticker:
            series_ticker = ticker.split("-")[0]
        cur.execute(
            """
            INSERT INTO markets (
                ticker, event_ticker, series_ticker, market_type, title, subtitle,
                yes_sub_title, no_sub_title, status, open_time, close_time,
                expected_expiration_time, expiration_time, settlement_timer_seconds,
                last_price_dollars, yes_bid_dollars, yes_ask_dollars,
                yes_bid_size_fp, yes_ask_size_fp, no_bid_dollars, no_ask_dollars,
                volume_fp, volume_24h_fp, open_interest_fp, notional_value_dollars,
                previous_yes_bid_dollars, previous_yes_ask_dollars, previous_price_dollars,
                result, settlement_value_dollars, settlement_ts, expiration_value,
                occurrence_datetime, strike_type, floor_strike, cap_strike,
                rules_primary, rules_secondary, price_level_structure,
                first_seen_ts, last_updated_ts
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                NOW(), NOW()
            )
            ON CONFLICT (ticker) DO UPDATE SET
                event_ticker = EXCLUDED.event_ticker,
                series_ticker = EXCLUDED.series_ticker,
                market_type = EXCLUDED.market_type,
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                close_time = EXCLUDED.close_time,
                last_price_dollars = EXCLUDED.last_price_dollars,
                yes_bid_dollars = EXCLUDED.yes_bid_dollars,
                yes_ask_dollars = EXCLUDED.yes_ask_dollars,
                yes_bid_size_fp = EXCLUDED.yes_bid_size_fp,
                yes_ask_size_fp = EXCLUDED.yes_ask_size_fp,
                no_bid_dollars = EXCLUDED.no_bid_dollars,
                no_ask_dollars = EXCLUDED.no_ask_dollars,
                volume_fp = EXCLUDED.volume_fp,
                volume_24h_fp = EXCLUDED.volume_24h_fp,
                open_interest_fp = EXCLUDED.open_interest_fp,
                result = EXCLUDED.result,
                settlement_value_dollars = EXCLUDED.settlement_value_dollars,
                settlement_ts = EXCLUDED.settlement_ts,
                last_updated_ts = NOW()
            """,
            (
                clean_str(m.get("ticker"), 200),
                clean_str(m.get("event_ticker"), 150),
                clean_str(series_ticker, 100),
                clean_str(m.get("market_type"), 50),
                m.get("title", ""),
                m.get("subtitle"),
                m.get("yes_sub_title"),
                m.get("no_sub_title"),
                clean_str(m.get("status"), 20),
                to_timestamp(m.get("open_time")),
                to_timestamp(m.get("close_time")),
                to_timestamp(m.get("expected_expiration_time")),
                to_timestamp(m.get("expiration_time")),
                to_int(m.get("settlement_timer_seconds")),
                to_decimal(m.get("last_price_dollars")),
                to_decimal(m.get("yes_bid_dollars")),
                to_decimal(m.get("yes_ask_dollars")),
                to_decimal(m.get("yes_bid_size_fp")),
                to_decimal(m.get("yes_ask_size_fp")),
                to_decimal(m.get("no_bid_dollars")),
                to_decimal(m.get("no_ask_dollars")),
                to_decimal(m.get("volume_fp")),
                to_decimal(m.get("volume_24h_fp")),
                to_decimal(m.get("open_interest_fp")),
                to_decimal(m.get("notional_value_dollars")),
                to_decimal(m.get("previous_yes_bid_dollars")),
                to_decimal(m.get("previous_yes_ask_dollars")),
                to_decimal(m.get("previous_price_dollars")),
                clean_str(m.get("result"), 10),
                to_decimal(m.get("settlement_value_dollars")),
                to_timestamp(m.get("settlement_ts")),
                m.get("expiration_value"),
                to_timestamp(m.get("occurrence_datetime")),
                clean_str(m.get("strike_type"), 50),
                to_decimal(m.get("floor_strike")),
                to_decimal(m.get("cap_strike")),
                m.get("rules_primary"),
                m.get("rules_secondary"),
                m.get("price_level_structure"),
            ),
        )

    def _upsert_event(self, cur, e: dict):
        """Upsert a single event record.

        Note: The Kalshi /events API returns 'event_ticker', not 'ticker'.
        The events table uses 'ticker' as the PK column name.
        """
        # API returns event_ticker; fall back to ticker for safety
        event_ticker = e.get("event_ticker") or e.get("ticker")
        # Store full event payload as metadata for fields not in dedicated columns
        metadata = {k: v for k, v in e.items()
                    if k not in ("event_ticker", "ticker", "series_ticker",
                                 "title", "sub_title", "status",
                                 "created_time", "close_time",
                                 "settlement_timer_seconds")}
        cur.execute(
            """
            INSERT INTO events (
                ticker, series_ticker, title, sub_title, status,
                created_time, close_time, settlement_timer_seconds,
                metadata, first_seen_ts, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, NOW(), NOW()
            )
            ON CONFLICT (ticker) DO UPDATE SET
                series_ticker = EXCLUDED.series_ticker,
                title = EXCLUDED.title,
                sub_title = EXCLUDED.sub_title,
                status = EXCLUDED.status,
                close_time = EXCLUDED.close_time,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            (
                clean_str(event_ticker, 150),
                clean_str(e.get("series_ticker"), 100),
                e.get("title", ""),
                e.get("sub_title"),
                clean_str(e.get("status"), 20),
                to_timestamp(e.get("created_time")),
                to_timestamp(e.get("close_time")),
                to_int(e.get("settlement_timer_seconds")),
                json.dumps(metadata) if metadata else None,
            ),
        )

    def _extract_items_from_mongo(self, collection: str, data_key: str):
        """Extract all items from raw MongoDB documents."""
        all_items = []
        for doc in self.mongo.db[collection].find({}, {"response": 1}):
            response = doc.get("response", {})
            items = response.get(data_key, [])
            all_items.extend(items)
        return all_items

    def sync_series(self):
        """ETL: MongoDB raw_series → PostgreSQL series table."""
        logger.info("Syncing series from MongoDB to PostgreSQL...")
        series_list = self._extract_items_from_mongo("raw_series", "series")
        logger.info(f"  Found {len(series_list)} series in MongoDB")

        cur = self.pg.cursor()
        count = 0
        batch_size = 500

        for s in series_list:
            try:
                self._upsert_series(cur, s)
                count += 1
                if count % batch_size == 0:
                    self.pg.commit()
                    logger.info(f"  Committed {count}/{len(series_list)} series...")
            except Exception as e:
                self.pg.rollback()
                cur = self.pg.cursor()
                self.stats["errors"] += 1
                logger.error(f"  Error on series {s.get('ticker', '?')}: {e}")

        self.pg.commit()
        cur.close()
        self.stats["series"] = count
        logger.info(f"  Synced {count} series to PostgreSQL")

    def sync_markets(self):
        """ETL: MongoDB raw_markets → PostgreSQL markets table."""
        logger.info("Syncing markets from MongoDB to PostgreSQL...")
        markets_list = self._extract_items_from_mongo("raw_markets", "markets")
        logger.info(f"  Found {len(markets_list)} markets in MongoDB")

        cur = self.pg.cursor()
        count = 0
        batch_size = 500

        for m in markets_list:
            try:
                self._upsert_market(cur, m)
                count += 1
                if count % batch_size == 0:
                    self.pg.commit()
                    logger.info(f"  Committed {count}/{len(markets_list)} markets...")
            except Exception as e:
                self.pg.rollback()
                cur = self.pg.cursor()
                self.stats["errors"] += 1
                logger.error(f"  Error on market {m.get('ticker', '?')}: {e}")

        self.pg.commit()
        cur.close()
        self.stats["markets"] = count
        logger.info(f"  Synced {count} markets to PostgreSQL")

    def sync_events(self):
        """ETL: MongoDB raw_events → PostgreSQL events table."""
        logger.info("Syncing events from MongoDB to PostgreSQL...")
        events_list = self._extract_items_from_mongo("raw_events", "events")
        logger.info(f"  Found {len(events_list)} events in MongoDB")

        if not events_list:
            logger.info("  No events to sync (need to ingest events first)")
            return

        cur = self.pg.cursor()
        count = 0
        batch_size = 1000

        for e in events_list:
            try:
                self._upsert_event(cur, e)
                count += 1
                if count % batch_size == 0:
                    self.pg.commit()
                    if count % 10000 == 0:
                        logger.info(f"  Committed {count}/{len(events_list)} events...")
            except Exception as e_err:
                self.pg.rollback()
                cur = self.pg.cursor()
                self.stats["errors"] += 1
                if self.stats["errors"] <= 10:
                    logger.error(f"  Error on event {e.get('event_ticker', e.get('ticker', '?'))}: {e_err}")

        self.pg.commit()
        cur.close()
        self.stats["events"] = count
        logger.info(f"  Synced {count} events to PostgreSQL ({self.stats['errors']} errors)")

    def run_full_sync(self):
        """Run full ETL sync from MongoDB to PostgreSQL."""
        start = datetime.now(timezone.utc)
        logger.info("=" * 60)
        logger.info("FULL ETL SYNC: MongoDB → PostgreSQL")
        logger.info("=" * 60)

        self.sync_series()
        self.sync_markets()
        self.sync_events()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("=" * 60)
        logger.info(f"ETL COMPLETE in {elapsed:.1f}s")
        logger.info(f"  Series:  {self.stats['series']}")
        logger.info(f"  Markets: {self.stats['markets']}")
        logger.info(f"  Events:  {self.stats['events']}")
        logger.info(f"  Errors:  {self.stats['errors']}")
        logger.info("=" * 60)

    def verify(self):
        """Print row counts from PostgreSQL."""
        cur = self.pg.cursor()
        for table in ["series", "markets", "events", "market_tags"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            logger.info(f"  PostgreSQL {table}: {count} rows")
        cur.close()

    def close(self):
        self.pg.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    etl = KalshiETL()
    etl.run_full_sync()
    print()
    etl.verify()
    etl.close()
