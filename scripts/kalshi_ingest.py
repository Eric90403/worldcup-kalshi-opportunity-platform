"""
Kalshi data ingestion client.

Rate-limit-aware REST polling with cursor-based pagination.
Every response is saved to MongoDB (raw landing zone) before any processing.

Rate limits (Basic tier):
  Read:  200 tokens/s ÷ 10 tokens per request = 20 requests/s
  Write: 100 tokens/s ÷ 10 tokens per request = 10 requests/s

We throttle conservatively to stay well under the limit and handle 429s
with exponential backoff.

Usage:
  from kalshi_ingest import KalshiIngester
  ingester = KalshiIngester()

  # Fetch all open markets (paginated)
  markets = ingester.fetch_all_markets(status="open")

  # Fetch all series
  series = ingester.fetch_all_series()

  # Fetch all events
  events = ingester.fetch_all_events()

  # Fetch trades for a specific market
  trades = ingester.fetch_trades(ticker="KXHIGHNY-26JUN26-T115")

  # Fetch orderbook for a market
  orderbook = ingester.fetch_orderbook(ticker="KXHIGHNY-26JUN26-T115")

  # Fetch candlesticks
  candles = ingester.fetch_candlesticks(
      ticker="KXHIGHNY-26JUN26-T115",
      period="1h",
      start_ts=1700000000,
      end_ts=1700086400,
  )
"""

import time
import logging
from datetime import datetime, timezone

