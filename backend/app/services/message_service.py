# app/services/message_service.py

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pymongo.database import Database
from bson import ObjectId
from fastapi import HTTPException, status

from ..models import UserDetails
from .redis_service import redis_service

logger = logging.getLogger(__name__)


class MessageService:
    """Service for handling message operations"""
    
    def validate_conversation_participant(
        self,
        db: Database,
        conversation_id: str,
        user_regno: str
    ) -> Dict[str, Any]:
        """
        Validate that user is a participant in the conversation
        
        Returns conversation data if valid, raises HTTPException otherwise
        """
        try:
            conversation = db["conversations"].find_one({"_id": ObjectId(conversation_id)})
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid conversation ID"
            )
        
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found"
            )
        
        # Check if user is participant
        is_participant = (
            conversation.get("initiatorId") == user_regno or
            conversation.get("receiverId") == user_regno
        )
        
        if not is_participant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a participant in this conversation"
            )
        
        # Check conversation status
        if conversation.get("status") != "accepted":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation must be accepted before sending messages"
            )
        
        # NOTE: Block check removed from here - reading messages should be allowed even when blocked
        # The block check is only enforced in send_message() to prevent sending new messages
        
        # Check if expired (4 hours from creation)
        created_at = conversation.get("createdAt")
        if created_at:
            expiry_time = created_at + timedelta(hours=4)
            if datetime.utcnow() > expiry_time:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Conversation has expired"
                )
        
        return conversation
    
    def send_message(
        self,
        db: Database,
        conversation_id: str,
        sender: UserDetails,
        text: str
    ) -> Dict[str, Any]:
        """
        Send a message in a conversation
        
        Enforces:
        - Sender is participant
        - Conversation is accepted
        - Conversation is not blocked
        - Conversation is not expired (4 hours)
        """
        # Validate sender is participant (allows reading but not sending if blocked)
        conversation = self.validate_conversation_participant(
            db, conversation_id, sender.Regno
        )
        
        # IMPORTANT: Check if conversation is blocked (prevents sending, but reading is OK)
        if conversation.get("isBlocked", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot send messages: this conversation is blocked"
            )
        
        # Determine receiver
        receiver_id = (
            conversation["receiverId"] 
            if conversation["initiatorId"] == sender.Regno 
            else conversation["initiatorId"]
        )
        
        # Create message document
        message_doc = {
            "conversation_id": ObjectId(conversation_id),
            "sender_id": sender.Regno,
            "receiver_id": receiver_id,
            "text": text,
            "timestamp": datetime.utcnow(),
            "read": False
        }
        
        # Insert into MongoDB
        result = db["messages"].insert_one(message_doc)
        message_doc["_id"] = result.inserted_id
        
        # Update conversation's last_message_at
        db["conversations"].update_one(
            {"_id": ObjectId(conversation_id)},
            {
                "$set": {
                    "last_message_at": message_doc["timestamp"],
                    "last_message_text": text[:100]  # Preview
                }
            }
        )
        
        # Publish to Redis for real-time delivery
        redis_message = {
            "id": str(message_doc["_id"]),
            "conversation_id": conversation_id,
            "sender_id": sender.Regno,
            "sender_name": sender.Name,
            "receiver_id": receiver_id,
            "text": text,
            "timestamp": message_doc["timestamp"].isoformat(),
            "read": False
        }
        
        redis_service.publish_message(conversation_id, redis_message)
        
        logger.info(f"Message sent: {sender.Regno} -> {receiver_id} in conversation {conversation_id}")
        
        return {
            "id": str(message_doc["_id"]),
            "conversation_id": conversation_id,
            "sender_id": sender.Regno,
            "receiver_id": receiver_id,
            "text": text,
            "timestamp": message_doc["timestamp"].isoformat(),
            "read": False
        }
    
    def get_messages(
        self,
        db: Database,
        conversation_id: str,
        user: UserDetails,
        limit: int = 200
    ) -> List[Dict[str, Any]]:
        """
        Get messages for a conversation
        
        Enforces:
        - User is participant in conversation
        - Conversation is accepted
        """
        # Validate user is participant
        self.validate_conversation_participant(db, conversation_id, user.Regno)
        
        # Fetch messages
        messages = list(
            db["messages"]
            .find({"conversation_id": ObjectId(conversation_id)})
            .sort("timestamp", 1)
            .limit(limit)
        )
        
        # Mark messages as read for receiver
        db["messages"].update_many(
            {
                "conversation_id": ObjectId(conversation_id),
                "receiver_id": user.Regno,
                "read": False
            },
            {"$set": {"read": True}}
        )
        
        # Format response
        return [
            {
                "id": str(msg["_id"]),
                "conversation_id": conversation_id,
                "sender_id": msg["sender_id"],
                "receiver_id": msg["receiver_id"],
                "text": msg["text"],
                "timestamp": msg["timestamp"].isoformat(),
                "read": msg.get("read", False),
                "is_sender": msg["sender_id"] == user.Regno
            }
            for msg in messages
        ]
    
    def report_message(
        self,
        db: Database,
        message_id: str,
        reporter: UserDetails,
        reason: str
    ) -> Dict[str, Any]:
        """
        Report a message
        
        Enforces:
        - Reporter is the receiver of the message
        - Message exists in an accepted conversation
        - No duplicate reports
        """
        # Get message
        try:
            message = db["messages"].find_one({"_id": ObjectId(message_id)})
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid message ID"
            )
        
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found"
            )
        
        # Validate reporter is receiver
        if message["receiver_id"] != reporter.Regno:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only report messages sent to you"
            )
        
        # Check conversation is accepted
        conversation = db["conversations"].find_one(
            {"_id": message["conversation_id"]}
        )
        
        if not conversation or conversation.get("status") != "accepted":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Can only report messages in accepted conversations"
            )
        
        # Check for duplicate report
        existing_report = db["message_reports"].find_one({
            "message_id": ObjectId(message_id),
            "reporter_id": reporter.Regno
        })
        
        if existing_report:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already reported this message"
            )
        
        # Create report
        report_doc = {
            "message_id": ObjectId(message_id),
            "conversation_id": message["conversation_id"],
            "reporter_id": reporter.Regno,
            "reported_user_id": message["sender_id"],
            "reason": reason,
            "reported_at": datetime.utcnow(),
            "status": "pending"
        }
        
        result = db["message_reports"].insert_one(report_doc)
        
        logger.info(f"Message {message_id} reported by {reporter.Regno} for: {reason}")
        
        return {
            "id": str(result.inserted_id),
            "message": "Message reported successfully"
        }
    
    def get_conversation_messages_count(
        self,
        db: Database,
        conversation_id: str
    ) -> int:
        """Get message count for a conversation"""
        return db["messages"].count_documents(
            {"conversation_id": ObjectId(conversation_id)}
        )


# Global instance
message_service = MessageService()
