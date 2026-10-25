// src/components/ReportLoveNoteDialog.tsx
import { logger } from '@/lib/logger';
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
import { reportLoveNote } from '@/services/api';
import { REPORT_REASONS } from '@/lib/reportReasons';
import { apiErrorStatus, describeApiError } from '@/lib/apiError';
import { toast } from 'sonner';

interface ReportLoveNoteDialogProps {
  isOpen: boolean;
  onClose: () => void;
  noteId: string;
  noteText: string;
  /** Called with the note id once the server has accepted the report. */
  onReported: (noteId: string) => void;
}

/**
 * Reports a love note the signed-in account received.
 *
 * The same shape as ReportMessageDialog, deliberately: a reporter who has just used one of
 * these should not have to learn a second one. What differs is the outcome. Filing a report
 * leaves the note in the recipient's inbox and changes nothing about it — staff decide what
 * happens next — so this dialog says so rather than implying the note has been taken down.
 */
export const ReportLoveNoteDialog: React.FC<ReportLoveNoteDialogProps> = ({
  isOpen,
  onClose,
  noteId,
  noteText,
  onReported,
}) => {
  const [selectedReason, setSelectedReason] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleReport = async () => {
    if (!selectedReason) {
      toast.error('Please select a reason for reporting');
      return;
    }

    setIsSubmitting(true);
    try {
      await reportLoveNote(noteId, selectedReason);
      toast.success('Reported. Our team will review this love note.');
      onReported(noteId);
      onClose();
      setSelectedReason('');
    } catch (error) {
      logger.error('Error reporting love note:', error);

      // The server owns the duplicate rule, so it is the one that decides whether this
      // account has already reported the note; the client just reflects the answer.
      if (apiErrorStatus(error) === 409) {
        toast.error('You have already reported this love note.');
        onReported(noteId);
        onClose();
      } else {
        // `detail` is a plain string for an HTTPException and an array of Pydantic error
        // objects for a 422, and a 422 is what an over-long reason produces. Rendering that
        // array as a toast child crashes the tree, so it goes through the shared describer.
        toast.error(describeApiError(error, 'Failed to report this love note. Please try again.'));
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
            Report this love note
          </DialogTitle>
          <DialogDescription>
            Help us keep ConfessIt safe. Select a reason for reporting this love note. It stays
            in your inbox until our team has looked at it.
          </DialogDescription>
        </DialogHeader>

        <div className="my-4 p-3 bg-muted rounded-md">
          <p className="text-sm text-muted-foreground line-clamp-2">&quot;{noteText}&quot;</p>
        </div>

        <RadioGroup value={selectedReason} onValueChange={setSelectedReason}>
          <div className="space-y-3 max-h-[300px] overflow-y-auto pr-2">
            {REPORT_REASONS.map((reason) => (
              <div key={reason.value} className="flex items-center space-x-2">
                <RadioGroupItem value={reason.value} id={`love-note-${reason.value}`} />
                <Label
                  htmlFor={`love-note-${reason.value}`}
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
            {isSubmitting ? 'Reporting...' : 'Report Love Note'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default ReportLoveNoteDialog;
