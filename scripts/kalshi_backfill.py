"""
Historical data backfill for Kalshi markets.

Fetches candlestick data for settled markets using the historical endpoints,
stores raw responses in MongoDB, and writes structured time-series to Parquet
files partitioned by date.

Also fetches historical trades for volume/price analysis.

Usage:
  from kalshi_backfill import KalshiBackfill
  bf = KalshiBackfill()

  # Backfill candlesticks for a specific market
  bf.backfill_candlesticks("KXHIGHNY-26JUN26-T115", period="1h")

  # Backfill candlesticks for top N markets by volume
  bf.backfill_top_markets(limit=50, period="1h")

  # Backfill historical trades
  bf.backfill_trades(max_pages=10)

  # Check historical cutoff
  bf.get_historical_cutoff()
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

from kalshi_auth import KalshiClient
from kalshi_ingest import RateLimiter
from kalshi_mongo import KalshiMongo, COLL_RAW_CANDLESTICKS, COLL_RAW_TRADES

logger = logging.getLogger(__name__)

PARQUET_BASE = Path("./data/parquet")


class KalshiBackfill:
    """Historical data backfill: candlesticks + trades → Mongo + Parquet."""

    def __init__(self, demo=False):
        self.client = KalshiClient(demo=demo)
        self.mongo = KalshiMongo()
        self.limiter = RateLimiter(read_per_second=15, write_per_second=8)
        self.stats = {
            "candlesticks_fetched": 0,
            "trades_fetched": 0,
            "markets_processed": 0,
            "parquet_files_written": 0,
            "errors": 0,
        }

    def _get_with_retry(self, path: str, params: dict = None) -> dict:
        """GET with rate limiting and exponential backoff."""
        for attempt in range(5):
            self.limiter.wait_read()
            try:
                return self.client.get(path, params=params)
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "500" in err_str or "502" in err_str or "503" in err_str:
                    wait = 1.0 * (2 ** attempt)
                    logger.warning(f"Retryable error on {path}, backing off {wait:.1f}s")
                    time.sleep(wait)
                    continue
                self.stats["errors"] += 1
                raise
        raise RuntimeError(f"Exhausted retries on {path}")

    def _lookup_series_ticker(self, ticker: str) -> str:
        """Derive the series_ticker from a market ticker.

        Kalshi market tickers follow the pattern: {SERIES_TICKER}-{event/market suffix}
        The series ticker is the prefix before the first '-'.
        Example: KXCOPPERD-26JUN1817-T6.52 → KXCOPPERD
        """
        if not ticker or "-" not in ticker:
            return None
        return ticker.split("-")[0]

    def get_historical_cutoff(self) -> dict:
        """Get the cutoff timestamps between live and historical data."""
        response = self._get_with_retry("/historical/cutoff")
        logger.info(f"Historical cutoff: {json.dumps(response, indent=2)}")
        return response

    def _store_candlesticks_parquet(self, ticker: str, candlesticks: list, period: str):
        """Write candlestick data to Parquet, partitioned by date."""
        if not candlesticks:
            return

        # Convert to Arrow table
        rows = []
        for c in candlesticks:
            end_ts = c.get("end_period_ts")
            if end_ts:
                dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
            else:
                continue

            price = c.get("price", {})
            yes_bid = c.get("yes_bid", {})
            yes_ask = c.get("yes_ask", {})

            rows.append({
                "ticker": ticker,
                "end_period_ts": end_ts,
                "date": dt.strftime("%Y-%m-%d"),
                "period": period,
                "price_open": float(price.get("open_dollars", 0) or 0),
                "price_high": float(price.get("high_dollars", 0) or 0),
                "price_low": float(price.get("low_dollars", 0) or 0),
                "price_close": float(price.get("close_dollars", 0) or 0),
                "price_mean": float(price.get("mean_dollars", 0) or 0),
                "yes_bid_open": float(yes_bid.get("open_dollars", 0) or 0),
                "yes_bid_high": float(yes_bid.get("high_dollars", 0) or 0),
                "yes_bid_low": float(yes_bid.get("low_dollars", 0) or 0),
                "yes_bid_close": float(yes_bid.get("close_dollars", 0) or 0),
                "yes_ask_open": float(yes_ask.get("open_dollars", 0) or 0),
                "yes_ask_high": float(yes_ask.get("high_dollars", 0) or 0),
                "yes_ask_low": float(yes_ask.get("low_dollars", 0) or 0),
                "yes_ask_close": float(yes_ask.get("close_dollars", 0) or 0),
                "volume_fp": float(c.get("volume_fp", 0) or 0),
                "open_interest_fp": float(c.get("open_interest_fp", 0) or 0),
            })

        if not rows:
            return

        table = pa.table({
            "ticker": [r["ticker"] for r in rows],
            "end_period_ts": pa.array([r["end_period_ts"] for r in rows], type=pa.int64()),
            "date": [r["date"] for r in rows],
            "period": [r["period"] for r in rows],
            "price_open": pa.array([r["price_open"] for r in rows], type=pa.float64()),
            "price_high": pa.array([r["price_high"] for r in rows], type=pa.float64()),
            "price_low": pa.array([r["price_low"] for r in rows], type=pa.float64()),
            "price_close": pa.array([r["price_close"] for r in rows], type=pa.float64()),
            "price_mean": pa.array([r["price_mean"] for r in rows], type=pa.float64()),
            "yes_bid_open": pa.array([r["yes_bid_open"] for r in rows], type=pa.float64()),
            "yes_bid_high": pa.array([r["yes_bid_high"] for r in rows], type=pa.float64()),
            "yes_bid_low": pa.array([r["yes_bid_low"] for r in rows], type=pa.float64()),
            "yes_bid_close": pa.array([r["yes_bid_close"] for r in rows], type=pa.float64()),
            "yes_ask_open": pa.array([r["yes_ask_open"] for r in rows], type=pa.float64()),
            "yes_ask_high": pa.array([r["yes_ask_high"] for r in rows], type=pa.float64()),
            "yes_ask_low": pa.array([r["yes_ask_low"] for r in rows], type=pa.float64()),
            "yes_ask_close": pa.array([r["yes_ask_close"] for r in rows], type=pa.float64()),
            "volume_fp": pa.array([r["volume_fp"] for r in rows], type=pa.float64()),
            "open_interest_fp": pa.array([r["open_interest_fp"] for r in rows], type=pa.float64()),
        })

        # Write partitioned by date
        # Group by date and write each date's data
        dates = set(r["date"] for r in rows)
        for date_str in sorted(dates):
            partition_table = table.filter(pc.equal(table["date"], date_str))

            out_dir = PARQUET_BASE / "candlesticks" / f"dt={date_str}" / f"ticker={ticker[:50]}"
            out_dir.mkdir(parents=True, exist_ok=True)

            out_file = out_dir / f"period={period}.parquet"
            pq.write_table(partition_table, out_file, compression="snappy")
            self.stats["parquet_files_written"] += 1

    def backfill_candlesticks(
        self,
        ticker: str,
        period: str = "1h",
        start_ts: int = None,
        end_ts: int = None,
        historical: bool = False,
        series_ticker: str = None,
    ) -> list:
        """Fetch candlestick data for a market, store to Mongo + Parquet.

        Args:
            ticker: Market ticker
            period: "1m", "1h", or "1d"
            start_ts: Unix timestamp start (optional)
            end_ts: Unix timestamp end (optional)
            historical: Use historical endpoint (for settled markets before cutoff)
            series_ticker: Required for live endpoint (auto-fetched from Postgres if not provided)
        """
        # Map period strings to API values
        period_map = {"1m": 1, "1h": 60, "1d": 1440}
        period_minutes = period_map.get(period, 60)

        # start_ts and end_ts are REQUIRED for the candlestick endpoint
        # Default to last 30 days if not provided
        if not start_ts:
            start_ts = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        if not end_ts:
            end_ts = int(datetime.now(timezone.utc).timestamp())

        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_minutes,
        }

        if historical:
            path = f"/historical/markets/{ticker}/candlesticks"
        else:
            # Live endpoint requires series_ticker in the path
            if not series_ticker:
                series_ticker = self._lookup_series_ticker(ticker)
            if not series_ticker:
                logger.error(f"  Cannot find series_ticker for {ticker}, skipping")
                self.stats["errors"] += 1
                return []
            path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"

        logger.info(f"Fetching candlesticks for {ticker} (period={period}, historical={historical})")

        all_candlesticks = []
        cursor = None
        page = 0

        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor

            try:
                response = self._get_with_retry(path, params=page_params)
            except Exception as e:
                logger.error(f"  Error fetching candlesticks for {ticker}: {e}")
                self.stats["errors"] += 1
                return all_candlesticks

            # Store raw in Mongo
            self.mongo.store_raw(
                collection=COLL_RAW_CANDLESTICKS,
                endpoint=path,
                method="GET",
                response=response,
                params=page_params,
            )

            candles = response.get("candlesticks", [])
            all_candlesticks.extend(candles)
            self.stats["candlesticks_fetched"] += len(candles)

            cursor = response.get("cursor")
            page += 1

            if not cursor:
                break
            if page % 5 == 0:
                logger.info(f"  {ticker}: page {page}, {len(all_candlesticks)} candlesticks...")

        logger.info(f"  {ticker}: fetched {len(all_candlesticks)} candlesticks")

        # Write to Parquet
        if all_candlesticks:
            self._store_candlesticks_parquet(ticker, all_candlesticks, period)

        self.stats["markets_processed"] += 1
        return all_candlesticks

    def backfill_trades(self, ticker: str = None, max_pages: int = 20) -> list:
        """Fetch historical trades, store to Mongo.

        Args:
            ticker: Optional market ticker filter
            max_pages: Safety limit
        """
        params = {}
        if ticker:
            params["ticker"] = ticker

        path = "/historical/trades"
        logger.info(f"Fetching historical trades (ticker={ticker or 'all'})...")

        all_trades = []
        cursor = None
        page = 0

        while True:
            page_params = dict(params)
            page_params["limit"] = 100
            if cursor:
                page_params["cursor"] = cursor

            response = self._get_with_retry(path, params=page_params)

            self.mongo.store_raw(
                collection=COLL_RAW_TRADES,
                endpoint=path,
                method="GET",
                response=response,
                params=page_params,
            )

            trades = response.get("trades", [])
            all_trades.extend(trades)
            self.stats["trades_fetched"] += len(trades)

            cursor = response.get("cursor")
            page += 1

            if not cursor or (max_pages and page >= max_pages):
                break
            if page % 5 == 0:
                logger.info(f"  Trades: page {page}, {len(all_trades)} trades...")

        logger.info(f"  Fetched {len(all_trades)} historical trades")
        return all_trades

    def backfill_top_markets(
        self,
        limit: int = 20,
        period: str = "1h",
        settled_only: bool = False,
    ) -> dict:
        """Backfill candlesticks for top markets by volume.

        Queries PostgreSQL for the highest-volume markets, then fetches
        candlesticks for each. Uses live endpoint for active markets,
        historical endpoint for settled markets.

        Args:
            limit: Number of markets to process
            period: Candlestick period
            settled_only: Only process settled markets (uses historical endpoint)
        """
        import psycopg2

        logger.info(f"Finding top {limit} markets for backfill...")

        conn = psycopg2.connect("postgresql://kalshi:***@localhost:5432/kalshi_warehouse")
        cur = conn.cursor()

        if settled_only:
            cur.execute(
                """
                SELECT ticker, series_ticker, title, volume_fp
                FROM markets
                WHERE volume_fp > 0 AND status != 'active'
                ORDER BY volume_fp DESC
                LIMIT %s
                """,
                (limit,),
            )
        else:
            cur.execute(
                """
                SELECT ticker, series_ticker, title, volume_fp
                FROM markets
                WHERE volume_fp > 0 AND status = 'active'
                ORDER BY volume_fp DESC
                LIMIT %s
                """,
                (limit,),
            )

        market_list = cur.fetchall()
        cur.close()
        conn.close()

        logger.info(f"Found {len(market_list)} markets to backfill")

        results = {}
        for i, (ticker, series_ticker, title, vol) in enumerate(market_list):
            logger.info(f"[{i+1}/{len(market_list)}] {ticker[:50]} (vol={vol})")
            try:
                candles = self.backfill_candlesticks(
                    ticker,
                    period=period,
                    historical=settled_only,
                    series_ticker=series_ticker,
                )
                results[ticker] = len(candles)
            except Exception as e:
                logger.error(f"  Failed: {e}")
                self.stats["errors"] += 1
                results[ticker] = 0

        return results

    def get_stats(self) -> dict:
        return dict(self.stats)

    def print_summary(self):
        """Print backfill stats and Parquet inventory."""
        stats = self.get_stats()
        logger.info("=" * 60)
        logger.info("BACKFILL SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Markets processed:    {stats['markets_processed']}")
        logger.info(f"  Candlesticks fetched: {stats['candlesticks_fetched']}")
        logger.info(f"  Trades fetched:       {stats['trades_fetched']}")
        logger.info(f"  Parquet files written:{stats['parquet_files_written']}")
        logger.info(f"  Errors:               {stats['errors']}")

        # Parquet inventory
        parquet_dir = PARQUET_BASE / "candlesticks"
        if parquet_dir.exists():
            parquet_files = list(parquet_dir.rglob("*.parquet"))
            total_size = sum(f.stat().st_size for f in parquet_files)
            logger.info(f"\n  Parquet files on disk: {len(parquet_files)}")
            logger.info(f"  Parquet total size:    {total_size / 1024:.0f} KB")

            # Sample: read one file
            if parquet_files:
                sample = pq.read_table(parquet_files[0], use_legacy_dataset=True)
                logger.info(f"  Sample file: {parquet_files[0].name}")
                logger.info(f"  Sample rows: {sample.num_rows}")
                logger.info(f"  Sample columns: {sample.column_names}")

        # Mongo summary
        logger.info(f"\n  MongoDB collections:")
        for coll in self.mongo.list_collections():
            if coll["count"] > 0 and "raw" in coll["collection"]:
                logger.info(f"    {coll['collection']:25s} {coll['count']} docs")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    bf = KalshiBackfill()

    print("=" * 60)
    print("HISTORICAL BACKFILL")
    print("=" * 60)

    # Step 1: Check historical cutoff
    print("\n--- Historical Cutoff ---")
    cutoff = bf.get_historical_cutoff()

    # Step 2: Backfill candlesticks for top 20 active markets by volume
    print("\n--- Backfilling Top 20 Active Markets (1h candlesticks) ---")
    results = bf.backfill_top_markets(limit=20, period="1h", settled_only=False)

    print("\n--- Backfill Results ---")
    for ticker, count in sorted(results.items(), key=lambda x: x[1], reverse=True):
        print(f"  {ticker[:50]:52s} {count:>6} candlesticks")

    # Step 3: Backfill some historical trades
    print("\n--- Backfilling Historical Trades (5 pages) ---")
    trades = bf.backfill_trades(max_pages=5)
    print(f"  Total historical trades: {len(trades)}")

    # Summary
    print()
    bf.print_summary()
