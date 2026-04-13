from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from starlette.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import json
import logging
import string
import random
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── MongoDB Connection ────────────────────────────────────────
db = None
mongo_client = None

# ── Device Registry ──────────────────────────────────────────
# CRITICAL: Devices are now stored in MongoDB, NOT in-memory
# These dicts are ONLY for live WebSocket connections
host_ws  = {}   # device_id -> WebSocket  (live host connection)
viewer_ws = {}  # device_id -> list[WebSocket]  (multiple viewers per device)
viewer_disconnect_tasks = {}  # device_id -> asyncio.Task (delayed disconnect)
viewer_last_seen = {}  # device_id -> dict[WebSocket, datetime]


def gen_token():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=32))


async def delayed_disconnect(device_id: str):
    """
    Grace period before notifying host that all viewers disconnected.
    """
    logger.info(f"[DELAYED_DISCONNECT] Starting 5s grace period for device {device_id}")
    await asyncio.sleep(5)
    
    viewers = viewer_ws.get(device_id, [])
    if not viewers:
        logger.info(f"[DELAYED_DISCONNECT] Grace period expired, no viewers for device {device_id}")
        hws = host_ws.get(device_id)
        if hws:
            try:
                await hws.send_json({"type": "viewer_disconnected"})
                logger.info(f"[SESSION] Sent viewer_disconnected to host for device {device_id}")
            except Exception as e:
                logger.error(f"[DELAYED_DISCONNECT] Failed to notify host {device_id}: {e}")
    else:
        logger.info(f"[DELAYED_DISCONNECT] Viewer(s) still connected for device {device_id}, cancelling disconnect")
    
    if device_id in viewer_disconnect_tasks:
        del viewer_disconnect_tasks[device_id]


def device_status(device_id: str) -> str:
    """
    Single source of truth for online/offline.
    A device is ONLINE if and only if there is an active WebSocket in host_ws.
    """
    return "online" if device_id in host_ws else "offline"


async def update_device_status(device_id: str, status: str, update_last_seen: bool = True):
    """
    Update device status in MongoDB.
    """
    if db is None:
        logger.warning(f"[DB] Cannot update status for {device_id} - MongoDB unavailable")
        return
    
    try:
        update_doc = {"status": status}
        if update_last_seen:
            update_doc["last_seen"] = datetime.now(timezone.utc).isoformat()
        
        await db.devices.update_one(
            {"device_id": device_id},
            {"$set": update_doc}
        )
        logger.info(f"[DB] Updated device {device_id}: status={status}")
    except Exception as e:
        logger.error(f"[DB] Failed to update device {device_id}: {e}")


async def heartbeat_loop():
    """
    Safety-net loop: closes zombie WebSocket connections that stopped sending
    pings. A healthy host pings every 5 s; we allow 20 s before evicting.
    """
    while True:
        await asyncio.sleep(10)
        
        if db is None:
            continue
        
        now = datetime.now(timezone.utc)
        stale = []
        
        try:
            # Fetch all online devices from MongoDB
            cursor = db.devices.find({"device_id": {"$in": list(host_ws.keys())}})
            async for dev in cursor:
                did = dev["device_id"]
                if did in host_ws:
                    last = datetime.fromisoformat(dev["last_seen"])
                    age = (now - last).total_seconds()
                    if age > 20:
                        logger.warning(f"[HEARTBEAT] Device {did} stale ({age:.0f}s) — closing WS")
                        stale.append(did)
        except Exception as e:
            logger.error(f"[HEARTBEAT] Error checking devices: {e}")
            continue
        
        for did in stale:
            ws = host_ws.pop(did, None)
            if ws:
                try:
                    await ws.close()
                except:
                    pass
            await update_device_status(did, "offline")


