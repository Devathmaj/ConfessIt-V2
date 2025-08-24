# app/websocket_manager.py

import json
import asyncio
from typing import Dict, Set, Optional
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
# Import these as functions to avoid circular imports
from .services.matchmaking_service import get_active_matches
from .dependencies import get_db
from .logger import get_logger

logger = get_logger(__name__)

class ConnectionManager:
    def __init__(self):
        # Map of match_id -> set of WebSocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        # Map of user_id -> set of WebSocket connections (for user-specific messages)
        self.user_connections: Dict[str, Set[WebSocket]] = {}
        # Map of WebSocket -> user_id for cleanup
        self.websocket_users: Dict[WebSocket, str] = {}
        # Map of WebSocket -> match_id for cleanup
        self.websocket_matches: Dict[WebSocket, str] = {}
        
    async def connect(self, websocket: WebSocket, user_id: str, match_id: str):
        await websocket.accept()
        
        # Add to match connections
        if match_id not in self.active_connections:
            self.active_connections[match_id] = set()
        self.active_connections[match_id].add(websocket)
        
        # Add to user connections
        if user_id not in self.user_connections:
            self.user_connections[user_id] = set()
        self.user_connections[user_id].add(websocket)
        
        # Track for cleanup
        self.websocket_users[websocket] = user_id
        self.websocket_matches[websocket] = match_id
        
        logger.info(f"User {user_id} connected to match {match_id}")
        
    def disconnect(self, websocket: WebSocket):
        # Remove from match connections
        match_id = self.websocket_matches.get(websocket)
        if match_id and match_id in self.active_connections:
            self.active_connections[match_id].discard(websocket)
            if not self.active_connections[match_id]:
                del self.active_connections[match_id]
        
        # Remove from user connections
        user_id = self.websocket_users.get(websocket)
        if user_id and user_id in self.user_connections:
            self.user_connections[user_id].discard(websocket)
            if not self.user_connections[user_id]:
                del self.user_connections[user_id]
        
        # Clean up tracking
        self.websocket_users.pop(websocket, None)
        self.websocket_matches.pop(websocket, None)
        
        if user_id and match_id:
            logger.info(f"User {user_id} disconnected from match {match_id}")
    
    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(message))
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")
            self.disconnect(websocket)
    
    async def broadcast_to_match(self, message: dict, match_id: str, exclude_websocket: Optional[WebSocket] = None):
        if match_id not in self.active_connections:
            return
        
        disconnected_websockets = set()
        
        for websocket in self.active_connections[match_id]:
            if websocket != exclude_websocket:
                try:
                    await websocket.send_text(json.dumps(message))
                except Exception as e:
                    logger.error(f"Error broadcasting to match {match_id}: {e}")
                    disconnected_websockets.add(websocket)
        
        # Clean up disconnected websockets
        for websocket in disconnected_websockets:
            self.disconnect(websocket)
    
    async def broadcast_to_user(self, message: dict, user_id: str, exclude_websocket: Optional[WebSocket] = None):
        if user_id not in self.user_connections:
            return
        
        disconnected_websockets = set()
        
        for websocket in self.user_connections[user_id]:
            if websocket != exclude_websocket:
                try:
                    await websocket.send_text(json.dumps(message))
                except Exception as e:
                    logger.error(f"Error broadcasting to user {user_id}: {e}")
                    disconnected_websockets.add(websocket)
        
        # Clean up disconnected websockets
        for websocket in disconnected_websockets:
            self.disconnect(websocket)

# Global connection manager instance
manager = ConnectionManager()

async def handle_websocket_connection(websocket: WebSocket, user_id: str, match_id: str):
    """Handle WebSocket connection for a user in a specific match"""
    await manager.connect(websocket, user_id, match_id)
    
    try:
        # Send initial connection confirmation
        await manager.send_personal_message({
            "type": "connection_established",
            "user_id": user_id,
            "match_id": match_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }, websocket)
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages from client
                data = await websocket.receive_text()
                message_data = json.loads(data)
                
                # Handle different message types
                message_type = message_data.get("type")
                
                if message_type == "ping":
                    # Respond to ping with pong
                    await manager.send_personal_message({
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }, websocket)
                
                elif message_type == "typing":
                    # Broadcast typing indicator to other users in the match
                    await manager.broadcast_to_match({
                        "type": "typing",
                        "user_id": user_id,
                        "match_id": match_id,
                        "is_typing": message_data.get("is_typing", False),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }, match_id, exclude_websocket=websocket)
                
                elif message_type == "message_sent":
                    # This is just a notification that a message was sent via REST API
                    # The actual message will be broadcasted when the REST API calls broadcast_new_message
                    await manager.send_personal_message({
                        "type": "message_confirmed",
                        "message_id": message_data.get("message_id"),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }, websocket)
                
            except WebSocketDisconnect:
                logger.info(f"WebSocket disconnected for user {user_id} in match {match_id}")
                break
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON received from user {user_id}")
                continue
            except Exception as e:
                logger.error(f"Error handling WebSocket message from user {user_id}: {e}")
                break
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for user {user_id} in match {match_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket connection for user {user_id}: {e}")
    finally:
        manager.disconnect(websocket)

async def broadcast_new_message(match_id: str, message_data: dict, exclude_user_id: Optional[str] = None):
    """Broadcast a new message to all users in a match"""
    message = {
        "type": "new_message",
        "match_id": match_id,
        "message": message_data,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)

async def broadcast_conversation_status_update(match_id: str, status_data: dict):
    """Broadcast conversation status updates (accepted, rejected, expired)"""
    message = {
        "type": "conversation_status_update",
        "match_id": match_id,
        "status": status_data,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)

async def broadcast_match_expiry_warning(match_id: str, time_left_seconds: int):
    """Broadcast match expiry warning to all users in the match"""
    message = {
        "type": "match_expiry_warning",
        "match_id": match_id,
        "time_left_seconds": time_left_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)

async def broadcast_match_expired(match_id: str):
    """Broadcast match expiry to all users in the match"""
    message = {
        "type": "match_expired",
        "match_id": match_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)

async def check_and_broadcast_expiry_warnings():
    """Background task to check for expiring matches and broadcast warnings"""
    while True:
        try:
            db = next(get_db())
            
            # Get all active matches
            active_matches = get_active_matches(db)
            
            for match in active_matches:
                match_id = str(match["_id"])
                expires_at = match["expires_at"]
                now = datetime.now(timezone.utc)
                
                # Calculate time left
                time_left = expires_at - now
                time_left_seconds = int(time_left.total_seconds())
                
                # Check if match is expired
                if time_left_seconds <= 0:
                    # Broadcast expiry
                    await broadcast_match_expired(match_id)
                    
                    # Update conversation status if needed (import here to avoid circular import)
                    from .services.conversation_service import get_conversation_by_match_id, update_conversation_status
                    conversation = get_conversation_by_match_id(match_id, db)
                    if conversation and conversation["status"] == "accepted":
                        update_conversation_status(match_id, "expired", db)
                        await broadcast_conversation_status_update(match_id, {
                            "status": "expired",
                            "reason": "match_expired"
                        })
                
                # Send warning if less than 30 minutes left
                elif time_left_seconds <= 1800:  # 30 minutes
                    await broadcast_match_expiry_warning(match_id, time_left_seconds)
            
            # Check every 30 seconds
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"Error in expiry check task: {e}")
            await asyncio.sleep(30)  # Continue checking even if there's an error
