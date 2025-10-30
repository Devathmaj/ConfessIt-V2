// src/components/BlockUserDialog.tsx
import React, { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle, ShieldOff } from 'lucide-react';
import { blockConversation } from '@/services/api';
import { toast } from 'sonner';

interface BlockUserDialogProps {
  isOpen: boolean;
  onClose: () => void;
  matchId: string;
  otherUserName: string;
  onBlockSuccess: () => void;
}

export const BlockUserDialog: React.FC<BlockUserDialogProps> = ({
  isOpen,
  onClose,
  matchId,
  otherUserName,
  onBlockSuccess,
}) => {
  const [isBlocking, setIsBlocking] = useState(false);

  const handleBlock = async () => {
    setIsBlocking(true);
    try {
      await blockConversation(matchId);
      toast.success(`${otherUserName} has been blocked`);
      onBlockSuccess();
      onClose();
    } catch (error) {
      console.error('Error blocking user:', error);
      toast.error('Failed to block user. Please try again.');
    } finally {
      setIsBlocking(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldOff className="h-5 w-5 text-red-500" />
            Block {otherUserName}?
          </DialogTitle>
          <DialogDescription>
            This action will block this conversation.
          </DialogDescription>
        </DialogHeader>

        <div className="my-4 p-4 bg-yellow-50 border border-yellow-200 rounded-md">
          <div className="flex gap-2 mb-2">
            <AlertTriangle className="h-5 w-5 text-yellow-600 flex-shrink-0 mt-0.5" />
            <div className="space-y-2 text-sm text-yellow-900">
              <p className="font-semibold">What happens when you block:</p>
              <ul className="list-disc list-inside space-y-1 ml-2">
                <li><strong>{otherUserName}</strong> will be notified that you blocked them</li>
                <li>Neither of you can send new messages</li>
                <li>You can unblock or hide messages later</li>
              </ul>
            </div>
          </div>
        </div>

        <DialogFooter className="flex gap-2 sm:gap-0">
          <Button
            variant="outline"
            onClick={onClose}
            disabled={isBlocking}
            className="flex-1 sm:flex-initial"
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={handleBlock}
            disabled={isBlocking}
            className="flex-1 sm:flex-initial"
          >
            {isBlocking ? 'Blocking...' : 'Block User'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default BlockUserDialog;
