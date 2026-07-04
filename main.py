import time
from typing import Optional, Dict
from fastapi import FastAPI, Header, HTTPException, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Enable CORS for the grader browser extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION (Assigned Values) ---
TOTAL_ORDERS = 43
RATE_LIMIT_REQUESTS = 18
RATE_LIMIT_WINDOW_SECS = 10.0

# --- STATE STORAGE (In-Memory for Stateless Deployment) ---
# Idempotency storage: maps key -> order_id
idempotency_store: Dict[str, str] = {}

# Rate limit storage: maps client_id -> list of request timestamps
rate_limit_store: Dict[str, list] = {}

# Mock Catalog generation (IDs 1 through 43)
ORDERS_CATALOG = [{"id": i, "item": f"Item {i}", "price": 10.0 + i} for i in range(1, TOTAL_ORDERS + 1)]


class OrderPayload(BaseModel):
    item: Optional[str] = "Item"
    price: Optional[float] = 10.0


# --- MIDDLEWARE FOR RATE LIMITING ---
@app.middleware("http")
async def rate_limiter(request, call_next):
    # Read the client ID header
    client_id = request.headers.get("X-Client-Id")
    
    # We only apply rate limiting if the client ID is present
    if client_id:
        current_time = time.time()
        
        # Initialize bucket if it doesn't exist
        if client_id not in rate_limit_store:
            rate_limit_store[client_id] = []
            
        # Filter out timestamps outside the active 10-second window
        timestamps = [t for t in rate_limit_store[client_id] if current_time - t < RATE_LIMIT_WINDOW_SECS]
        rate_limit_store[client_id] = timestamps
        
        # Check if client has exceeded the 18 requests threshold
        if len(timestamps) >= RATE_LIMIT_REQUESTS:
            # Calculate how long until the oldest request falls out of the window
            oldest_request = timestamps[0]
            retry_after = max(1, int(RATE_LIMIT_WINDOW_SECS - (current_time - oldest_request)))
            
            return Response(
                content="Rate limit exceeded. Too many requests.",
                status_code=429,
                headers={"Retry-After": str(retry_after)}
            )
            
        # Track this successful request
        rate_limit_store[client_id].append(current_time)

    response = await call_next(request)
    return response


# --- 1. IDEMPOTENT ORDER CREATION ---
@app.post("/orders", status_code=201)
async def create_order(payload: OrderPayload, response: Response, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key header")
        
    # Check if this exact key was used before
    if idempotency_key in idempotency_store:
        # Return the EXACT same order ID without processing or creating a new record
        existing_id = idempotency_store[idempotency_key]
        return {"id": existing_id, "status": "completed", "duplicated": True}
        
    # Generate a unique order ID for first-time creation
    new_order_id = f"ord_{int(time.time() * 1000)}"
    idempotency_store[idempotency_key] = new_order_id
    
    return {"id": new_order_id, "status": "created", "duplicated": False}


# --- 2. CURSOR PAGINATION ---
@app.get("/orders")
async def list_orders(limit: int = Query(default=10, ge=1), cursor: Optional[str] = Query(None)):
    # Simple, opaque numeric indexing string cursor logic
    start_index = 0
    if cursor:
        try:
            start_index = int(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor format")
            
    # Bound check
    if start_index >= len(ORDERS_CATALOG):
        return {"items": [], "next_cursor": None}
        
    end_index = start_index + limit
    sliced_items = ORDERS_CATALOG[start_index:end_index]
    
    # Generate the next cursor if there are still more items left in the 1..43 catalog
    next_cursor = str(end_index) if end_index < len(ORDERS_CATALOG) else None
    
    return {
        "items": sliced_items,
        "next_cursor": next_cursor
    }