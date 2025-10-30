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
import { Search, ShieldCheck, ShieldX, UsersRound, Mail, User, UserCircle2, Plus } from 'lucide-react';

interface AdminUserProfile {
  id: string;
  Regno?: string | null;
  Name?: string | null;
  email?: string | null;
  username?: string | null;
  which_class?: string | null;
  gender?: string | null;
  user_role?: string | null;
  isMatchmaking?: boolean;
  isNotifications?: boolean;
  isLovenotesRecieve?: boolean;
  isLovenotesSend?: boolean;
  reported_count?: number;
  last_login_time?: string | null;
  last_login_ip?: string | null;
  is_blocked?: boolean;
  bio?: string | null;
  emoji?: string | null;
  profile_picture_id?: string | null;
  interests?: string[] | null;
  last_matchmaking_time?: string | null;
}

type CreateUserForm = {
  Regno: string;
  Name: string;
  email: string;
  which_class: string;
  gender: string;
  username: string;
  emoji: string;
  bio: string;
  profile_picture_id: string;
  isMatchmaking: boolean;
  isNotifications: boolean;
  isLovenotesRecieve: boolean;
  isLovenotesSend: boolean;
  user_role: 'user' | 'admin';
};

const formatDate = (value?: string | null) => {
  if (!value) return 'Never';
  const date = new Date(value);
  return date.toLocaleString();
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
    Regno: '',
    Name: '',
    email: '',
    which_class: '',
    gender: '',
    username: '',
    emoji: '',
    bio: '',
    profile_picture_id: '',
    isMatchmaking: true,
    isNotifications: true,
    isLovenotesRecieve: true,
    isLovenotesSend: false,
    user_role: 'user',
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
        profile.Name,
        profile.Regno,
        profile.email,
        profile.username,
        profile.which_class,
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
      Name: profile.Name ?? '',
      Regno: profile.Regno ?? '',
      email: profile.email ?? '',
      username: profile.username ?? '',
      which_class: profile.which_class ?? '',
      gender: profile.gender ?? '',
      bio: profile.bio ?? '',
      emoji: profile.emoji ?? '',
      profile_picture_id: profile.profile_picture_id ?? '',
      isMatchmaking: profile.isMatchmaking ?? false,
      isNotifications: profile.isNotifications ?? false,
      isLovenotesRecieve: profile.isLovenotesRecieve ?? true,
      isLovenotesSend: profile.isLovenotesSend ?? false,
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
      'Name',
      'Regno',
      'email',
      'username',
      'which_class',
      'gender',
      'bio',
      'emoji',
      'profile_picture_id',
    ];
    const booleanFields: Array<keyof AdminUserProfile> = [
      'isMatchmaking',
      'isNotifications',
      'isLovenotesRecieve',
      'isLovenotesSend',
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
      ['Name', createForm.Name],
      ['Regno', createForm.Regno],
      ['email', createForm.email],
      ['which_class', createForm.which_class],
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
      Regno: createForm.Regno.trim(),
      Name: createForm.Name.trim(),
      email: createForm.email.trim(),
      which_class: createForm.which_class.trim(),
      gender: createForm.gender.trim(),
      user_role: createForm.user_role,
      isMatchmaking: createForm.isMatchmaking,
      isNotifications: createForm.isNotifications,
      isLovenotesRecieve: createForm.isLovenotesRecieve,
      isLovenotesSend: createForm.isLovenotesSend,
    };

    if (createForm.username.trim()) {
      payload.username = createForm.username.trim();
    }
    if (createForm.emoji.trim()) {
      payload.emoji = createForm.emoji.trim();
    }
    if (createForm.bio.trim()) {
      payload.bio = createForm.bio.trim();
    }
    if (createForm.profile_picture_id.trim()) {
      payload.profile_picture_id = createForm.profile_picture_id.trim();
    }
    if (sanitizedInterests.length > 0) {
      payload.interests = sanitizedInterests;
    }

    try {
      setCreating(true);
      const createdProfile = await createAdminUser(payload);
      setProfiles((prev) => [createdProfile, ...prev]);
      toast.success(
        `${createForm.user_role === 'admin' ? 'Administrator' : 'User'} account created successfully.`
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
                            aria-label={`View profile for ${profile.Name ?? 'user'}`}
                          >
                            <Avatar className="h-12 w-12">
                              <AvatarFallback>{makeInitials(profile.Name)}</AvatarFallback>
                            </Avatar>
                            <div className="grid gap-1 text-sm">
                              <div className="flex flex-wrap items-center gap-2 text-base font-semibold">
                                <span>{profile.Name ?? 'Unknown user'}</span>
                                {profile.user_role && (
                                  <Badge variant={profile.user_role === 'admin' ? 'default' : 'secondary'}>
                                    {profile.user_role}
                                  </Badge>
                                )}
                                {blocked && <Badge variant="destructive">Blocked</Badge>}
                              </div>
                              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                                {profile.Regno && (
                                  <span className="flex items-center gap-1">
                                    <User className="h-3 w-3" />
                                    {profile.Regno}
                                  </span>
                                )}
                                {profile.username && (
                                  <span className="flex items-center gap-1">
                                    <UserCircle2 className="h-3 w-3" />
                                    @{profile.username}
                                  </span>
                                )}
                                {profile.which_class && (
                                  <span className="flex items-center gap-1">
                                    <Badge variant="outline">{profile.which_class}</Badge>
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
                                <span>Reports: {profile.reported_count ?? 0}</span>
                                <span>Last login: {formatDate(profile.last_login_time)}</span>
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
                            Matchmaking: {profile.isMatchmaking ? 'Enabled' : 'Disabled'} · Notifications:{' '}
                            {profile.isNotifications ? 'On' : 'Off'} · Love notes receive:{' '}
                            {profile.isLovenotesRecieve ? 'On' : 'Off'}
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
                      value={editForm.Name ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, Name: event.target.value } : prev))
                      }
                      placeholder="Enter full name"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-username">Username</Label>
                    <Input
                      id="edit-username"
                      value={editForm.username ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, username: event.target.value } : prev))
                      }
                      placeholder="Preferred username"
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
                      value={editForm.Regno ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, Regno: event.target.value } : prev))
                      }
                      placeholder="Enter register number"
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-class">Class / department</Label>
                    <Input
                      id="edit-class"
                      value={editForm.which_class ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) => (prev ? { ...prev, which_class: event.target.value } : prev))
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
                      value={editForm.profile_picture_id ?? ''}
                      onChange={(event) =>
                        setEditForm((prev) =>
                          prev ? { ...prev, profile_picture_id: event.target.value } : prev
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
                      checked={Boolean(editForm.isMatchmaking)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, isMatchmaking: checked } : prev))
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
                      checked={Boolean(editForm.isNotifications)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, isNotifications: checked } : prev))
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
                      checked={Boolean(editForm.isLovenotesRecieve)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, isLovenotesRecieve: checked } : prev))
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
                      checked={Boolean(editForm.isLovenotesSend)}
                      onCheckedChange={(checked) =>
                        setEditForm((prev) => (prev ? { ...prev, isLovenotesSend: checked } : prev))
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
                    value={createForm.Name}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, Name: event.target.value }))
                    }
                    placeholder="Enter full name"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-regno">Register number *</Label>
                  <Input
                    id="create-regno"
                    value={createForm.Regno}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, Regno: event.target.value }))
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
                    value={createForm.which_class}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, which_class: event.target.value }))
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
                  <Label htmlFor="create-username">Username</Label>
                  <Input
                    id="create-username"
                    value={createForm.username}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, username: event.target.value }))
                    }
                    placeholder="Preferred username (optional)"
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
                  <Label htmlFor="create-avatar">Profile picture ID</Label>
                  <Input
                    id="create-avatar"
                    value={createForm.profile_picture_id}
                    onChange={(event) =>
                      setCreateForm((prev) => ({ ...prev, profile_picture_id: event.target.value }))
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
                <Label>Account role *</Label>
                <Select
                  value={createForm.user_role}
                  onValueChange={(value) =>
                    setCreateForm((prev) => ({ ...prev, user_role: value as 'user' | 'admin' }))
                  }
                >
                  <SelectTrigger id="create-role">
                    <SelectValue placeholder="Choose role" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="user">User</SelectItem>
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
                    checked={createForm.isMatchmaking}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, isMatchmaking: checked }))
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
                    checked={createForm.isNotifications}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, isNotifications: checked }))
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
                    checked={createForm.isLovenotesRecieve}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, isLovenotesRecieve: checked }))
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
                    checked={createForm.isLovenotesSend}
                    onCheckedChange={(checked) =>
                      setCreateForm((prev) => ({ ...prev, isLovenotesSend: checked }))
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
