import os
import json
import time
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# --- ASSIGNED CONFIGURATION ---
TOTAL_ORDERS = 43
RATE_LIMIT_REQUESTS = 18
RATE_LIMIT_WINDOW_SECS = 10.0

# Fixed path inside Vercel's writable temporary environment
IDEMPOTENCY_FILE = "/tmp/idempotency_store.json"
RATELIMIT_FILE = "/tmp/rate_limit_store.json"

# Static catalog generation
ORDERS_CATALOG = [{"id": i, "item": f"Item {i}", "price": 10.0 + i} for i in range(1, TOTAL_ORDERS + 1)]

class OrderPayload(BaseModel):
    item: Optional[str] = "Item"
    price: Optional[float] = 10.0

# --- PERSISTENT DISK STATE HELPER FUNCTIONS ---
def load_state(filepath: str) -> dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(filepath: str, data: dict):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

# --- CORS RESPONSE WRAPPER FOR ERRORS & INTERCEPTIONS ---
def make_cors_response(content: str, status_code: int, retry_after: Optional[int] = None) -> Response:
    res = Response(content=content, status_code=status_code)
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Access-Control-Allow-Headers"] = "*"
    res.headers["Access-Control-Allow-Methods"] = "*"
    res.headers["Access-Control-Expose-Headers"] = "Retry-After"
    if retry_after is not None:
        res.headers["Retry-After"] = str(retry_after)
    return res

# --- RATE LIMIT MIDDLEWARE ---
@app.middleware("http")
async def rate_limiter(request, call_next):
    # Always let browser preflight OPTIONS checks slip through instantly
    if request.method == "OPTIONS":
        return await call_next(request)
        
    client_id = request.headers.get("X-Client-Id")
    if client_id:
        current_time = time.time()
        state = load_state(RATELIMIT_FILE)
        
        timestamps = state.get(client_id, [])
        # Keep only timestamps within the active sliding window
        timestamps = [t for t in timestamps if current_time - t < RATE_LIMIT_WINDOW_SECS]
        
        if len(timestamps) >= RATE_LIMIT_REQUESTS:
            oldest_request = timestamps[0]
            retry_after = max(1, int(RATE_LIMIT_WINDOW_SECS - (current_time - oldest_request)))
            return make_cors_response("Rate limit exceeded.", 429, retry_after)
            
        timestamps.append(current_time)
        state[client_id] = timestamps
        save_state(RATELIMIT_FILE, state)

    response = await call_next(request)
    
    # Inject full CORS headers to outbound successful responses
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    return response

# Standard Fallback CORS Middleware Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 1. IDEMPOTENT ORDER CREATION ---
@app.post("/orders", status_code=201)
async def create_order(payload: OrderPayload, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    if not idempotency_key:
        return make_cors_response("Missing Idempotency-Key header", 400)
        
    state = load_state(IDEMPOTENCY_FILE)
    
    # If key exists, return the identical payload immediately
    if idempotency_key in state:
        return {
            "id": state[idempotency_key],
            "status": "completed",
            "duplicated": True
        }
        
    # Generate a new unique record order ID
    new_order_id = f"ord_{int(time.time() * 1000)}"
    state[idempotency_key] = new_order_id
    save_state(IDEMPOTENCY_FILE, state)
    
    return {
        "id": new_order_id,
        "status": "created",
        "duplicated": False
    }

# --- 2. CURSOR PAGINATION ---
@app.get("/orders")
async def list_orders(limit: int = Query(default=10, ge=1), cursor: Optional[str] = Query(None)):
    start_index = 0
    if cursor:
        try:
            start_index = int(cursor)
        except ValueError:
            return make_cors_response("Invalid cursor format", 400)
            
    if start_index >= len(ORDERS_CATALOG):
        return {"items": [], "next_cursor": None}
        
    end_index = start_index + limit
    sliced_items = ORDERS_CATALOG[start_index:end_index]
    next_cursor = str(end_index) if end_index < len(ORDERS_CATALOG) else None
    
    return {
        "items": sliced_items,
        "next_cursor": next_cursor
    }