async def viewer_heartbeat_loop():
    """
    Monitor viewer connections for timeouts.
    """
    while True:
        await asyncio.sleep(15)
        now = datetime.now(timezone.utc)
        
        for did in list(viewer_ws.keys()):
            viewers = viewer_ws.get(did, [])
            stale = []
            last_seen_map = viewer_last_seen.get(did, {})
            for ws in list(viewers):
                last = last_seen_map.get(id(ws))
                if last:
                    age = (now - last).total_seconds()
                    if age > 30:
                        logger.warning(f"[HEARTBEAT] Viewer for device {did} timeout ({age:.0f}s)")
                        stale.append(ws)
            for ws in stale:
                try:
                    await ws.close()
                except Exception:
                    pass
                if did in viewer_ws and ws in viewer_ws[did]:
                    viewer_ws[did].remove(ws)
                if did in viewer_last_seen:
                    viewer_last_seen[did].pop(id(ws), None)
            
            # If no viewers left, schedule disconnect notification
            if did in viewer_ws and not viewer_ws[did]:
                del viewer_ws[did]
                if did not in viewer_disconnect_tasks:
                    viewer_disconnect_tasks[did] = asyncio.create_task(delayed_disconnect(did))


async def init_mongodb():
    """Initialize MongoDB connection on startup with graceful degradation"""
    global db, mongo_client
    
    mongo_url = os.getenv('MONGO_URL', 'mongodb://localhost:27017')
    db_name = os.getenv('DB_NAME', 'remotedesktop')
    
    try:
        logger.info(f"[MONGODB] Connecting to {mongo_url} (database: {db_name})")
        mongo_client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
        
        # Test connection
        await mongo_client.admin.command('ping')
        
        db = mongo_client[db_name]
        
        # Create indexes for devices collection
        await db.devices.create_index('device_id', unique=True)
        logger.info("[MONGODB] ✅ Created unique index on devices.device_id")
        
        # Create indexes for device_notes collection
        await db.device_notes.create_index('device_id', unique=True)
        logger.info("[MONGODB] ✅ Created unique index on device_notes.device_id")
        
        # Create indexes for device_screenshots collection
        await db.device_screenshots.create_index('device_id', unique=True)
        logger.info("[MONGODB] ✅ Created unique index on device_screenshots.device_id")
        
        logger.info(f"[MONGODB] ✅ Connected successfully to database: {db_name}")
    except Exception as e:
        logger.error(f"[MONGODB] ❌ Connection failed: {e}")
        logger.warning("[MONGODB] ⚠️  Running with degraded functionality (no persistence)")
        db = None
        mongo_client = None


@app.on_event("startup")
async def on_startup():
    await init_mongodb()
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(viewer_heartbeat_loop())


@app.on_event("shutdown")
async def on_shutdown():
    """Cleanup MongoDB connection on shutdown"""
    global mongo_client
    if mongo_client:
        logger.info("[MONGODB] Closing connection")
        mongo_client.close()


# ── Pydantic Models ──────────────────────────────────────────
class RegisterBody(BaseModel):
    device_id: str
    device_name: str
    auth_token: Optional[str] = None


class DeviceNoteRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=100)
    note: str = Field(..., max_length=10000)


class DeviceNoteResponse(BaseModel):
    device_id: str
    note: str
    updated_at: str


class DeviceScreenshotRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=100)
    image: str = Field(..., min_length=1)
    
    @field_validator('image')
    @classmethod
    def validate_image(cls, v):
        """Validate base64 encoding and size limit"""
        import base64
        
        # Validate base64 encoding
        try:
            # Handle data URL format (data:image/jpeg;base64,...)
            image_data = v.split(',')[1] if ',' in v else v
            base64.b64decode(image_data)
        except Exception:
            raise ValueError('Invalid base64 encoding')
        
        # Enforce 200KB size limit (266KB base64 due to ~33% encoding overhead)
        if len(v) > 266 * 1024:
            raise ValueError('Image too large (max 200KB)')
        
        return v


class DeviceScreenshotResponse(BaseModel):
    device_id: str
    image: str
    updated_at: str


# ── Helper Functions ─────────────────────────────────────────
async def safe_mongo_operation(operation):
    """Wrapper for MongoDB operations with graceful degradation"""
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
    try:
        return await operation()
    except HTTPException:
        # Re-raise HTTPExceptions (like 404) without wrapping
        raise
    except Exception as e:
        logger.error(f"MongoDB operation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Database operation failed"
        )


