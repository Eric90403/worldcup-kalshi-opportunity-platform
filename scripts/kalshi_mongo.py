"""
MongoDB connection layer for the Kalshi trading project.

Stores raw JSON API responses as immutable landing-zone documents.
Every API call is saved before any transformation — insurance for replay.

Collections:
  raw_markets       — GET /markets responses (market snapshots)
  raw_events        — GET /events responses
  raw_series        — GET /series responses
  raw_trades        — GET /markets/trades responses
  raw_candlesticks  — GET /markets/{ticker}/candlesticks responses
  raw_orderbooks    — GET /markets/{ticker}/orderbook responses
  raw_balances      — GET /portfolio/balance responses
  raw_api_errors    — Any failed API calls (for debugging)

Each document has:
  _id: ObjectId
  endpoint: the API path called
  method: GET/POST/DELETE
  params: query parameters used
  response: the raw JSON response body
  fetched_at: timestamp when we made the call
  status_code: HTTP status code
"""

from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId


MONGO_HOST = "127.0.0.1"
MONGO_PORT = 27017
MONGO_DB = "kalshi_warehouse"

# Collection names
COLL_RAW_MARKETS = "raw_markets"
COLL_RAW_EVENTS = "raw_events"
COLL_RAW_SERIES = "raw_series"
COLL_RAW_TRADES = "raw_trades"
COLL_RAW_CANDLESTICKS = "raw_candlesticks"
COLL_RAW_ORDERBOOKS = "raw_orderbooks"
COLL_RAW_BALANCES = "raw_balances"
COLL_RAW_API_ERRORS = "raw_api_errors"


class KalshiMongo:
    """MongoDB landing zone for raw Kalshi API responses."""

    def __init__(self, host=MONGO_HOST, port=MONGO_PORT, db_name=MONGO_DB):
        self.client = MongoClient(host, port, serverSelectionTimeoutMS=5000)
        self.db = self.client[db_name]
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes on commonly-queried fields."""
        for coll_name in [
            COLL_RAW_MARKETS,
            COLL_RAW_EVENTS,
            COLL_RAW_SERIES,
            COLL_RAW_TRADES,
            COLL_RAW_CANDLESTICKS,
            COLL_RAW_ORDERBOOKS,
            COLL_RAW_BALANCES,
        ]:
            coll = self.db[coll_name]
            coll.create_index([("fetched_at", DESCENDING)])
            coll.create_index([("endpoint", ASCENDING)])

    def store_raw(
        self,
        collection: str,
        endpoint: str,
        method: str,
        response: dict | list,
        params: dict = None,
        status_code: int = 200,
    ):
        """Store a raw API response.

        Args:
            collection: Which collection to store in
            endpoint: API path (e.g. "/markets")
            method: HTTP method
            response: The parsed JSON response
            params: Query parameters used
            status_code: HTTP status code

        Returns:
            The inserted document's _id
        """
        doc = {
            "endpoint": endpoint,
            "method": method,
            "params": params or {},
            "response": response,
            "status_code": status_code,
            "fetched_at": datetime.now(timezone.utc),
        }
        result = self.db[collection].insert_one(doc)
        return result.inserted_id

    def store_error(
        self,
        endpoint: str,
        method: str,
        error: str,
        params: dict = None,
        status_code: int = None,
    ):
        """Store a failed API call for debugging."""
        doc = {
            "endpoint": endpoint,
            "method": method,
            "params": params or {},
            "error": error,
            "status_code": status_code,
            "fetched_at": datetime.now(timezone.utc),
        }
        result = self.db[COLL_RAW_API_ERRORS].insert_one(doc)
        return result.inserted_id

    def get_latest(self, collection: str, endpoint: str = None):
        """Get the most recent raw response from a collection."""
        query = {"endpoint": endpoint} if endpoint else {}
        return self.db[collection].find_one(query, sort=[("fetched_at", DESCENDING)])

    def count(self, collection: str):
        """Count documents in a collection."""
        return self.db[collection].count_documents({})

    def ping(self):
        """Test the connection."""
        try:
            result = self.client.admin.command("ping")
            return True, result
        except Exception as e:
            return False, str(e)

    def list_collections(self):
        """List all collections with document counts."""
        info = []
        for coll_name in sorted(self.db.list_collection_names()):
            count = self.db[coll_name].count_documents({})
            info.append({"collection": coll_name, "count": count})
        return info


if __name__ == "__main__":
    print("Testing MongoDB connection for Kalshi warehouse...")
    mongo = KalshiMongo()

    ok, result = mongo.ping()
    print(f"Ping: {'OK' if ok else 'FAILED'} — {result}")
    if not ok:
        exit(1)

    print(f"\nCollections:")
    for coll in mongo.list_collections():
        print(f"  {coll['collection']:25s} {coll['count']} docs")

    # End-to-end test: store a raw balance response and read it back
    print("\n=== END-TO-END TEST ===")
    test_response = {
        "balance": 1000,
        "balance_dollars": "10.0000",
        "portfolio_value": 0,
    }
    doc_id = mongo.store_raw(
        collection=COLL_RAW_BALANCES,
        endpoint="/portfolio/balance",
        method="GET",
        response=test_response,
    )
    print(f"Stored test document: {doc_id}")

    latest = mongo.get_latest(COLL_RAW_BALANCES, "/portfolio/balance")
    print(f"Retrieved: {latest['response']}")
    print(f"Fetched at: {latest['fetched_at']}")

    # Clean up test doc
    mongo.db[COLL_RAW_BALANCES].delete_one({"_id": doc_id})
    print("Test document cleaned up.")
    print("\nMongoDB landing zone is ready.")
