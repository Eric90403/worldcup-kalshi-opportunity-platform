"""
Kalshi WebSocket client for real-time data streaming.

Subscribes to public market data channels (ticker, trade, orderbook_delta)
and stores all messages to MongoDB. WebSocket subscriptions are NOT rate-limited
— they don't consume API tokens. This is our primary real-time data pipe.

Channels:
  ticker           — price, volume, OI updates for all markets
  trade            — public trade notifications
  orderbook_delta  — incremental orderbook changes (per-market)
  market_lifecycle_v2 — market state changes (open, close, settle)

Usage:
  from kalshi_ws import KalshiWSClient
  client = KalshiWSClient()
  client.subscribe_ticker()               # all markets
  client.subscribe_trades(["TICKER1"])     # specific markets
  client.subscribe_orderbook(["TICKER1"])
  await client.run()                       # blocks, processes messages
"""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from kalshi_mongo import KalshiMongo

logger = logging.getLogger(__name__)

WS_PRODUCTION = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_DEMO = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"


class KalshiWSClient:
    """Authenticated WebSocket client for Kalshi real-time data.

    Handles auth handshake, subscriptions, reconnection, and MongoDB storage.
    Runs as an async event loop — call run() to start streaming.
    """

    def __init__(self, demo=False, mongo=None):
        self.ws_url = WS_DEMO if demo else WS_PRODUCTION
        self.demo = demo

        # Load credentials from pass
        import subprocess
        result = subprocess.run(["pass", "show", "kalshi"], capture_output=True, text=True)
        self.api_key_id = result.stdout.strip()
        result = subprocess.run(["pass", "show", "kalshi-private-key"], capture_output=True, text=True)
        self.private_key = serialization.load_pem_private_key(
            result.stdout.strip().encode("utf-8"),
            password=None,
            backend=default_backend(),
        )

        self.mongo = mongo or KalshiMongo()
        self.websocket = None
        self.message_id = 1
        self.subscriptions = {}  # sid -> channel info
        self.stats = {
            "messages_received": 0,
            "ticker_updates": 0,
            "trade_updates": 0,
            "orderbook_updates": 0,
            "lifecycle_updates": 0,
            "errors": 0,
            "reconnects": 0,
        }
        self._should_run = False
        self._pending_subscriptions = []

    def _sign(self, timestamp: str, method: str, path: str) -> str:
        """RSA-PSS signature for WebSocket handshake."""
        path_clean = path.split("?")[0]
        message = f"{timestamp}{method}{path_clean}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _make_auth_headers(self) -> dict:
        """Build auth headers for the WebSocket handshake."""
        timestamp = str(int(time.time() * 1000))
        signature = self._sign(timestamp, "GET", WS_SIGN_PATH)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def subscribe_ticker(self, market_tickers=None, send_initial_snapshot=True):
        """Queue a ticker subscription. Call before run() or use async subscribe()."""
        params = {
            "channels": ["ticker"],
            "send_initial_snapshot": send_initial_snapshot,
        }
        if market_tickers:
            params["market_tickers"] = market_tickers
        self._pending_subscriptions.append(params)

    def subscribe_trades(self, market_tickers=None):
        """Queue a trade subscription."""
        params = {"channels": ["trade"]}
        if market_tickers:
            params["market_tickers"] = market_tickers
        self._pending_subscriptions.append(params)

    def subscribe_orderbook(self, market_tickers, use_yes_price=True):
        """Queue an orderbook delta subscription."""
        params = {
            "channels": ["orderbook_delta"],
            "market_tickers": market_tickers,
            "use_yes_price": use_yes_price,
        }
        self._pending_subscriptions.append(params)

    def subscribe_lifecycle(self):
        """Queue a market lifecycle subscription."""
        self._pending_subscriptions.append({"channels": ["market_lifecycle_v2"]})

    async def _send_subscribe(self, params: dict):
        """Send a subscribe command to the WebSocket."""
        cmd = {
            "id": self.message_id,
            "cmd": "subscribe",
            "params": params,
        }
        self.message_id += 1
        await self.websocket.send(json.dumps(cmd))
        logger.info(f"Sent subscribe: {params['channels']}")

    async def _process_message(self, raw_message: str):
        """Process a single WebSocket message."""
        self.stats["messages_received"] += 1

        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning(f"Non-JSON message: {raw_message[:200]}")
            return

        msg_type = data.get("type", "")
        msg = data.get("msg", {})

        # Store all messages to MongoDB (raw landing zone)
        self.mongo.db["ws_messages"].insert_one({
            "type": msg_type,
            "msg": msg,
            "raw": data,
            "received_at": datetime.now(timezone.utc),
        })

        if msg_type == "subscribed":
            sid = msg.get("sid")
            channel = msg.get("channel")
            self.subscriptions[sid] = channel
            logger.info(f"  Subscribed (sid={sid}): {channel}")

        elif msg_type == "ticker":
            self.stats["ticker_updates"] += 1
            ticker = msg.get("market_ticker", "?")
            bid = msg.get("yes_bid_dollars", "?")
            ask = msg.get("yes_ask_dollars", "?")
            vol = msg.get("volume_fp", "?")
            if self.stats["ticker_updates"] % 100 == 0:
                logger.info(
                    f"  [ticker #{self.stats['ticker_updates']}] "
                    f"{ticker[:40]} bid={bid} ask={ask} vol={vol}"
                )

        elif msg_type == "trade":
            self.stats["trade_updates"] += 1
            ticker = msg.get("ticker", "?")
            price = msg.get("yes_price_dollars", "?")
            count = msg.get("count_fp", "?")
            side = msg.get("taker_outcome_side", "?")
            logger.info(
                f"  [trade] {ticker[:40]} price={price} count={count} side={side}"
            )

        elif msg_type in ("orderbook_snapshot", "orderbook_delta"):
            self.stats["orderbook_updates"] += 1
            if self.stats["orderbook_updates"] % 100 == 0:
                ticker = msg.get("market_ticker", "?")
                logger.info(
                    f"  [orderbook #{self.stats['orderbook_updates']}] "
                    f"{msg_type} for {ticker[:40]}"
                )

        elif msg_type == "market_lifecycle_v2":
            self.stats["lifecycle_updates"] += 1
            ticker = msg.get("market_ticker", "?")
            status = msg.get("status", "?")
            logger.info(f"  [lifecycle] {ticker[:40]} → {status}")

        elif msg_type == "error":
            self.stats["errors"] += 1
            code = msg.get("code", "?")
            error_msg = msg.get("msg", "?")
            logger.error(f"  [WS ERROR] code={code}: {error_msg}")

        elif msg_type == "orderbook_snapshot":
            pass  # already stored

        elif msg_type == "unsubscribed":
            sid = msg.get("sid")
            logger.info(f"  Unsubscribed sid={sid}")

    async def run(self, duration_seconds=None):
        """Connect, subscribe, and process messages.

        Args:
            duration_seconds: If set, disconnect after this many seconds.
                              If None, runs forever until interrupted.
        """
        self._should_run = True
        reconnect_delay = 1.0
        max_reconnect_delay = 60.0

        while self._should_run:
            try:
                headers = self._make_auth_headers()
                logger.info(f"Connecting to {self.ws_url}...")

                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=10,
                    ping_timeout=20,
                    close_timeout=10,
                ) as websocket:
                    self.websocket = websocket
                    self.stats["reconnects"] += 1 if self.stats["reconnects"] > 0 or len(self.subscriptions) > 0 else 0
                    logger.info("Connected! Sending subscriptions...")

                    # Send all pending subscriptions
                    for params in self._pending_subscriptions:
                        await self._send_subscribe(params)
                    self._pending_subscriptions.clear()

                    logger.info("Streaming. Press Ctrl+C to stop.")

                    # Process messages
                    start_time = time.time()
                    async for raw_message in websocket:
                        if not self._should_run:
                            break
                        await self._process_message(raw_message)

                        if duration_seconds and (time.time() - start_time) > duration_seconds:
                            logger.info(f"Duration limit reached ({duration_seconds}s)")
                            break

                reconnect_delay = 1.0  # Reset on clean disconnect

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Connection closed: {e.code} {e.reason}")
                if self._should_run:
                    logger.info(f"Reconnecting in {reconnect_delay:.1f}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self.stats["errors"] += 1
                if self._should_run:
                    logger.info(f"Reconnecting in {reconnect_delay:.1f}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        logger.info(f"WebSocket client stopped. Stats: {self.get_stats()}")

    def stop(self):
        """Signal the client to stop."""
        self._should_run = False

    def get_stats(self) -> dict:
        return dict(self.stats)

    def print_mongo_summary(self):
        """Print MongoDB collection counts for WS data."""
        for coll_info in self.mongo.list_collections():
            if coll_info["count"] > 0:
                logger.info(f"  {coll_info['collection']:25s} {coll_info['count']} docs")


async def _test_run():
    """Test the WebSocket client for 60 seconds."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    client = KalshiWSClient()

    # Subscribe to ticker for all markets, trade feed, and lifecycle
    client.subscribe_ticker(send_initial_snapshot=False)
    client.subscribe_trades()
    client.subscribe_lifecycle()

    logger.info("Starting 60-second WebSocket test...")
    await client.run(duration_seconds=60)

    logger.info("\n" + "=" * 60)
    logger.info("WEBSOCKET TEST COMPLETE")
    logger.info("=" * 60)
    stats = client.get_stats()
    logger.info(f"  Total messages:   {stats['messages_received']}")
    logger.info(f"  Ticker updates:   {stats['ticker_updates']}")
    logger.info(f"  Trade updates:    {stats['trade_updates']}")
    logger.info(f"  Orderbook updates:{stats['orderbook_updates']}")
    logger.info(f"  Lifecycle updates:{stats['lifecycle_updates']}")
    logger.info(f"  Errors:           {stats['errors']}")
    logger.info(f"  Reconnects:       {stats['reconnects']}")

    logger.info("\n--- MongoDB WS Collections ---")
    client.print_mongo_summary()


if __name__ == "__main__":
    asyncio.run(_test_run())
