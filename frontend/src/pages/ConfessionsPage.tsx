import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { 
  Heart, 
  Laugh, 
  Eye, 
  HeartCrack, 
  ArrowLeft, 
  TrendingUp, 
  Clock, 
  MessageSquare, 
  Download,
  Send,
  Plus,
  ThumbsUp,
  ThumbsDown
} from 'lucide-react';
import { toast } from 'sonner';
import { getConfessions, createConfession, reactToConfession, createComment, likeComment, dislikeComment } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import { Navigation } from '@/components/Navigation';

// Interface for user information
interface UserInfo {
  id: string;
  username: string;
  avatar?: string;
}

// Interface for a single comment on a confession
interface ConfessionComment {
  id: string;
  user_info: UserInfo;
  message: string;
  timestamp: string;
  like_count: number;
  dislike_count: number;
  user_reaction: 'like' | 'dislike' | null;
}

// Interface for a single confession post
interface Confession {
  id: string;
  confession: string;
  is_anonymous: boolean;
  user_id: string;
  timestamp: string;
  comments: ConfessionComment[];
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  user_reaction: string | null;
}

export const ConfessionsPage = () => {
  const [confessions, setConfessions] = useState<Confession[]>([]);
  const [sortBy, setSortBy] = useState<'popularity' | 'time' | 'comments'>('popularity');
  const [newConfession, setNewConfession] = useState('');
  const [isAnonymous, setIsAnonymous] = useState(true);
  const [allowComments, setAllowComments] = useState(true);
  const [isPostDialogOpen, setIsPostDialogOpen] = useState(false);
  const { user, isAuthenticated } = useAuth();

  // State to manage the confession selected for viewing in the modal
  const [selectedConfession, setSelectedConfession] = useState<Confession | null>(null);
  const [newComment, setNewComment] = useState('');

  // Effect to fetch confessions when the component mounts or sorting preference changes
  useEffect(() => {
    const fetchConfessions = async () => {
      try {
        const data = await getConfessions(sortBy);
        setConfessions(data);
      } catch (error) {
        toast.error("Failed to load confessions. Please try again.");
        console.error(error);
      }
    };
    fetchConfessions();
  }, [sortBy, isAuthenticated]);

  /**
   * Opens the comment modal for a specific confession.
   * @param {Confession} confession - The confession object to display in the modal.
   */
  const handleCommentClick = (confession: Confession) => {
    setSelectedConfession(confession);
  };

  /**
   * Handles the submission of a new comment for a confession.
   * @param {string} confessionId - The ID of the confession being commented on.
   */
  const handlePostComment = async (confessionId: string) => {
    if (!newComment.trim()) {
      toast.info("Please write a comment first.");
      return;
    }
    if (!isAuthenticated || !user) {
      toast.error("You must be logged in to comment.");
      return;
    }
    
    try {
      const newCommentData = await createComment(confessionId, newComment);
      const updateConfessions = (prev: Confession[]) => 
        prev.map(conf => 
          conf.id === confessionId 
            ? { ...conf, comments: [newCommentData, ...conf.comments], comment_count: conf.comment_count + 1 } 
            : conf
        );
      
      setConfessions(updateConfessions);
      // Also update the state of the selected confession if it's open in the modal
      if (selectedConfession && selectedConfession.id === confessionId) {
        setSelectedConfession(prev => prev ? {...prev, comments: [newCommentData, ...prev.comments], comment_count: prev.comment_count + 1} : null);
      }

      setNewComment('');
      toast.success("Your comment has been posted!");
    } catch (error: any) {
      if (error.response && error.response.status === 400) {
        toast.error("You have reached your comment limit for this post (3 comments).");
      } else {
        toast.error("Failed to post comment. Please try again.");
      }
    }
  };

  /**
   * Handles user reactions to a confession.
   * @param {string} confessionId - The ID of the confession.
   * @param {string} reactionType - The type of reaction (e.g., 'heart', 'haha').
   */
  const handleReaction = async (confessionId: string, reactionType: string) => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to react.");
      return;
    }
    
    const originalConfessions = [...confessions];

    // Optimistically update the UI
    setConfessions(prevConfessions => 
      prevConfessions.map(conf => {
        if (conf.id === confessionId) {
          const newConf = { ...conf };
          const isTogglingOff = newConf.user_reaction === reactionType;
          
          if (newConf.user_reaction) {
            const oldCountKey = `${newConf.user_reaction}_count` as keyof Confession;
            (newConf[oldCountKey] as number)--;
          }

          if (!isTogglingOff) {
            const newCountKey = `${reactionType}_count` as keyof Confession;
            (newConf[newCountKey] as number)++;
          }

          newConf.user_reaction = isTogglingOff ? null : reactionType;
          return newConf;
        }
        return conf;
      })
    );

    try {
      // API call to update the backend
      const updatedConfession = await reactToConfession(confessionId, reactionType);
      setConfessions(prevConfessions => 
        prevConfessions.map(c => c.id === confessionId ? { ...c, ...updatedConfession } : c)
      );
    } catch (error) {
      toast.error("Failed to save reaction.");
      setConfessions(originalConfessions); // Revert on error
    }
  };

  /**
   * Handles user reactions (like/dislike) to a comment.
   * This function now correctly merges server state with the optimistic UI state.
   * @param {string} confessionId - The ID of the parent confession.
   * @param {string} commentId - The ID of the comment.
   * @param {'like' | 'dislike'} reaction - The reaction type.
   */
  const handleCommentReaction = async (confessionId: string, commentId: string, reaction: 'like' | 'dislike') => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to react to comments.");
      return;
    }

    const originalConfessions = JSON.parse(JSON.stringify(confessions));

    // Logic to perform the optimistic state update.
    const updateReactionState = (conf: Confession) => {
      if (conf.id !== confessionId) return conf;

      return {
        ...conf,
        comments: conf.comments.map(c => {
          if (c.id !== commentId) return c;

          const currentReaction = c.user_reaction;
          const isTogglingOff = currentReaction === reaction;
          
          let newLikeCount = c.like_count;
          let newDislikeCount = c.dislike_count;

          // Decrement the count for the previous reaction, if any.
          if (currentReaction === 'like') newLikeCount--;
          if (currentReaction === 'dislike') newDislikeCount--;

          // If not simply toggling off, increment the count for the new reaction.
          if (!isTogglingOff) {
            if (reaction === 'like') newLikeCount++;
            if (reaction === 'dislike') newDislikeCount++;
          }
          
          return {
            ...c,
            like_count: newLikeCount,
            dislike_count: newDislikeCount,
            user_reaction: isTogglingOff ? null : reaction,
          };
        }),
      };
    };
    
    // Apply the optimistic update to both the main list and the selected confession.
    setConfessions(prev => prev.map(updateReactionState));
    if (selectedConfession) {
        setSelectedConfession(prev => prev ? updateReactionState(prev) : null);
    }

    try {
      const apiCall = reaction === 'like' ? likeComment : dislikeComment;
      const updatedCommentFromServer = await apiCall(commentId);
      
      // Logic to synchronize the state with the server's response.
      const syncWithServer = (conf: Confession) => {
        if (conf.id !== confessionId) return conf;
        
        return {
          ...conf,
          comments: conf.comments.map(c => 
            // **THE FIX**: Merge the server response into the existing comment object.
            // This preserves the optimistically set `user_reaction` while updating other fields
            // like `like_count` from the server.
            c.id === commentId ? { ...c, ...updatedCommentFromServer } : c
          ),
        };
      };

      // Apply the server sync.
      setConfessions(prev => prev.map(syncWithServer));
      if (selectedConfession) {
        setSelectedConfession(prev => prev ? syncWithServer(prev) : null);
      }

    } catch (error) {
      toast.error(`Failed to ${reaction} comment.`);
      // Revert to the original state on error.
      setConfessions(originalConfessions); 
      if (selectedConfession) {
        const originalSelected = originalConfessions.find((c: Confession) => c.id === selectedConfession.id);
        setSelectedConfession(originalSelected || null);
      }
    }
  };


  /**
   * Handles the submission of a new confession.
   */
  const handleSubmitConfession = async () => {
    if (!newConfession.trim()) {
      toast.error('Please write your confession first! 💌');
      return;
    }
    if (!isAuthenticated) {
      toast.error('You must be logged in to post a confession.');
      return;
    }

    try {
      const confessionData = {
        confession: newConfession,
        is_anonymous: isAnonymous,
        is_comment: allowComments,
      };
      const newConfessionResponse = await createConfession(confessionData);
      setConfessions([newConfessionResponse, ...confessions]);
      setNewConfession('');
      setIsPostDialogOpen(false);
      toast.success('Your confession has been shared! 💖');
    } catch (error) {
       toast.error('Failed to share confession. Please try again.');
    }
  };
  
  const handleDownload = (confession: Confession) => {
    toast.success('Screenshot saved! 🖼️');
  };

  const reactionTypes = [
    { type: 'heart', icon: Heart, countKey: 'heart_count' },
    { type: 'haha', icon: Laugh, countKey: 'haha_count' },
    { type: 'whoa', icon: Eye, countKey: 'whoa_count' },
    { type: 'heartbreak', icon: HeartCrack, countKey: 'heartbreak_count' },
  ] as const;

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      <div className="max-w-6xl mx-auto mb-8">
        <div className="flex items-center gap-4 mb-6">
          <Button variant="outline" size="sm" onClick={() => window.history.back()}>
            <ArrowLeft className="w-4 h-4 mr-2" />
            Back
          </Button>
          <CountdownTimer />
        </div>
        <div className="text-center mb-8">
          <h1 className="text-5xl font-dancing text-romantic mb-4">
            Anonymous Confessions 💌
          </h1>
          <p className="text-xl text-muted-foreground">
            Share your heart safely and anonymously
          </p>
        </div>
        <div className="flex flex-col md:flex-row justify-between items-center gap-4">
          <div className="flex items-center gap-4">
            <span className="text-sm text-muted-foreground">Sort by:</span>
            <Button variant={sortBy === 'popularity' ? 'default' : 'outline'} size="sm" onClick={() => setSortBy('popularity')} className={sortBy === 'popularity' ? 'btn-romantic' : 'btn-love'}>
              <TrendingUp className="w-4 h-4 mr-2" />
              Popularity
            </Button>
            <Button variant={sortBy === 'comments' ? 'default' : 'outline'} size="sm" onClick={() => setSortBy('comments')} className={sortBy === 'comments' ? 'btn-romantic' : 'btn-love'}>
              <MessageSquare className="w-4 h-4 mr-2" />
              By comments
            </Button>
            <Button variant={sortBy === 'time' ? 'default' : 'outline'} size="sm" onClick={() => setSortBy('time')} className={sortBy === 'time' ? 'btn-romantic' : 'btn-love'}>
              <Clock className="w-4 h-4 mr-2" />
              Recent
            </Button>
          </div>
          <Dialog open={isPostDialogOpen} onOpenChange={setIsPostDialogOpen}>
            <DialogTrigger asChild>
              <Button className="btn-romantic">
                <Plus className="w-4 h-4 mr-2" />
                Share Your Heart
              </Button>
            </DialogTrigger>
            <DialogContent className="max-w-md">
              <DialogHeader>
                <DialogTitle className="font-dancing text-2xl text-romantic">
                  Share Your Confession 💌
                </DialogTitle>
                <DialogDescription>
                  Express your feelings safely and anonymously
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-4">
                <Textarea placeholder="Share what's in your heart..." value={newConfession} onChange={(e) => setNewConfession(e.target.value)} className="min-h-[120px] bg-background/80 border-romantic/30 focus:border-romantic" />
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-muted-foreground">Post anonymously</span>
                    <Switch checked={isAnonymous} onCheckedChange={setIsAnonymous} />
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-muted-foreground">Allow comments</span>
                    <Switch checked={allowComments} onCheckedChange={setAllowComments} />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button onClick={handleSubmitConfession} className="btn-romantic w-full">
                  <Send className="w-4 h-4 mr-2" />
                  Share Confession
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="max-w-4xl mx-auto space-y-6">
        {confessions.map((confession, index) => (
          <Card key={confession.id} className="confession-card animate-fade-in transition-all duration-300" style={{ animationDelay: `${index * 0.1}s` }}>
            <CardHeader>
              <div className="flex justify-between items-start">
                <div>
                  <CardTitle className="text-lg font-dancing text-romantic">
                    {confession.is_anonymous ? "🤫 Anonymous" : (user?.Name || "User")}
                  </CardTitle>
                  <CardDescription>{new Date(confession.timestamp).toLocaleString('en-GB')}</CardDescription>
                </div>
                <Button variant="outline" size="sm" onClick={() => handleDownload(confession)}>
                  <Download className="w-4 h-4" />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-foreground leading-relaxed">{confession.confession}</p>
              <div className="flex items-center justify-between pt-4 border-t border-border">
                <div className="flex items-center gap-2 md:gap-4">
                  {reactionTypes.map(reaction => {
                    const Icon = reaction.icon;
                    const isSelected = confession.user_reaction === reaction.type;
                    return (
                      <Button
                        key={reaction.type}
                        variant={isSelected ? 'default' : 'ghost'}
                        size="sm"
                        onClick={() => handleReaction(confession.id, reaction.type)}
                        className={`transition-colors ${isSelected ? 'btn-romantic' : 'hover:bg-romantic/10'}`}
                      >
                        <Icon className={`w-4 h-4 mr-1.5 ${isSelected ? 'fill-current' : ''}`} />
                        {confession[reaction.countKey]}
                      </Button>
                    );
                  })}
                </div>
                {/* This button now opens the modal */}
                <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => handleCommentClick(confession)}>
                  <MessageSquare className="w-4 h-4 mr-1" />
                  {confession.comment_count}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
      
      {/* Comment Modal */}
      <Dialog open={!!selectedConfession} onOpenChange={(isOpen) => !isOpen && setSelectedConfession(null)}>
        <DialogContent className="sm:max-w-2xl md:max-w-3xl lg:max-w-4xl w-[95vw] h-[90vh] flex flex-col p-0">
          {selectedConfession && (
            <>
              <DialogHeader className="p-6 pb-4">
                <DialogTitle className="font-dancing text-2xl text-romantic">
                  {selectedConfession.is_anonymous ? "🤫 Anonymous Confession" : (user?.Name || "User") + "'s Confession"}
                </DialogTitle>
                <DialogDescription>{new Date(selectedConfession.timestamp).toLocaleString('en-GB')}</DialogDescription>
              </DialogHeader>
              <div className="px-6 pb-4 border-b">
                <p className="text-foreground leading-relaxed">{selectedConfession.confession}</p>
              </div>

              <div className="flex-grow overflow-y-auto p-6 space-y-4">
                <h4 className="text-lg font-semibold text-romantic">Comments ({selectedConfession.comment_count})</h4>
                {selectedConfession.comments.length > 0 ? (
                  selectedConfession.comments.map(comment => (
                    <div key={comment.id} className="text-sm p-3 bg-muted/50 rounded-lg shadow-sm">
                      <div className="flex items-center gap-2">
                         <span className="text-lg">{comment.user_info.avatar || '👤'}</span>
                         <p className="font-bold text-foreground">{comment.user_info.username}</p>
                      </div>
                      <p className="my-2">{comment.message}</p>
                      <div className="flex items-center justify-between">
                          <p className="text-xs text-muted-foreground">{new Date(comment.timestamp).toLocaleString('en-GB')}</p>
                          <div className="flex items-center gap-2">
                              <Button 
                                size="icon" 
                                variant="ghost" 
                                className={`h-7 w-7 rounded-full transition-colors ${
                                  comment.user_reaction === 'like' 
                                  ? 'bg-red-500 text-white hover:bg-red-600' 
                                  : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                                }`} 
                                onClick={() => handleCommentReaction(selectedConfession.id, comment.id, 'like')}
                              >
                                  <ThumbsUp className="h-4 w-4" />
                              </Button>
                              <span className="text-xs font-medium">{comment.like_count}</span>
                              <Button 
                                size="icon" 
                                variant="ghost" 
                                className={`h-7 w-7 rounded-full transition-colors ${
                                  comment.user_reaction === 'dislike' 
                                  ? 'bg-red-500 text-white hover:bg-red-600' 
                                  : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                                }`} 
                                onClick={() => handleCommentReaction(selectedConfession.id, comment.id, 'dislike')}
                              >
                                  <ThumbsDown className="h-4 w-4" />
                              </Button>
                              <span className="text-xs font-medium">{comment.dislike_count}</span>
                          </div>
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="text-sm text-center text-muted-foreground py-4">No comments yet. Be the first to share your thoughts!</p>
                )}
              </div>

              <DialogFooter className="p-4 border-t bg-background/95">
                <div className="flex gap-2 items-start w-full">
                    <Textarea
                      placeholder="Write a thoughtful comment..."
                      value={newComment}
                      onChange={(e) => setNewComment(e.target.value)}
                      className="flex-grow bg-background border-romantic/30 focus:border-romantic"
                      rows={2}
                    />
                    <Button onClick={() => handlePostComment(selectedConfession.id)} className="btn-romantic" size="icon">
                      <Send className="w-4 h-4" />
                    </Button>
                </div>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
};
