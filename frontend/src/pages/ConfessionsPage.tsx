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
  ThumbsDown,
  Flag,
  Edit,
} from 'lucide-react';
import { toast } from 'sonner';
import { getConfessions, createConfession, reactToConfession, createComment, likeComment, dislikeComment, reportComment, reportConfession, updateConfession } from '../services/api';
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
  report_count: number;
  reported_by: string[];
}

// Interface for a single confession post
interface Confession {
  id: string;
  confession: string;
  is_anonymous: boolean;
  is_comment: boolean;
  user_id: string;
  user_info: UserInfo;
  timestamp: string;
  comments: ConfessionComment[];
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  user_reaction: string | null;
  report_count: number;
  reported_by: string[];
}

export const ConfessionsPage = () => {
  const [confessions, setConfessions] = useState<Confession[]>([]);
  const [sortBy, setSortBy] = useState<'popularity' | 'time' | 'comments'>('popularity');
  const [newConfession, setNewConfession] = useState('');
  const [isAnonymous, setIsAnonymous] = useState(true);
  const [allowComments, setAllowComments] = useState(true);
  const [isPostDialogOpen, setIsPostDialogOpen] = useState(false);
  const { user, isAuthenticated } = useAuth();

  const [selectedConfession, setSelectedConfession] = useState<Confession | null>(null);
  const [newComment, setNewComment] = useState('');
  
  const [isReportConfirmOpen, setIsReportConfirmOpen] = useState(false);
  const [isReportReasonOpen, setIsReportReasonOpen] = useState(false);
  const [commentToReport, setCommentToReport] = useState<ConfessionComment | null>(null);
  const [reportReason, setReportReason] = useState("");

  const [confessionToReport, setConfessionToReport] = useState<Confession | null>(null);
  const [isConfessionReportConfirmOpen, setIsConfessionReportConfirmOpen] = useState(false);
  const [isConfessionReportReasonOpen, setIsConfessionReportReasonOpen] = useState(false);

  // State for the edit dialog
  const [confessionToEdit, setConfessionToEdit] = useState<Confession | null>(null);
  const [editIsAnonymous, setEditIsAnonymous] = useState(false);
  const [editAllowComments, setEditAllowComments] = useState(false);

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

  const handleCommentClick = (confession: Confession) => {
    setSelectedConfession(confession);
  };

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
      
      let updatedConfessionsList: Confession[] = [];
      setConfessions(prev => {
        updatedConfessionsList = prev.map(conf => 
          conf.id === confessionId 
            ? { ...conf, comments: [newCommentData, ...conf.comments], comment_count: conf.comment_count + 1 } 
            : conf
        );
        return updatedConfessionsList;
      });
      
      const updatedSelectedConfession = updatedConfessionsList.find(c => c.id === confessionId);
      if (updatedSelectedConfession) {
        setSelectedConfession(updatedSelectedConfession);
      }

      setNewComment('');
      toast.success("Your comment has been posted!");
    } catch (error: any) {
      if (error.response && error.response.status === 403) {
        toast.error("Comments are disabled for this confession.");
      } else if (error.response && error.response.status === 400) {
        toast.error("You have reached your comment limit for this post (3 comments).");
      } else {
        toast.error("Failed to post comment. Please try again.");
      }
    }
  };

  const handleReaction = async (confessionId: string, reactionType: string) => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to react.");
      return;
    }
    
    const originalConfessions = [...confessions];

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
      const updatedConfession = await reactToConfession(confessionId, reactionType);
      setConfessions(prevConfessions => 
        prevConfessions.map(c => c.id === confessionId ? { ...c, ...updatedConfession } : c)
      );
    } catch (error) {
      toast.error("Failed to save reaction.");
      setConfessions(originalConfessions);
    }
  };

  const handleCommentReaction = async (confessionId: string, commentId: string, reaction: 'like' | 'dislike') => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to react to comments.");
      return;
    }

    const originalConfessions = JSON.parse(JSON.stringify(confessions));

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

          if (currentReaction === 'like') newLikeCount--;
          if (currentReaction === 'dislike') newDislikeCount--;

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
    
    let optimisticallyUpdatedConfessions: Confession[] = [];
    setConfessions(prev => {
        optimisticallyUpdatedConfessions = prev.map(updateReactionState);
        return optimisticallyUpdatedConfessions;
    });
    const optimisticConfession = optimisticallyUpdatedConfessions.find(c => c.id === confessionId);
    if (optimisticConfession) {
        setSelectedConfession(optimisticConfession);
    }


    try {
      const apiCall = reaction === 'like' ? likeComment : dislikeComment;
      const updatedCommentFromServer = await apiCall(commentId);
      
      const syncWithServer = (conf: Confession) => {
        if (conf.id !== confessionId) return conf;
        
        return {
          ...conf,
          comments: conf.comments.map(c => 
            c.id === commentId ? { ...c, ...updatedCommentFromServer } : c
          ),
        };
      };

      let serverSyncedConfessions: Confession[] = [];
      setConfessions(prev => {
          serverSyncedConfessions = prev.map(syncWithServer);
          return serverSyncedConfessions;
      });
      const serverSyncedConfession = serverSyncedConfessions.find(c => c.id === confessionId);
      if (serverSyncedConfession) {
          setSelectedConfession(serverSyncedConfession);
      }

    } catch (error) {
      toast.error(`Failed to ${reaction} comment.`);
      setConfessions(originalConfessions); 
      const originalSelected = originalConfessions.find((c: Confession) => c.id === confessionId);
      setSelectedConfession(originalSelected || null);
    }
  };


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

  const handleReportClick = (comment: ConfessionComment) => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to report a comment.");
      return;
    }
    setCommentToReport(comment);
    setIsReportConfirmOpen(true);
  };

  const handleConfirmReport = () => {
    setIsReportConfirmOpen(false);
    setIsReportReasonOpen(true);
  };

  const handleReportSubmit = async () => {
    if (!reportReason.trim()) {
      toast.info("Please provide a reason for the report.");
      return;
    }
    if (!commentToReport || !user) return;

    try {
      const updatedComment = await reportComment(commentToReport.id, reportReason);
      
      const updateCommentInState = (prev: Confession[]) => prev.map(conf => {
        if (conf.id === selectedConfession?.id) {
          return {
            ...conf,
            comments: conf.comments.map(c => c.id === commentToReport.id ? updatedComment : c)
          };
        }
        return conf;
      });

      setConfessions(updateCommentInState);
      if (selectedConfession) {
        setSelectedConfession(prev => prev ? {
            ...prev,
            comments: prev.comments.map(c => c.id === commentToReport.id ? updatedComment : c)
        } : null);
      }

      toast.success("Comment reported successfully.");
    } catch (error) {
      toast.error("Failed to report comment. You may have already reported it.");
    } finally {
      setIsReportReasonOpen(false);
      setReportReason("");
      setCommentToReport(null);
    }
  };

  const handleReportConfessionClick = (confession: Confession) => {
    if (!isAuthenticated) {
        toast.error("You must be logged in to report a confession.");
        return;
    }
    setConfessionToReport(confession);
    setIsConfessionReportConfirmOpen(true);
  };

  const handleConfirmConfessionReport = () => {
      setIsConfessionReportConfirmOpen(false);
      setIsConfessionReportReasonOpen(true);
  };

  const handleConfessionReportSubmit = async () => {
      if (!reportReason.trim()) {
          toast.info("Please provide a reason for the report.");
          return;
      }
      if (!confessionToReport || !user) return;

      try {
          const updatedConfession = await reportConfession(confessionToReport.id, reportReason);
          
          setConfessions(prev => prev.map(conf => 
              conf.id === confessionToReport.id ? updatedConfession : conf
          ));

          toast.success("Confession reported successfully.");
      } catch (error) {
          toast.error("Failed to report confession. You may have already reported it.");
      } finally {
          setIsConfessionReportReasonOpen(false);
          setReportReason("");
          setConfessionToReport(null);
      }
  };

  const handleEditClick = (confession: Confession) => {
    setConfessionToEdit(confession);
    setEditIsAnonymous(confession.is_anonymous);
    setEditAllowComments(confession.is_comment);
  };

  const handleSaveChanges = async () => {
    if (!confessionToEdit) return;

    const payload: { is_anonymous?: boolean; is_comment?: boolean } = {};

    if (editAllowComments !== confessionToEdit.is_comment) {
      payload.is_comment = editAllowComments;
    }
    if (editIsAnonymous !== confessionToEdit.is_anonymous) {
      payload.is_anonymous = editIsAnonymous;
    }

    if (Object.keys(payload).length === 0) {
      toast.info("No changes were made.");
      setConfessionToEdit(null);
      return;
    }

    try {
      const updatedConfession = await updateConfession(confessionToEdit.id, payload);
      
      setConfessions(prev => 
        prev.map(conf => conf.id === confessionToEdit.id ? updatedConfession : conf)
      );

      if (selectedConfession?.id === confessionToEdit.id) {
        setSelectedConfession(updatedConfession);
      }

      toast.success("Your confession has been updated!");
      setConfessionToEdit(null);
    } catch (error) {
      toast.error("Failed to update confession. Please try again.");
      console.error(error);
    }
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
        {confessions.map((confession, index) => {
          const hasReportedConfession = user && confession.reported_by.includes(user.id);
          return (
            <Card key={confession.id} className="confession-card animate-fade-in transition-all duration-300" style={{ animationDelay: `${index * 0.1}s` }}>
              <CardHeader>
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg font-dancing text-romantic">
                      {confession.is_anonymous ? "🤫 Anonymous" : (confession.user_info?.username || "User")}
                    </CardTitle>
                    <CardDescription>{new Date(confession.timestamp).toLocaleString('en-GB')}</CardDescription>
                  </div>
                  <div className="flex items-center gap-2">
                    {isAuthenticated && user?.id === confession.user_id && (
                        <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8 rounded-full"
                        onClick={() => handleEditClick(confession)}
                        >
                        <Edit className="h-4 w-4" />
                        </Button>
                    )}
                    <Button
                      size="icon"
                      variant="ghost"
                      className={`h-8 w-8 rounded-full transition-colors ${
                        hasReportedConfession
                          ? 'bg-yellow-500 text-white hover:bg-yellow-600'
                          : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                      }`}
                      onClick={() => !hasReportedConfession && handleReportConfessionClick(confession)}
                      disabled={hasReportedConfession}
                    >
                      <Flag className="h-4 w-4" />
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => handleDownload(confession)}>
                      <Download className="w-4 h-4" />
                    </Button>
                  </div>
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
                  <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => handleCommentClick(confession)}>
                    <MessageSquare className="w-4 h-4 mr-1" />
                    {confession.comment_count}
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
      
      <Dialog open={!!selectedConfession} onOpenChange={(isOpen) => !isOpen && setSelectedConfession(null)}>
        <DialogContent className="sm:max-w-2xl md:max-w-3xl lg:max-w-4xl w-[95vw] h-[90vh] flex flex-col p-0">
          {selectedConfession && (
            <>
              <DialogHeader className="p-6 pb-4">
                <DialogTitle className="font-dancing text-2xl text-romantic">
                  {selectedConfession.is_anonymous ? "🤫 Anonymous Confession" : (selectedConfession.user_info?.username || "User") + "'s Confession"}
                </DialogTitle>
                <DialogDescription>{new Date(selectedConfession.timestamp).toLocaleString('en-GB')}</DialogDescription>
              </DialogHeader>
              <div className="px-6 pb-4 border-b">
                <p className="text-foreground leading-relaxed">{selectedConfession.confession}</p>
              </div>

              <div className="flex-grow overflow-y-auto p-6 space-y-4">
                <h4 className="text-lg font-semibold text-romantic">Comments ({selectedConfession.comment_count})</h4>
                {selectedConfession.comments.length > 0 ? (
                  selectedConfession.comments.map(comment => {
                    const hasReported = user && comment.reported_by.includes(user.id);
                    return (
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
                               <Button 
                                size="icon" 
                                variant="ghost" 
                                className={`h-7 w-7 rounded-full transition-colors ${
                                  hasReported
                                  ? 'bg-yellow-500 text-white hover:bg-yellow-600' 
                                  : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                                }`} 
                                onClick={() => !hasReported && handleReportClick(comment)}
                                disabled={hasReported}
                              >
                                  <Flag className="h-4 w-4" />
                              </Button>
                              <span className="text-xs font-medium">{comment.report_count}</span>
                          </div>
                      </div>
                    </div>
                  )})
                ) : (
                  <p className="text-sm text-center text-muted-foreground py-4">No comments yet. Be the first to share your thoughts!</p>
                )}
              </div>

              <DialogFooter className="p-4 border-t bg-background/95">
                {selectedConfession.is_comment ? (
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
                ) : (
                    <div className="w-full text-center text-sm text-muted-foreground">
                        Comments are disabled for this post.
                    </div>
                )}
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
      
      <Dialog open={isReportConfirmOpen} onOpenChange={setIsReportConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Report Comment</DialogTitle>
            <DialogDescription>
              Are you sure you want to report this comment? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsReportConfirmOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleConfirmReport}>Yes, Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isReportReasonOpen} onOpenChange={setIsReportReasonOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reason for Reporting</DialogTitle>
            <DialogDescription>
              Please provide a reason for reporting this comment. This will help our moderators review it.
            </DialogDescription>
          </DialogHeader>
          <Textarea 
            placeholder="e.g., Harassment, spam, inappropriate content..."
            value={reportReason}
            onChange={(e) => setReportReason(e.target.value)}
            className="my-4"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsReportReasonOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleReportSubmit}>Submit Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isConfessionReportConfirmOpen} onOpenChange={setIsConfessionReportConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Report Confession</DialogTitle>
            <DialogDescription>
              Are you sure you want to report this confession? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsConfessionReportConfirmOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleConfirmConfessionReport}>Yes, Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isConfessionReportReasonOpen} onOpenChange={setIsConfessionReportReasonOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reason for Reporting</DialogTitle>
            <DialogDescription>
              Please provide a reason for reporting this confession. This will help our moderators review it.
            </DialogDescription>
          </DialogHeader>
          <Textarea 
            placeholder="e.g., Harassment, spam, inappropriate content..."
            value={reportReason}
            onChange={(e) => setReportReason(e.target.value)}
            className="my-4"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsConfessionReportReasonOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleConfessionReportSubmit}>Submit Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Confession Dialog */}
        <Dialog open={!!confessionToEdit} onOpenChange={(isOpen) => !isOpen && setConfessionToEdit(null)}>
        <DialogContent>
            <DialogHeader>
            <DialogTitle>Edit Your Confession</DialogTitle>
            <DialogDescription>
                Manage settings for your confession.
            </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
            <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Allow comments</span>
                <Switch
                checked={editAllowComments}
                onCheckedChange={setEditAllowComments}
                />
            </div>
            <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Post anonymously</span>
                <Switch
                checked={editIsAnonymous}
                onCheckedChange={setEditIsAnonymous}
                disabled={!confessionToEdit?.is_anonymous}
                />
            </div>
            {!confessionToEdit?.is_anonymous && (
                <p className="text-xs text-muted-foreground">You cannot make a public confession anonymous after posting.</p>
            )}
            </div>
            <DialogFooter>
            <Button variant="outline" onClick={() => setConfessionToEdit(null)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleSaveChanges}>Save Changes</Button>
            </DialogFooter>
        </DialogContent>
        </Dialog>
    </div>
  );
};
