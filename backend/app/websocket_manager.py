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
        self._lock = asyncio.Lock()  # Add thread-safe lock
        logger.info("ConnectionManager initialized")
    
    async def connect(self, websocket: WebSocket, user_id: str, match_id: str):
        try:
            await websocket.accept()
            
            # Get client information from headers
            client_host = websocket.client.host
            user_agent = websocket.headers.get("user-agent", "Unknown")
            
            async with self._lock:
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
                
                # Log detailed connection information
                logger.info(f"""
New WebSocket Connection:
    Match ID: {match_id}
    User ID: {user_id}
    Client IP: {client_host}
    User Agent: {user_agent}
    WebSocket ID: {id(websocket)}
Active Connections Summary:
    Total connections for match: {len(self.active_connections[match_id])}
    Total connections for user: {len(self.user_connections[user_id])}
Current Active Sessions for Match {match_id}:
    {[{
        'websocket_id': id(ws),
        'user_id': self.websocket_users.get(ws),
        'client_ip': ws.client.host,
        'user_agent': ws.headers.get("user-agent", "Unknown")[:50] + "..."  # Truncate long user agents
    } for ws in self.active_connections[match_id]]}
                """)
        except Exception as e:
            logger.error(f"Error in WebSocket connection for user {user_id}, match {match_id}: {e}")
            raise
    
    def disconnect(self, websocket: WebSocket):
        match_id = self.websocket_matches.get(websocket)
        user_id = self.websocket_users.get(websocket)
        
        # Log disconnection with details
        if user_id and match_id:
            logger.info(f"""
WebSocket Disconnection:
    Match ID: {match_id}
    User ID: {user_id}
    WebSocket ID: {id(websocket)}
    Client IP: {websocket.client.host}
    Remaining connections for match: {len(self.active_connections.get(match_id, set())) - 1}
    Remaining connections for user: {len(self.user_connections.get(user_id, set())) - 1}
            """)
        
        # Remove from match connections
        if match_id and match_id in self.active_connections:
            self.active_connections[match_id].discard(websocket)
            if not self.active_connections[match_id]:
                del self.active_connections[match_id]
        
        # Remove from user connections
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
            logger.warning(f"No active connections found for match {match_id}")
            return
        
        async with self._lock:  # Use lock for thread-safe operations
            logger.info(f"Broadcasting to match {match_id}: {len(self.active_connections[match_id])} connected clients")
            logger.debug(f"Broadcast message: {message}")
            logger.debug(f"Active connections for match: {[id(ws) for ws in self.active_connections[match_id]]}")
            
            disconnected_websockets = set()
            broadcast_count = 0
            
            # Create a copy of the connections set to avoid modification during iteration
            current_connections = self.active_connections[match_id].copy()
            
            for websocket in current_connections:
                try:
                    # Convert the message to string only once
                    message_str = json.dumps(message)
                    await websocket.send_text(message_str)
                    broadcast_count += 1
                    logger.debug(f"Successfully sent message to websocket {id(websocket)}")
                except Exception as e:
                    logger.error(f"Error broadcasting to websocket {id(websocket)}: {e}")
                    disconnected_websockets.add(websocket)
            
            # Clean up disconnected websockets
            for websocket in disconnected_websockets:
                await self.disconnect_async(websocket)
            
            logger.info(f"Broadcast completed for match {match_id}: {broadcast_count} successful, {len(disconnected_websockets)} failed")

    async def disconnect_async(self, websocket: WebSocket):
        """Async version of disconnect for use within async contexts"""
        async with self._lock:
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

async def check_and_broadcast_expiry_warnings():
    """Background task to check for expiring matches and broadcast warnings"""
    while True:
        try:
            db = next(get_db())
            
            # Get all active matches
            active_matches = list(get_active_matches(db))
            
            # Process matches if any exist
            if active_matches:
                for match in active_matches:
                    try:
                        match_id = str(match["_id"])
                        expires_at = match["expires_at"]
                        now = datetime.now(timezone.utc)
                        
                        # Ensure expires_at is timezone-aware
                        if not expires_at.tzinfo:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                        
                        time_left = expires_at - now
                        time_left_seconds = int(time_left.total_seconds())
                        
                        if time_left_seconds <= 0:
                            # Get conversation status
                            from .services.conversation_service import get_conversation_by_match_id, update_conversation_status
                            conversation = get_conversation_by_match_id(match_id, db)
                            
                            if conversation and conversation["status"] == "accepted":
                                # Update status first
                                update_conversation_status(match_id, "expired", db)
                                
                                # Then broadcast updates
                                await broadcast_match_expired(match_id)
                                await broadcast_conversation_status_update(match_id, {
                                    "status": "expired",
                                    "reason": "match_expired"
                                })
                            else:
                                await broadcast_match_expired(match_id)
                        
                        elif time_left_seconds <= 1800:  # 30 minutes
                            await broadcast_match_expiry_warning(match_id, time_left_seconds)
                    
                    except Exception as e:
                        logger.error(f"Error processing match {match.get('_id')}: {e}")
                        continue
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"Error in expiry check task: {e}")
            await asyncio.sleep(30)

async def broadcast_new_message(match_id: str, message_data: dict, exclude_user_id: Optional[str] = None):
    """Broadcast a new message to all users in a match"""
    try:
        logger.info(f"Broadcasting new message for match {match_id}")
        logger.debug(f"Message data: {message_data}")
        logger.debug(f"Active connections before broadcast: {len(manager.active_connections.get(match_id, set()))}")
        
        message = {
            "type": "new_message",
            "match_id": match_id,
            "message": message_data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        await manager.broadcast_to_match(message, match_id)
        logger.info(f"Message broadcast completed for match {match_id}")
    except Exception as e:
        logger.error(f"Error broadcasting message for match {match_id}: {e}")
        raise

async def broadcast_conversation_status_update(match_id: str, status_data: dict):
    """Broadcast conversation status updates (accepted, rejected, expired)"""
    logger.info(f"Broadcasting status update for match {match_id}: {status_data['status']}")
    logger.debug(f"Status data: {status_data}")
    
    message = {
        "type": "conversation_status_update",
        "match_id": match_id,
        "status": status_data,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)
    logger.info(f"Status update broadcast completed for match {match_id}")

async def broadcast_match_expiry_warning(match_id: str, time_left_seconds: int):
    """Broadcast match expiry warning to all users in the match"""
    logger.info(f"Broadcasting expiry warning for match {match_id}: {time_left_seconds}s remaining")
    
    message = {
        "type": "match_expiry_warning",
        "match_id": match_id,
        "time_left_seconds": time_left_seconds,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)
    logger.info(f"Expiry warning broadcast completed for match {match_id}")

async def broadcast_match_expired(match_id: str):
    """Broadcast match expiry to all users in the match"""
    logger.info(f"Broadcasting match expired notification for match {match_id}")
    
    message = {
        "type": "match_expired",
        "match_id": match_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)
    logger.info(f"Match expired broadcast completed for match {match_id}")
    logger.info(f"Match expired broadcast completed for match {match_id}")
    logger.info(f"Expiry warning broadcast completed for match {match_id}")

async def broadcast_match_expired(match_id: str):
    """Broadcast match expiry to all users in the match"""
    logger.info(f"Broadcasting match expired notification for match {match_id}")
    
    message = {
        "type": "match_expired",
        "match_id": match_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    await manager.broadcast_to_match(message, match_id)
    logger.info(f"Match expired broadcast completed for match {match_id}")
    logger.info(f"Match expired broadcast completed for match {match_id}")