# ── REST Endpoints ───────────────────────────────────────────
@api_router.patch("/devices/{device_id}/rename")
async def rename_device(device_id: str, body: dict):
    """Rename a device's display name. Device ID stays the same."""
    new_name = (body.get("device_name") or "").strip()
    if not new_name or len(new_name) > 100:
        raise HTTPException(status_code=400, detail="Invalid device name (1-100 chars)")
    
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    
    result = await db.devices.update_one(
        {"device_id": device_id},
        {"$set": {"device_name": new_name}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    
    logger.info(f"[RENAME] Device {device_id} renamed to '{new_name}'")
    return {"success": True, "device_id": device_id, "device_name": new_name}


@api_router.get("/")
async def root():
    return {"message": "WebRTC Remote Desktop - Device Signaling Server"}


@api_router.get("/health")
async def health():
    """Health check endpoint with device statistics from MongoDB"""
    if db is None:
        return {
            "status": "degraded",
            "message": "MongoDB unavailable",
            "devices_total": 0,
            "devices_online": len(host_ws),
        }
    
    try:
        total = await db.devices.count_documents({})
        online = len(host_ws)
        return {
            "status": "healthy",
            "devices_total": total,
            "devices_online": online,
            "viewers_connected": sum(len(v) for v in viewer_ws.values()),
        }
    except Exception as e:
        logger.error(f"[HEALTH] Failed to query MongoDB: {e}")
        return {
            "status": "degraded",
            "message": "Database query failed",
            "devices_total": 0,
            "devices_online": len(host_ws),
        }


@api_router.post("/register-device")
async def register_device(body: RegisterBody):
    """
    Register or re-register a device in MongoDB.
    Returns auth token for new devices or validates token for existing devices.
    """
    did = body.device_id
    
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable - cannot register device"
        )
    
    try:
        # Check if device already exists in MongoDB
        existing = await db.devices.find_one({"device_id": did})
        
        if existing:
            # Re-registration: validate token when provided
            if body.auth_token and body.auth_token != existing["auth_token"]:
                logger.warning(f"Device {did} re-registration rejected — invalid token")
                return {"success": False, "error": "Invalid auth token"}
            
            # Update mutable fields; preserve auth_token and status
            await db.devices.update_one(
                {"device_id": did},
                {
                    "$set": {
                        "device_name": body.device_name,
                        "last_seen": datetime.now(timezone.utc).isoformat()
                    }
                }
            )
            logger.info(f"Device {did} re-registered (name={body.device_name}) token=...{existing['auth_token'][-6:]}")
            return {"success": True, "device_id": did, "auth_token": existing["auth_token"]}
        
        # New device - insert into MongoDB
        token = gen_token()
        device_doc = {
            "device_id": did,
            "device_name": body.device_name,
            "auth_token": token,
            "status": "offline",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        await db.devices.insert_one(device_doc)
        logger.info(f"Device {did} registered (name={body.device_name}) token=...{token[-6:]}")
        return {"success": True, "device_id": did, "auth_token": token}
        
    except Exception as e:
        logger.error(f"[REGISTER] Failed to register device {did}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to register device"
        )


def calculate_last_online_ago(last_seen_iso: str, is_online: bool) -> str:
    """
    Calculate human-readable last online string from ISO 8601 timestamp.
    Returns "Online now" for online devices, or "X seconds/minutes/hours/days ago" for offline.
    
    **Validates: Requirements 10.1, 10.2, 10.3**
    """
    if is_online:
        return "Online now"
    
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
        now = datetime.now(timezone.utc)
        delta = now - last_seen
        
        total_seconds = int(delta.total_seconds())
        
        if total_seconds < 60:
            return f"{total_seconds} seconds ago"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = total_seconds // 86400
            return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception as e:
        logger.error(f"Failed to calculate last_online_ago: {e}")
        return "Unknown"


@api_router.get("/devices")
async def list_devices():
    """
    List all registered devices from MongoDB with enhanced metadata.
    Includes last_seen timestamp and human-readable last_online_ago string.
    Status is derived from live WebSocket connections (host_ws).
    
    **Validates: Requirements 9.5, 10.1, 10.2, 10.3, 10.4, 10.5**
    """
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
    
    try:
        devices_list = []
        cursor = db.devices.find({})
        
        async for d in cursor:
            # Derive status from live WS map — single source of truth
            status = device_status(d["device_id"])
            
            devices_list.append({
                "device_id": d["device_id"],
                "device_name": d["device_name"],
                "status": status,
                "last_seen": d["last_seen"],
                "last_online_ago": calculate_last_online_ago(d["last_seen"], status == "online"),
            })
        
        return {"devices": devices_list}
        
    except Exception as e:
        logger.error(f"[DEVICES] Failed to list devices: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve devices"
        )


@api_router.get("/devices/{device_id}")
async def get_device(device_id: str):
    """
    Get a single device by ID from MongoDB.
    Status is derived from live WebSocket connection.
    """
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
    
    try:
        d = await db.devices.find_one({"device_id": device_id})
        if not d:
            return {"exists": False}
        
        return {
            "exists": True,
            "device_id": d["device_id"],
            "device_name": d["device_name"],
            "status": device_status(device_id),  # live, not cached
            "last_seen": d["last_seen"],
        }
    except Exception as e:
        logger.error(f"[GET_DEVICE] Failed to get device {device_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve device"
        )


@api_router.post("/device-note")
async def create_or_update_device_note(req: DeviceNoteRequest):
    """
    Create or update a device note.
    Upserts note to device_notes collection using device_id as unique key.
    """
    async def operation():
        now = datetime.now(timezone.utc)
        
        result = await db.device_notes.update_one(
            {"device_id": req.device_id},
            {
                "$set": {
                    "device_id": req.device_id,
                    "note": req.note,
                    "updated_at": now
                }
            },
            upsert=True
        )
        
        return {
            "success": True,
            "device_id": req.device_id,
            "updated_at": now.isoformat()
        }
    
    return await safe_mongo_operation(operation)


@api_router.get("/device-note/{device_id}")
async def get_device_note(device_id: str):
    """
    Retrieve a device note by device_id.
    Returns 404 if note doesn't exist.
    Returns 503 if MongoDB is unavailable.
    """
    async def operation():
        doc = await db.device_notes.find_one({"device_id": device_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Note not found")
        
        return DeviceNoteResponse(
            device_id=doc["device_id"],
            note=doc["note"],
            updated_at=doc["updated_at"].isoformat()
        )
    
    return await safe_mongo_operation(operation)


@api_router.post("/device-screenshot")
async def upload_device_screenshot(req: DeviceScreenshotRequest):
    """
    Upload or update a device screenshot.
    Upserts screenshot to device_screenshots collection using device_id as unique key.
    
    Validates:
    - Base64 encoding (handled by Pydantic validator)
    - Size limit: 200KB (266KB base64, handled by Pydantic validator)
    
    Returns 400 for validation errors, 503 if MongoDB unavailable.
    """
    async def operation():
        now = datetime.now(timezone.utc)
        
        result = await db.device_screenshots.update_one(
            {"device_id": req.device_id},
            {
                "$set": {
                    "device_id": req.device_id,
                    "image": req.image,
                    "updated_at": now
                }
            },
            upsert=True
        )
        
        return {
            "success": True,
            "device_id": req.device_id,
            "updated_at": now.isoformat(),
            "size_bytes": len(req.image)
        }
    
    return await safe_mongo_operation(operation)


@api_router.get("/device-screenshot/{device_id}")
async def get_device_screenshot(device_id: str):
    """
    Retrieve a device screenshot by device_id.
    Returns 404 if screenshot doesn't exist.
    Returns 503 if MongoDB is unavailable.
    
    **Validates: Requirements 6.4, 6.5, 14.1, 14.5**
    """
    async def operation():
        doc = await db.device_screenshots.find_one({"device_id": device_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        
        return DeviceScreenshotResponse(
            device_id=doc["device_id"],
            image=doc["image"],
            updated_at=doc["updated_at"].isoformat()
        )
    
    return await safe_mongo_operation(operation)


@api_router.post("/device-screenshot/refresh/{device_id}")
async def refresh_device_screenshot(device_id: str):
    """
    Trigger a manual screenshot refresh for a device.
    Sends a WebSocket message to the host to capture a new screenshot immediately.
    Returns 404 if device not found.
    Returns 503 if device is offline.
    """
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )
    
    try:
        # Check if device exists in MongoDB
        device = await db.devices.find_one({"device_id": device_id})
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        
        # Check if device is online (has active WebSocket)
        hws = host_ws.get(device_id)
        if not hws:
            raise HTTPException(status_code=503, detail="Device is offline")
        
        # Send refresh command to host via WebSocket
        try:
            await hws.send_json({"type": "refresh_screenshot"})
            logger.info(f"[SCREENSHOT] Sent refresh command to device {device_id}")
            return {
                "success": True,
                "message": "Screenshot refresh triggered",
                "device_id": device_id
            }
        except Exception as e:
            logger.error(f"[SCREENSHOT] Failed to send refresh command to {device_id}: {e}")
            raise HTTPException(
                status_code=500,
                detail="Failed to send refresh command to device"
            )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SCREENSHOT] Error refreshing screenshot for {device_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to refresh screenshot"
        )


# ── Host WebSocket ────────────────────────────────────────────
@api_router.websocket("/ws/host/{device_id}")
async def ws_host(websocket: WebSocket, device_id: str):
    await websocket.accept()
    
    if db is None:
        await websocket.send_json({"type": "error", "message": "Database unavailable"})
        await websocket.close()
        return
    
    try:
        # Must be registered in MongoDB
        device = await db.devices.find_one({"device_id": device_id})
        if not device:
            await websocket.send_json({"type": "error", "message": "Device not registered"})
            await websocket.close()
            return
        
        # Validate auth token
        token = websocket.query_params.get("token", "")
        expected = device["auth_token"]
        if token != expected:
            logger.warning(
                f"Device {device_id} WS rejected — "
                f"got token ...{token[-6:] if token else 'EMPTY'}, "
                f"expected ...{expected[-6:]}"
            )
            await websocket.send_json({"type": "error", "message": "Invalid auth token"})
            await websocket.close()
            return
        
        # Evict previous connection for this device (e.g. Electron restart)
        old = host_ws.get(device_id)
        if old:
            logger.info(f"[SESSION] Device {device_id} — replacing existing host connection")
            try:
                await old.send_json({"type": "replaced"})
                await old.close()
            except Exception:
                pass
        
        # Register connection and mark ONLINE immediately in MongoDB
        host_ws[device_id] = websocket
        await update_device_status(device_id, "online", update_last_seen=True)
        
        logger.info(f"[SESSION] Device {device_id} ONLINE via WS (active hosts: {len(host_ws)}, active viewers: {len(viewer_ws)})")
        await websocket.send_json({"type": "connected", "device_id": device_id})
        
        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                t = data.get("type")
                
                if t == "ping":
                    # Refresh last_seen in MongoDB so heartbeat loop doesn't evict this device
                    await update_device_status(device_id, "online", update_last_seen=True)
                    await websocket.send_json({"type": "pong"})
                
                elif t in ("offer", "answer", "ice-candidate"):
                    viewers = viewer_ws.get(device_id, [])
                    for vws in list(viewers):
                        try:
                            await vws.send_text(raw)
                        except Exception:
                            pass
        
        except WebSocketDisconnect:
            logger.info(f"[SESSION] Device {device_id} WS disconnected (WebSocketDisconnect)")
        except Exception as e:
            logger.error(f"[SESSION] Device {device_id} WS error: {e}")
        finally:
            # Only remove our reference — don't clobber a newer connection
            if host_ws.get(device_id) is websocket:
                del host_ws[device_id]
                await update_device_status(device_id, "offline", update_last_seen=True)
                logger.info(f"[SESSION] Device {device_id} OFFLINE via WS disconnect (active hosts: {len(host_ws)}, active viewers: {len(viewer_ws)})")
            
            # Notify all active viewers
            for vws in list(viewer_ws.get(device_id, [])):
                try:
                    await vws.send_json({"type": "host_disconnected"})
                except Exception:
                    pass
    
    except Exception as e:
        logger.error(f"[WS_HOST] Unexpected error for device {device_id}: {e}")
        try:
            await websocket.close()
        except:
            pass


# ── Viewer WebSocket ──────────────────────────────────────────
@api_router.websocket("/ws/viewer/{device_id}")
async def ws_viewer(websocket: WebSocket, device_id: str):
    await websocket.accept()
    
    if db is None:
        await websocket.send_json({"type": "error", "message": "Database unavailable"})
        await websocket.close()
        return
    
    try:
        # Check if device exists in MongoDB
        device = await db.devices.find_one({"device_id": device_id})
        if not device:
            await websocket.send_json({"type": "error", "message": "Device not found"})
            await websocket.close()
            return
        
        hws = host_ws.get(device_id)
        if not hws:
            await websocket.send_json({"type": "error", "message": "Device is offline"})
            await websocket.close()
            return
        
        # Cancel any pending delayed disconnect (viewer reconnecting)
        if device_id in viewer_disconnect_tasks:
            logger.info(f"[SESSION] Viewer reconnecting to {device_id}, cancelling delayed disconnect")
            viewer_disconnect_tasks[device_id].cancel()
            del viewer_disconnect_tasks[device_id]
        
        # Add this viewer to the list (allow multiple viewers)
        if device_id not in viewer_ws:
            viewer_ws[device_id] = []
        if device_id not in viewer_last_seen:
            viewer_last_seen[device_id] = {}
        
        viewer_ws[device_id].append(websocket)
        viewer_last_seen[device_id][id(websocket)] = datetime.now(timezone.utc)
        viewer_count = len(viewer_ws[device_id])
        
        await websocket.send_json({
            "type": "connected",
            "device_id": device_id,
            "device_name": device["device_name"],
        })
        
        # Tell host a viewer is ready
        hws = host_ws.get(device_id)
        if not hws:
            logger.warning(f"[SESSION] Host {device_id} disappeared before viewer_connected could be sent")
            await websocket.send_json({"type": "error", "message": "Host disconnected"})
            await websocket.close()
            if device_id in viewer_ws and websocket in viewer_ws[device_id]:
                viewer_ws[device_id].remove(websocket)
            return
        
        try:
            await hws.send_json({"type": "viewer_connected"})
            logger.info(f"[SESSION] Viewer #{viewer_count} connected to device {device_id}")
        except Exception as e:
            logger.error(f"[SESSION] Failed to reach host {device_id}: {e}")
            await websocket.send_json({"type": "error", "message": "Failed to reach host"})
            await websocket.close()
            if device_id in viewer_ws and websocket in viewer_ws[device_id]:
                viewer_ws[device_id].remove(websocket)
            return
        
        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                t = data.get("type")
                
                if t == "ping":
                    viewer_last_seen[device_id][id(websocket)] = datetime.now(timezone.utc)
                    await websocket.send_json({"type": "pong"})
                
                elif t in ("offer", "answer", "ice-candidate"):
                    hws = host_ws.get(device_id)
                    if hws:
                        try:
                            await hws.send_text(raw)
                        except Exception:
                            pass
        
        except WebSocketDisconnect:
            logger.info(f"[SESSION] Viewer disconnected from device {device_id}")
        except Exception as e:
            logger.error(f"[SESSION] Viewer WS error ({device_id}): {e}")
        finally:
            # Remove this specific viewer
            if device_id in viewer_ws and websocket in viewer_ws[device_id]:
                viewer_ws[device_id].remove(websocket)
                if device_id in viewer_last_seen:
                    viewer_last_seen[device_id].pop(id(websocket), None)
                remaining = len(viewer_ws[device_id])
                logger.info(f"[SESSION] Viewer removed from device {device_id}, {remaining} viewer(s) remaining")
                
                # Clean up empty list
                if not viewer_ws[device_id]:
                    del viewer_ws[device_id]
                    # Schedule delayed disconnect notification to host
                    if device_id not in viewer_disconnect_tasks:
                        viewer_disconnect_tasks[device_id] = asyncio.create_task(delayed_disconnect(device_id))
                        logger.info(f"[SESSION] Scheduled delayed disconnect for device {device_id}")
    
    except Exception as e:
        logger.error(f"[WS_VIEWER] Unexpected error for device {device_id}: {e}")
        try:
            await websocket.close()
        except:
            pass


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
