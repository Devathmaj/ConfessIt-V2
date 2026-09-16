import { useEffect, useMemo, useState } from 'react';
import { AdminNavigation } from '@/components/AdminNavigation';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { toast } from 'sonner';
import { createAdminUser, getAdminUsers, setAdminUserBlocked, updateAdminUser } from '@/services/api';
import { Search, ShieldCheck, ShieldX, UsersRound, Mail, User, Plus } from 'lucide-react';
import { formatDateTimeDDMMYYYY } from '@/lib/utils';
import { AccountType } from '@/lib/capabilities';

/**
 * The three kinds of content a participant can be reported for. A confession and a comment on
 * one share a kind, so the keys here match `report_counters.REPORT_KINDS` on the server.
 */
export type ReportKind = 'confession' | 'love_note' | 'message';

export interface ReportTally {
  /** Every report filed against this person's content of this kind, dismissed ones included. */
  reports: number;
  /** Only the reports a moderator took the content down for. The one to act on. */
  removals: number;
}

/** Row order and wording for the tally, so all three kinds read the same way in the UI. */
const REPORT_KIND_LABELS: Record<ReportKind, string> = {
  confession: 'Confessions & comments',
  love_note: 'Love notes',
  message: 'Chat messages',
};

const REPORT_KIND_ORDER: ReportKind[] = ['confession', 'love_note', 'message'];

interface AdminUserProfile {
  id: string;
  regno?: string | null;
  name?: string | null;
  email?: string | null;
  class_name?: string | null;
  gender?: string | null;
  /**
   * Which account table the row came from. There is no role: a staff account has no row in
   * ``users`` at all, so the table is the only thing that says what kind of account this is.
   */
  account_type?: string | null;
  matchmaking_enabled?: boolean;
  notifications_enabled?: boolean;
  love_notes_receive_enabled?: boolean;
  love_notes_send_enabled?: boolean;
  /**
   * How often this participant's content has been reported, per kind, and how many of those
   * reports a moderator upheld. Absent on staff rows, which have no reportable content.
   *
   * `removals` is the authoritative figure -- `reports` includes complaints that were
   * dismissed, so it measures how often people complained rather than what was upheld.
   */
  report_history?: Partial<Record<ReportKind, ReportTally>>;
  last_login_at?: string | null;
  last_login_ip?: string | null;
  is_blocked?: boolean;
  bio?: string | null;
  emoji?: string | null;
  profile_picture_url?: string | null;
  interests?: string[] | null;
  last_matchmaking_at?: string | null;
  /**
   * When this participant last posted a confession of their own. Read by the server for the
   * two-hour cooldown; carried here so the field is not silently dropped from an account
   * payload. Optional because the API omits it when the participant has never confessed.
   */
  last_confession_at?: string | null;
}

type CreateUserForm = {
  regno: string;
  name: string;
  email: string;
  class_name: string;
  gender: string;
  emoji: string;
  bio: string;
  profile_picture_url: string;
  matchmaking_enabled: boolean;
  notifications_enabled: boolean;
  love_notes_receive_enabled: boolean;
  love_notes_send_enabled: boolean;
  /**
   * Which table to create the account in. Sent as ``account_type`` and used by the server to
   * choose the table, which is why it is a table name and not a label: a staff account is
   * created by writing to ``admin`` or ``moderator``, not by setting a flag on a participant.
   */
  account_type: AccountType;
};

const formatDate = (value?: string | null) => {
  if (!value) return 'Never';
  return formatDateTimeDDMMYYYY(value);
};

/**
 * How each account table reads in a sentence. The table name is what the API and the
 * database use, and it stays that way in every request; this is only for text an operator
 * reads. `users` in particular is a table of participants, not an account called "Users".
 */
const ACCOUNT_TYPE_LABELS: Record<AccountType, string> = {
  users: 'Participant',
  admin: 'Admin',
  moderator: 'Moderator',
};

const makeInitials = (name?: string | null) => {
  if (!name) return '??';
  return name
    .split(' ')
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase())
    .join('');
};

