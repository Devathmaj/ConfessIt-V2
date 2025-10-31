// src/components/ReportMessageDialog.tsx
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
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Label } from '@/components/ui/label';
import { AlertTriangle } from 'lucide-react';
import { reportMessage } from '@/lib/messaging';
import { useAuth } from '@/contexts/AuthContext';
import { toast } from 'sonner';

interface ReportMessageDialogProps {
  isOpen: boolean;
  onClose: () => void;
  messageId: string;
  messageText: string;
  reporterId: string;
  reportedUserId: string;
  conversationId: string;
}

const REPORT_REASONS = [
  { value: 'harassment', label: 'Harassment or bullying' },
  { value: 'hate_speech', label: 'Hate speech or discrimination' },
  { value: 'spam', label: 'Spam or scam' },
  { value: 'inappropriate_content', label: 'Inappropriate or offensive content' },
  { value: 'threats', label: 'Threats or violence' },
  { value: 'sexual_content', label: 'Unwanted sexual content' },
  { value: 'impersonation', label: 'Impersonation or fake identity' },
  { value: 'other', label: 'Other violation' },
];

export const ReportMessageDialog: React.FC<ReportMessageDialogProps> = ({
  isOpen,
  onClose,
  messageId,
  messageText,
  reporterId,
  reportedUserId,
  conversationId,
}) => {
  const { token } = useAuth(); // Get token from AuthContext
  const [selectedReason, setSelectedReason] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleReport = async () => {
    if (!selectedReason) {
      toast.error('Please select a reason for reporting');
      return;
    }

    if (!token) {
      toast.error('Authentication token missing');
      return;
    }

    setIsSubmitting(true);
    try {
      await reportMessage(messageId, selectedReason, token);

      toast.success('Message reported successfully. Our team will review it.');
      onClose();
      setSelectedReason('');
    } catch (error: any) {
      console.error('Error reporting message:', error);
      
      // Check if it's a duplicate report error
      if (error?.message?.includes('duplicate') || 
          error?.message?.includes('already reported')) {
        toast.error('You have already reported this message.');
      } else {
        toast.error('Failed to report message. Please try again.');
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClose = () => {
    setSelectedReason('');
    onClose();
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-red-500" />
            Report this message
          </DialogTitle>
          <DialogDescription>
            Help us keep ConfessIt safe. Select a reason for reporting this message.
          </DialogDescription>
        </DialogHeader>

        <div className="my-4 p-3 bg-muted rounded-md">
          <p className="text-sm text-muted-foreground line-clamp-2">"{messageText}"</p>
        </div>

        <RadioGroup value={selectedReason} onValueChange={setSelectedReason}>
          <div className="space-y-3 max-h-[300px] overflow-y-auto pr-2">
            {REPORT_REASONS.map((reason) => (
              <div key={reason.value} className="flex items-center space-x-2">
                <RadioGroupItem value={reason.value} id={reason.value} />
                <Label
                  htmlFor={reason.value}
                  className="text-sm font-normal cursor-pointer flex-1"
                >
                  {reason.label}
                </Label>
              </div>
            ))}
          </div>
        </RadioGroup>

        <DialogFooter className="flex gap-2 sm:gap-0">
          <Button
            variant="outline"
            onClick={handleClose}
            disabled={isSubmitting}
            className="flex-1 sm:flex-initial"
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={handleReport}
            disabled={isSubmitting || !selectedReason}
            className="flex-1 sm:flex-initial"
          >
            {isSubmitting ? 'Reporting...' : 'Report Message'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default ReportMessageDialog;