from kalshi_auth import KalshiClient
from kalshi_mongo import (
    KalshiMongo,
    COLL_RAW_MARKETS,
    COLL_RAW_EVENTS,
    COLL_RAW_SERIES,
    COLL_RAW_TRADES,
    COLL_RAW_CANDLESTICKS,
    COLL_RAW_ORDERBOOKS,
    COLL_RAW_BALANCES,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter for Kalshi API.

    Basic tier: 200 read tokens/s, 100 write tokens/s.
    Default request cost: 10 tokens.
    So: 20 reads/s, 10 writes/s.

    We set conservative limits below the theoretical max to leave headroom.
    """

    def __init__(self, read_per_second=15, write_per_second=8):
        self.read_interval = 1.0 / read_per_second
        self.write_interval = 1.0 / write_per_second
        self._last_read = 0.0
        self._last_write = 0.0

    def wait_read(self):
        now = time.monotonic()
        elapsed = now - self._last_read
        if elapsed < self.read_interval:
            time.sleep(self.read_interval - elapsed)
        self._last_read = time.monotonic()

    def wait_write(self):
        now = time.monotonic()
        elapsed = now - self._last_write
        if elapsed < self.write_interval:
            time.sleep(self.write_interval - elapsed)
        self._last_write = time.monotonic()


class KalshiIngester:
    """Rate-limit-aware Kalshi API ingester with pagination and MongoDB landing."""

    # Maximum retries on 429 / 5xx
    MAX_RETRIES = 5
    # Base backoff in seconds
    BACKOFF_BASE = 1.0

    def __init__(self, demo=False):
        self.client = KalshiClient(demo=demo)
        self.mongo = KalshiMongo()
        self.limiter = RateLimiter()
        self.stats = {
            "requests_made": 0,
            "pages_fetched": 0,
            "items_fetched": 0,
            "retries": 0,
            "errors": 0,
        }

    def _get_with_retry(self, path: str, params: dict = None) -> dict:
        """Make a GET request with rate limiting and exponential backoff on 429/5xx/timeout."""
        for attempt in range(self.MAX_RETRIES):
            self.limiter.wait_read()

            try:
                self.stats["requests_made"] += 1
                response = self.client.get(path, params=params)
                return response
            except Exception as e:
                err_str = str(e)

                if "429" in err_str or "Too Many Requests" in err_str:
                    # Rate limited — exponential backoff
                    wait = self.BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        f"Rate limited on {path}, backing off {wait:.1f}s "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                    )
                    self.stats["retries"] += 1
                    time.sleep(wait)
                    continue

                if "500" in err_str or "502" in err_str or "503" in err_str:
                    # Server error — retry with backoff
                    wait = self.BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        f"Server error on {path}, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                    )
                    self.stats["retries"] += 1
                    time.sleep(wait)
                    continue

                if "Timeout" in err_str or "timed out" in err_str:
                    # Read timeout — retry with backoff and longer timeout
                    wait = self.BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        f"Timeout on {path}, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                    )
                    self.stats["retries"] += 1
                    time.sleep(wait)
                    continue

                # Non-retryable error
                self.stats["errors"] += 1
                self.mongo.store_error(
                    endpoint=path,
                    method="GET",
                    error=err_str,
                    params=params,
                )
                raise

        # Exhausted retries
        self.stats["errors"] += 1
        self.mongo.store_error(
            endpoint=path,
            method="GET",
            error=f"Exhausted {self.MAX_RETRIES} retries",
            params=params,
        )
        raise RuntimeError(f"Exhausted {self.MAX_RETRIES} retries on {path}")

    def _paginate(
        self,
        path: str,
        data_key: str,
        params: dict = None,
        max_pages: int = None,
    ) -> list:
        """Fetch all pages from a cursor-based paginated endpoint.

        Args:
            path: API path (e.g. "/markets")
            data_key: Key in response containing the items array (e.g. "markets")
            params: Additional query parameters
            max_pages: Safety limit on number of pages (None = unlimited)

        Returns:
            List of all items across all pages
        """
        all_items = []
        cursor = None
        page_num = 0

        while True:
            page_params = dict(params or {})
            page_params["limit"] = 100
            if cursor:
                page_params["cursor"] = cursor

            response = self._get_with_retry(path, params=page_params)
            self.stats["pages_fetched"] += 1
            page_num += 1

            # Store raw response in Mongo
            self.mongo.store_raw(
                collection=self._collection_for(path),
                endpoint=path,
                method="GET",
                response=response,
                params=page_params,
            )

            items = response.get(data_key, [])
            all_items.extend(items)
            self.stats["items_fetched"] += len(items)

            cursor = response.get("cursor")

            if not cursor:
                break

            if max_pages and page_num >= max_pages:
                logger.info(f"Reached max_pages limit ({max_pages}) on {path}")
                break

            if page_num % 10 == 0:
                logger.info(
                    f"  {path}: page {page_num}, {len(all_items)} items so far..."
                )

        return all_items

    @staticmethod
    def _collection_for(path: str) -> str:
        """Map API path to MongoDB collection name."""
        if "/markets" in path and "/trades" not in path and "candlestick" not in path:
            if "/orderbook" in path:
                return COLL_RAW_ORDERBOOKS
            return COLL_RAW_MARKETS
        if "/events" in path:
            return COLL_RAW_EVENTS
        if "/series" in path:
            return COLL_RAW_SERIES
        if "/trades" in path:
            return COLL_RAW_TRADES
        if "candlestick" in path:
            return COLL_RAW_CANDLESTICKS
        if "/balance" in path:
            return COLL_RAW_BALANCES
        return "raw_misc"

    # ---- High-level fetch methods ----

    def fetch_all_markets(self, status: str = None, max_pages: int = None) -> list:
        """Fetch all markets, optionally filtered by status.

        Args:
            status: Filter by market status (unopened, open, closed, settled)
            max_pages: Safety limit on pagination

        Returns:
            List of market objects
        """
        params = {}
        if status:
            params["status"] = status

        logger.info(f"Fetching markets (status={status or 'all'})...")
        markets = self._paginate("/markets", "markets", params, max_pages)
        logger.info(f"Fetched {len(markets)} markets")
        return markets

    def fetch_all_series(self, max_pages: int = None) -> list:
        """Fetch all series (recurring event templates)."""
        logger.info("Fetching series...")
        series = self._paginate("/series", "series", max_pages=max_pages)
        logger.info(f"Fetched {len(series)} series")
        return series

    def fetch_all_events(self, status: str = None, max_pages: int = None) -> list:
        """Fetch all events."""
        params = {}
        if status:
            params["status"] = status

        logger.info(f"Fetching events (status={status or 'all'})...")
        events = self._paginate("/events", "events", params, max_pages)
        logger.info(f"Fetched {len(events)} events")
        return events

    def fetch_trades(self, ticker: str = None, max_pages: int = None) -> list:
        """Fetch trades, optionally filtered by market ticker.

        Args:
            ticker: Market ticker filter
            max_pages: Safety limit
        """
        params = {}
        if ticker:
            params["ticker"] = ticker

        logger.info(f"Fetching trades (ticker={ticker or 'all'})...")
        trades = self._paginate("/markets/trades", "trades", params, max_pages)
        logger.info(f"Fetched {len(trades)} trades")
        return trades

    def fetch_orderbook(self, ticker: str) -> dict:
        """Fetch the current orderbook for a specific market."""
        logger.info(f"Fetching orderbook for {ticker}...")
        response = self._get_with_retry(
            f"/markets/{ticker}/orderbook"
        )

        self.mongo.store_raw(
            collection=COLL_RAW_ORDERBOOKS,
            endpoint=f"/markets/{ticker}/orderbook",
            method="GET",
            response=response,
            params={"ticker": ticker},
        )

        return response

    def fetch_candlesticks(
        self,
        ticker: str,
        period: str = "1h",
        start_ts: int = None,
        end_ts: int = None,
        max_pages: int = None,
    ) -> list:
        """Fetch candlestick data for a market.

        Args:
            ticker: Market ticker
            period: Candlestick period — "1m", "1h", or "1d"
            start_ts: Unix timestamp start
            end_ts: Unix timestamp end
            max_pages: Safety limit
        """
        params = {"period": period}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts

        logger.info(f"Fetching candlesticks for {ticker} (period={period})...")
        # Candlesticks endpoint path varies — uses series/market path
        path = f"/series/markets/{ticker}/candlesticks"
        candles = self._paginate(path, "candlesticks", params, max_pages)
        logger.info(f"Fetched {len(candles)} candlesticks for {ticker}")
        return candles

    def fetch_balance(self) -> dict:
        """Fetch current account balance."""
        response = self._get_with_retry("/portfolio/balance")

        self.mongo.store_raw(
            collection=COLL_RAW_BALANCES,
            endpoint="/portfolio/balance",
            method="GET",
            response=response,
        )

        return response

    def fetch_account_limits(self) -> dict:
        """Fetch current API tier and rate limits."""
        response = self._get_with_retry("/account/limits")
        return response

    def fetch_exchange_status(self) -> dict:
        """Fetch exchange status."""
        response = self._get_with_retry("/exchange/status")
        return response

    def get_stats(self) -> dict:
        """Return ingestion statistics."""
        return dict(self.stats)


if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ingester = KalshiIngester()

    print("=" * 60)
    print("KALSHI INGESTION TEST")
    print("=" * 60)

    # Check exchange status
    print("\n--- Exchange Status ---")
    status = ingester.fetch_exchange_status()
    print(json.dumps(status, indent=2))

    # Check our tier
    print("\n--- Account Limits ---")
    limits = ingester.fetch_account_limits()
    print(f"  Tier: {limits.get('usage_tier')}")
    print(f"  Read:  {limits.get('read', {}).get('refill_rate')} tokens/s")
    print(f"  Write: {limits.get('write', {}).get('refill_rate')} tokens/s")

    # Fetch balance
    print("\n--- Balance ---")
    balance = ingester.fetch_balance()
    print(f"  Balance: ${balance.get('balance_dollars', '0.00')}")

    # Fetch all series (small dataset, good first test)
    print("\n--- Fetching All Series ---")
    series = ingester.fetch_all_series()
    print(f"  Total series: {len(series)}")
    if series:
        print(f"  Sample series:")
        for s in series[:5]:
            print(f"    {s.get('ticker', '?'):30s} {s.get('title', '?')[:60]}")
        print(f"    ... and {len(series) - 5} more" if len(series) > 5 else "")

    # Fetch open markets (the ones we'd trade)
    print("\n--- Fetching Open Markets ---")
    markets = ingester.fetch_all_markets(status="open")
    print(f"  Total open markets: {len(markets)}")
    if markets:
        print(f"  Sample markets:")
        for m in markets[:5]:
            ticker = m.get("ticker", "?")
            title = m.get("title", "?")[:50]
            vol = m.get("volume_24h_fp", "0")
            price = m.get("last_price_dollars", "?")
            print(f"    {ticker:45s} ${price:>6s}  vol24h={vol}")
        print(f"    ... and {len(markets) - 5} more" if len(markets) > 5 else "")

    # Fetch all events
    print("\n--- Fetching All Events ---")
    events = ingester.fetch_all_events()
    print(f"  Total events: {len(events)}")
    if events:
        print(f"  Sample events:")
        for e in events[:5]:
            print(f"    {e.get('ticker', '?'):35s} {e.get('title', '?')[:55]}")

    # Print stats
    print("\n" + "=" * 60)
    print("INGESTION STATS")
    print("=" * 60)
    stats = ingester.get_stats()
    print(f"  API requests:  {stats['requests_made']}")
    print(f"  Pages fetched: {stats['pages_fetched']}")
    print(f"  Items fetched: {stats['items_fetched']}")
    print(f"  Retries:       {stats['retries']}")
    print(f"  Errors:        {stats['errors']}")

    # Mongo summary
    print(f"\n--- MongoDB Landing Zone ---")
    for coll in ingester.mongo.list_collections():
        if coll["count"] > 0:
            print(f"  {coll['collection']:25s} {coll['count']} docs")