export const ProfileReview = () => {
  const buildDefaultCreateForm = (): CreateUserForm => ({
    regno: '',
    name: '',
    email: '',
    class_name: '',
    gender: '',
    emoji: '',
    bio: '',
    profile_picture_url: '',
    matchmaking_enabled: true,
    notifications_enabled: true,
    love_notes_receive_enabled: true,
    love_notes_send_enabled: false,
    account_type: 'users',
  });

  const [profiles, setProfiles] = useState<AdminUserProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  const [blocking, setBlocking] = useState<string | null>(null);
  const [selectedProfile, setSelectedProfile] = useState<AdminUserProfile | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState<Partial<AdminUserProfile> | null>(null);
  const [interestsInput, setInterestsInput] = useState('');
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateUserForm>(() => buildDefaultCreateForm());
  const [createInterestsInput, setCreateInterestsInput] = useState('');
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    const fetchProfiles = async () => {
      try {
        setLoading(true);
        const data = await getAdminUsers();
        setProfiles(data);
      } catch (error) {
        console.error('Failed to fetch profiles', error);
        toast.error('Unable to load user profiles.');
      } finally {
        setLoading(false);
      }
    };

    fetchProfiles();
  }, []);

  const filteredProfiles = useMemo(() => {
    const query = searchTerm.trim().toLowerCase();
    if (!query) return profiles;
    return profiles.filter((profile) => {
      const haystack = [
        profile.name,
        profile.regno,
        profile.email,
        profile.class_name,
      ]
        .filter(Boolean)
        .join(' ') // join to single string
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [profiles, searchTerm]);

  const toggleBlock = async (profile: AdminUserProfile) => {
    if (!profile.id) return;

    try {
      setBlocking(profile.id);
      const nextState = !profile.is_blocked;
      await setAdminUserBlocked(profile.id, nextState);
      setProfiles((prev) =>
        prev.map((item) =>
          item.id === profile.id ? { ...item, is_blocked: nextState } : item
        )
      );
      toast.success(`User ${nextState ? 'blocked' : 'unblocked'} successfully.`);
    } catch (error) {
      console.error('Failed to update block state', error);
      toast.error('Unable to update block status for this user.');
    } finally {
      setBlocking(null);
    }
  };

  const openProfileEditor = (profile: AdminUserProfile) => {
    setSelectedProfile(profile);
    setEditForm({
      name: profile.name ?? '',
      regno: profile.regno ?? '',
      email: profile.email ?? '',
      class_name: profile.class_name ?? '',
      gender: profile.gender ?? '',
      bio: profile.bio ?? '',
      emoji: profile.emoji ?? '',
      profile_picture_url: profile.profile_picture_url ?? '',
      matchmaking_enabled: profile.matchmaking_enabled ?? false,
      notifications_enabled: profile.notifications_enabled ?? false,
      love_notes_receive_enabled: profile.love_notes_receive_enabled ?? true,
      love_notes_send_enabled: profile.love_notes_send_enabled ?? false,
    });
    setInterestsInput(profile.interests?.join(', ') ?? '');
    setEditOpen(true);
  };

  const resetEditor = () => {
    setEditOpen(false);
    setSelectedProfile(null);
    setEditForm(null);
    setInterestsInput('');
    setSaving(false);
  };

  const handleSaveProfile = async () => {
    if (!selectedProfile || !editForm) {
      return;
    }

    const payload: Record<string, unknown> = {};
    const textFields: Array<keyof AdminUserProfile> = [
      'name',
      'regno',
      'email',
      'class_name',
      'gender',
      'bio',
      'emoji',
      'profile_picture_url',
    ];
    const booleanFields: Array<keyof AdminUserProfile> = [
      'matchmaking_enabled',
      'notifications_enabled',
      'love_notes_receive_enabled',
      'love_notes_send_enabled',
    ];

    textFields.forEach((field) => {
      const newValue = (editForm as Record<string, unknown>)[field];
      if (newValue === undefined) return;
      const currentValue = selectedProfile[field];
      if ((newValue ?? '') !== (currentValue ?? '')) {
        payload[field] = typeof newValue === 'string' ? newValue : '';
      }
    });

    booleanFields.forEach((field) => {
      const newValue = (editForm as Record<string, unknown>)[field];
      if (typeof newValue !== 'boolean') return;
      const currentValue = Boolean(selectedProfile[field]);
      if (newValue !== currentValue) {
        payload[field] = newValue;
      }
    });

    const sanitizedInterests = interestsInput
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean);
    const currentInterests = selectedProfile.interests ?? [];
    if (JSON.stringify(sanitizedInterests) !== JSON.stringify(currentInterests)) {
      payload.interests = sanitizedInterests;
    }

    if (Object.keys(payload).length === 0) {
      toast.info('No changes to save.');
      return;
    }

    try {
      setSaving(true);
      const updatedProfile = await updateAdminUser(selectedProfile.id, payload);
      setProfiles((prev) =>
        prev.map((profile) =>
          profile.id === updatedProfile.id ? { ...profile, ...updatedProfile } : profile
        )
      );
      toast.success('Profile updated successfully.');
      resetEditor();
    } catch (error) {
      console.error('Failed to update profile', error);
      toast.error('Unable to save changes for this user.');
    } finally {
      setSaving(false);
    }
  };

  const openCreateDialog = () => {
    setCreateForm(buildDefaultCreateForm());
    setCreateInterestsInput('');
    setCreateOpen(true);
  };

  const resetCreateDialog = () => {
    setCreateOpen(false);
    setCreateForm(buildDefaultCreateForm());
    setCreateInterestsInput('');
    setCreating(false);
  };

  const handleCreateUser = async () => {
    const requiredFields: Array<[keyof CreateUserForm, string]> = [
      ['name', createForm.name],
      ['regno', createForm.regno],
      ['email', createForm.email],
      ['class_name', createForm.class_name],
      ['gender', createForm.gender],
    ];

    const missingField = requiredFields.find(([, value]) => !value.trim());
    if (missingField) {
      toast.error('Please fill in all required fields before creating an account.');
      return;
    }

    const sanitizedInterests = createInterestsInput
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);

    const payload: Record<string, unknown> = {
      regno: createForm.regno.trim(),
      name: createForm.name.trim(),
      email: createForm.email.trim(),
      class_name: createForm.class_name.trim(),
      gender: createForm.gender.trim(),
      account_type: createForm.account_type,
      matchmaking_enabled: createForm.matchmaking_enabled,
      notifications_enabled: createForm.notifications_enabled,
      love_notes_receive_enabled: createForm.love_notes_receive_enabled,
      love_notes_send_enabled: createForm.love_notes_send_enabled,
    };

    if (createForm.emoji.trim()) {
      payload.emoji = createForm.emoji.trim();
    }
    if (createForm.bio.trim()) {
      payload.bio = createForm.bio.trim();
    }
    if (createForm.profile_picture_url.trim()) {
      payload.profile_picture_url = createForm.profile_picture_url.trim();
    }
    if (sanitizedInterests.length > 0) {
      payload.interests = sanitizedInterests;
    }

    try {
      setCreating(true);
      const createdProfile = await createAdminUser(payload);
      setProfiles((prev) => [createdProfile, ...prev]);
      toast.success(
        `${ACCOUNT_TYPE_LABELS[createForm.account_type]} account created successfully.`
      );
      resetCreateDialog();
    } catch (error: unknown) {
      console.error('Failed to create user', error);
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ?? 'Unable to create this account.');
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">Profile Review</h1>
          <p className="text-sm text-muted-foreground">
            Inspect every user account and block harmful profiles instantly.
          </p>
          <p className="text-xs text-muted-foreground/80">
            Total users: {profiles.length} · Blocked: {profiles.filter((p) => p.is_blocked).length}
          </p>
        </header>

        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="relative w-full max-w-md">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search by name, register number, email, or class"
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              className="pl-9"
            />
          </div>
          <div className="flex items-center gap-3">
            <Badge variant="secondary" className="w-max gap-2">
              <UsersRound className="h-4 w-4" />
              Showing {filteredProfiles.length} profiles
            </Badge>
            <Button size="sm" onClick={openCreateDialog}>
              <Plus className="mr-2 h-4 w-4" />
              New account
            </Button>
          </div>
        </div>

        <Card className="border border-border">
          <CardHeader className="flex flex-col gap-1">
            <CardTitle className="text-lg">Profiles</CardTitle>
            <CardDescription>
              Click block to immediately suspend access. Unblock to restore their account.
            </CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-[70vh]">
              <div className="divide-y divide-border">
                {loading ? (
                  <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
                    Loading user profiles...
                  </div>
                ) : filteredProfiles.length === 0 ? (
                  <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
                    No profiles match your search.
                  </div>
                ) : (
                  filteredProfiles.map((profile) => {
                    const blocked = Boolean(profile.is_blocked);
                    // Staff accounts have no reportable content, so the tally is absent on
                    // their rows rather than present-and-zero. Derived from the table the row
                    // came from, the same way the rest of this app distinguishes accounts.
                    const isParticipant = profile.account_type === 'users';
                    const totalRemovals = isParticipant
                      ? REPORT_KIND_ORDER.reduce(
                          (sum, kind) => sum + (profile.report_history?.[kind]?.removals ?? 0),
                          0,
                        )
                      : 0;
                    return (
                      <div
                        key={profile.id}
                        className="flex flex-col gap-4 px-4 py-4 md:flex-row md:items-center md:justify-between"
                      >
                        <div className="flex flex-1 flex-col gap-2">
                          <div
                            className="flex cursor-pointer items-start gap-4 rounded-xl border border-transparent p-2 transition hover:border-border hover:bg-muted/30"
                            onClick={() => openProfileEditor(profile)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                openProfileEditor(profile);
                              }
                            }}
                            role="button"
                            tabIndex={0}
                            aria-label={`View profile for ${profile.name ?? 'user'}`}
                          >
                            <Avatar className="h-12 w-12">
                              <AvatarFallback>{makeInitials(profile.name)}</AvatarFallback>
                            </Avatar>
                            <div className="grid gap-1 text-sm">
                              <div className="flex flex-wrap items-center gap-2 text-base font-semibold">
                                <span>{profile.name ?? 'Unknown user'}</span>
                                {profile.account_type && profile.account_type !== 'users' && (
                                  // Only staff is badged. A participant badge on every row of a
                                  // hundred would be noise, and the one thing an operator needs
                                  // to see at a glance is which rows are not participants.
                                  <Badge variant={profile.account_type === 'admin' ? 'default' : 'secondary'}>
                                    {ACCOUNT_TYPE_LABELS[profile.account_type as AccountType] ??
                                      profile.account_type}
                                  </Badge>
                                )}
                                {blocked && <Badge variant="destructive">Blocked</Badge>}
                              </div>
                              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                                {profile.regno && (
                                  <span className="flex items-center gap-1">
                                    <User className="h-3 w-3" />
                                    {profile.regno}
                                  </span>
                                )}
                                {profile.class_name && (
                                  <span className="flex items-center gap-1">
                                    <Badge variant="outline">{profile.class_name}</Badge>
                                  </span>
                                )}
                                {profile.email && (
                                  <span className="flex items-center gap-1">
                                    <Mail className="h-3 w-3" />
                                    {profile.email}
                                  </span>
                                )}
                              </div>
                              <div className="grid gap-1 text-xs text-muted-foreground">
                                {/* Only participants have reportable content, so a staff row
                                    renders nothing here rather than three rows of zeros that
                                    would read as a clean record. */}
                                {isParticipant && (
                                  <div className="mt-1 space-y-1 rounded-md border p-2">
                                    <div className="flex items-center justify-between">
                                      <span className="font-medium text-foreground">
                                        Report history
                                      </span>
                                      {totalRemovals > 0 && (
                                        <Badge variant="destructive">
                                          {totalRemovals} removed
                                        </Badge>
                                      )}
                                    </div>
                                    {REPORT_KIND_ORDER.map((kind) => {
                                      const tally = profile.report_history?.[kind];
                                      return (
                                        <div
                                          key={kind}
                                          className="flex items-center justify-between gap-2"
                                        >
                                          <span>{REPORT_KIND_LABELS[kind]}</span>
                                          <span className="font-mono tabular-nums">
                                            {/* Removals first and in the foreground: it is the
                                                number a decision should be based on, and
                                                `reports` beside it is the number of complaints
                                                that survived scrutiny. Each figure is its own
                                                element so the pair reads as two facts rather
                                                than one string. */}
                                            <span
                                              className={
                                                (tally?.removals ?? 0) > 0
                                                  ? 'font-semibold text-destructive'
                                                  : ''
                                              }
                                            >
                                              {tally?.removals ?? 0}
                                            </span>
                                            <span className="text-muted-foreground">
                                              removed
                                            </span>
                                            <span
                                              aria-hidden="true"
                                              className="px-1 text-muted-foreground"
                                            >
                                              /
                                            </span>
                                            <span className="text-muted-foreground">
                                              {tally?.reports ?? 0} reported
                                            </span>
                                          </span>
                                        </div>
                                      );
                                    })}
                                    <p className="text-[11px] leading-snug text-muted-foreground">
                                      Removals are the authoritative count: reports includes
                                      complaints a moderator dismissed as unfounded.
                                    </p>
                                  </div>
                                )}
                                <span>Last login: {formatDate(profile.last_login_at)}</span>
                                {profile.last_login_ip && <span>Last IP: {profile.last_login_ip}</span>}
                              </div>
                            </div>
                          </div>
                        </div>
                        <div className="flex w-full flex-col gap-2 md:w-auto">
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={blocking === profile.id}
                            className={
                              blocked
                                ? 'border-green-500 text-green-500 hover:bg-green-500 hover:text-white'
                                : 'border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground'
                            }
                            onClick={() => toggleBlock(profile)}
                          >
                            {blocked ? (
                              <>
                                <ShieldCheck className="mr-2 h-4 w-4" />
                                Unblock user
                              </>
                            ) : (
                              <>
                                <ShieldX className="mr-2 h-4 w-4" />
                                Block user
                              </>
                            )}
                          </Button>
                          <div className="text-xs text-muted-foreground">
                            Matchmaking: {profile.matchmaking_enabled ? 'Enabled' : 'Disabled'} · Notifications:{' '}
                            {profile.notifications_enabled ? 'On' : 'Off'} · Love notes receive:{' '}
                            {profile.love_notes_receive_enabled ? 'On' : 'Off'}
                          </div>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </ScrollArea>
          </CardContent>
        </Card>
      </div>

      <Dialog
        open={editOpen}
        onOpenChange={(open) => {
          if (!open) {
            resetEditor();
          }
        }}
      >
        <DialogContent className="max-w-2xl" aria-describedby={undefined}>
          {selectedProfile && editForm ? (
            <>
              <DialogHeader>
                <DialogTitle>Edit profile</DialogTitle>
                <DialogDescription>
                  Update the selected user&apos;s information. Leave a field untouched to keep the current value.
                </DialogDescription>
              </DialogHeader>

              <ScrollArea className="max-h-[65vh] pr-2">
                <div className="grid gap-6 py-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="grid gap-2">
                    <Label htmlFor="edit-name">Full name</Label>
                    <Input
                      id="edit-name"
                      value={editForm.name ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, name: event.target.value } : prev))
                      }
                      placeholder="Enter full name"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-email">Email</Label>
                    <Input
                      id="edit-email"
                      type="email"
                      value={editForm.email ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, email: event.target.value } : prev))
                      }
                      placeholder="user@example.com"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-regno">Register number</Label>
                    <Input
                      id="edit-regno"
                      value={editForm.regno ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, regno: event.target.value } : prev))
                      }
                      placeholder="Enter register number"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-class">Class / department</Label>
                    <Input
                      id="edit-class"
                      value={editForm.class_name ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, class_name: event.target.value } : prev))
                      }
                      placeholder="CSE, ECE, Year, etc."
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-gender">Gender</Label>
                    <Input
                      id="edit-gender"
                      value={editForm.gender ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, gender: event.target.value } : prev))
                      }
                      placeholder="Enter gender"
                    />
                  </div>
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  <div className="grid gap-2">
                    <Label htmlFor="edit-emoji">Emoji tag</Label>
                    <Input
                      id="edit-emoji"
                      value={editForm.emoji ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, emoji: event.target.value } : prev))
                      }
                      placeholder="e.g. 😄"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-avatar">Profile picture ID</Label>
                    <Input
                      id="edit-avatar"
                      value={editForm.profile_picture_url ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) =>
                          prev ? { ...prev, profile_picture_url: event.target.value } : prev
                        )
                      }
                      placeholder="Optional asset identifier"
                    />
                  </div>
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="edit-bio">Bio</Label>
                  <Textarea
                    id="edit-bio"
                    rows={4}
                    value={editForm.bio ?? ''}
                    onChange={(event) =>
                      setEditForm((prev) => (prev ? { ...prev, bio: event.target.value } : prev))
                    }
                    placeholder="Short description that appears on their profile"
                  />
                </div>

                <div className="grid gap-2">
                  <Label htmlFor="edit-interests">Interests</Label>
                  <Textarea
                    id="edit-interests"
                    rows={3}
                    value={interestsInput}
                    onChange={(event) => setInterestsInput(event.target.value)}
                    placeholder="Comma-separated interests (e.g. music, movies, travel)"
                  />
                  <p className="text-xs text-muted-foreground">
                    Interests help matchmaking results. Separate values with commas.
                  </p>
                </div>

                <div className="grid gap-3 md:grid-cols-2">
                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">Allow matchmaking</p>
                      <p className="text-xs text-muted-foreground">
                        Enable to include this user in matchmaking suggestions.
                      </p>
                    </div>
                    <Switch
                      checked={Boolean(editForm.matchmaking_enabled)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, matchmaking_enabled: checked } : prev))
                      }
                    />
                  </div>
                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">Email notifications</p>
                      <p className="text-xs text-muted-foreground">
                        Toggle to control announcement emails for this user.
                      </p>
                    </div>
                    <Switch
                      checked={Boolean(editForm.notifications_enabled)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, notifications_enabled: checked } : prev))
                      }
                    />
                  </div>
                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">Receive love notes</p>
                      <p className="text-xs text-muted-foreground">
                        When off, other students cannot send new love notes.
                      </p>
                    </div>
                    <Switch
                      checked={Boolean(editForm.love_notes_receive_enabled)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, love_notes_receive_enabled: checked } : prev))
                      }
                    />
                  </div>
                  <div className="flex items-center justify-between rounded-md border px-3 py-2">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">Send love notes</p>
                      <p className="text-xs text-muted-foreground">
                        Disable if the user is restricted from sending notes.
                      </p>
                    </div>
                    <Switch
                      checked={Boolean(editForm.love_notes_send_enabled)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, love_notes_send_enabled: checked } : prev))
                      }
                    />
                  </div>
                </div>
                </div>
              </ScrollArea>

              <DialogFooter className="gap-2 sm:gap-0">
                <Button variant="outline" onClick={resetEditor} disabled={saving}>
                  Cancel
                </Button>
                <Button onClick={handleSaveProfile} disabled={saving}>
                  {saving ? 'Saving...' : 'Save changes'}
                </Button>
              </DialogFooter>
            </>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog
        open={createOpen}
        onOpenChange={(open) => {
          if (!open) {
            resetCreateDialog();
          } else {
            setCreateOpen(true);
          }
        }}
      >
        <DialogContent className="max-w-2xl" aria-describedby={undefined}>
          <DialogHeader>
            <DialogTitle>Create new account</DialogTitle>
            <DialogDescription>
              Provision a fresh student or administrator profile. Required fields are marked and the account is active immediately.
            </DialogDescription>
          </DialogHeader>

          <ScrollArea className="max-h-[65vh] pr-2">
            <div className="grid gap-6 py-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="grid gap-2">
                  <Label htmlFor="create-name">Full name *</Label>
                  <Input
                    id="create-name"
                    value={createForm.name}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, name: event.target.value }))
                    }
                    placeholder="Enter full name"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-regno">Register number *</Label>
                  <Input
                    id="create-regno"
                    value={createForm.regno}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, regno: event.target.value }))
                    }
                    placeholder="eg. 21CS100"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-email">Email *</Label>
                  <Input
                    id="create-email"
                    type="email"
                    value={createForm.email}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, email: event.target.value }))
                    }
                    placeholder="user@example.com"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-class">Class / department *</Label>
                  <Input
                    id="create-class"
                    value={createForm.class_name}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, class_name: event.target.value }))
                    }
                    placeholder="CSE, ECE, Year, etc."
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-gender">Gender *</Label>
                  <Input
                    id="create-gender"
                    value={createForm.gender}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, gender: event.target.value }))
                    }
                    placeholder="Enter gender"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-emoji">Emoji tag</Label>
                  <Input
                    id="create-emoji"
                    value={createForm.emoji}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, emoji: event.target.value }))
                    }
                    placeholder="eg. 😄"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-avatar">Profile picture URL</Label>
                  <Input
                    id="create-avatar"
                    value={createForm.profile_picture_url}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, profile_picture_url: event.target.value }))
                    }
                    placeholder="Optional asset identifier"
                  />
                </div>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="create-bio">Bio</Label>
                <Textarea
                  id="create-bio"
                  rows={4}
                  value={createForm.bio}
                  onChange={(event) =>
                    setCreateForm((prev) => ({ ...prev, bio: event.target.value }))
                  }
                  placeholder="Short description that appears on their profile"
                />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="create-interests">Interests</Label>
                <Textarea
                  id="create-interests"
                  rows={3}
                  value={createInterestsInput}
                  onChange={(event) => setCreateInterestsInput(event.target.value)}
                  placeholder="Comma-separated interests (e.g. music, movies, travel)"
                />
                <p className="text-xs text-muted-foreground">
                  Interests enrich matchmaking recommendations. Separate multiple values with commas.
                </p>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="create-account-type">Account type *</Label>
                  <p className="text-xs text-muted-foreground">
                    Decides which table the account is created in. A participant can post
                    confessions and be matched; a staff account cannot, and a moderator sees
                    fewer moderation screens than an admin.
                  </p>
                  <Select
                  value={createForm.account_type}
                  onValueChange={(value) =>
                    setCreateForm((prev) => ({ ...prev, account_type: value as AccountType }))
                  }
                >
                  <SelectTrigger id="create-account-type">
                    <SelectValue placeholder="Choose an account type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="users">Participant</SelectItem>
                    <SelectItem value="moderator">Moderator</SelectItem>
                    <SelectItem value="admin">Admin</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <div className="flex items-center justify-between rounded-md border px-3 py-2">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Allow matchmaking</p>
                    <p className="text-xs text-muted-foreground">
                      Enable to include the new account in matchmaking results.
                    </p>
                  </div>
                  <Switch
                    checked={createForm.matchmaking_enabled}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, matchmaking_enabled: checked }))
                    }
                  />
                </div>
                <div className="flex items-center justify-between rounded-md border px-3 py-2">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Email notifications</p>
                    <p className="text-xs text-muted-foreground">
                      Controls announcement emails for this account.
                    </p>
                  </div>
                  <Switch
                    checked={createForm.notifications_enabled}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, notifications_enabled: checked }))
                    }
                  />
                </div>
                <div className="flex items-center justify-between rounded-md border px-3 py-2">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Receive love notes</p>
                    <p className="text-xs text-muted-foreground">
                      When disabled, other students cannot send love notes to this user.
                    </p>
                  </div>
                  <Switch
                    checked={createForm.love_notes_receive_enabled}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, love_notes_receive_enabled: checked }))
                    }
                  />
                </div>
                <div className="flex items-center justify-between rounded-md border px-3 py-2">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Send love notes</p>
                    <p className="text-xs text-muted-foreground">
                      Disable to prevent the account from sending love notes.
                    </p>
                  </div>
                  <Switch
                    checked={createForm.love_notes_send_enabled}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, love_notes_send_enabled: checked }))
                    }
                  />
                </div>
              </div>
            </div>
          </ScrollArea>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={resetCreateDialog} disabled={creating}>
              Cancel
            </Button>
            <Button onClick={handleCreateUser} disabled={creating}>
              {creating ? 'Creating...' : 'Create account'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};
