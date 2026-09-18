import { useEffect, useMemo, useState } from 'react';
import { AdminNavigation } from '@/components/AdminNavigation';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { toast } from 'sonner';
import { createAdminUser, getAdminUsers, setAdminUserBlocked, updateAdminUser, uploadAdminUserPicture } from '@/services/api';
import type { AdminUserCreatePayload } from '@/services/api';
import {
  Crown,
  Mail,
  Plus,
  Search,
  ShieldAlert,
  ShieldCheck,
  ShieldHalf,
  ShieldX,
  User,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { formatDateTimeDDMMYYYY } from '@/lib/utils';
import { AccountType } from '@/lib/capabilities';
import {
  cohortLabel,
  INTEREST_VALUES,
  isUserGender,
  isUserYear,
  toInterests,
  USER_GENDER_OPTIONS,
  USER_YEAR_OPTIONS,
} from '@/lib/accountFields';
import type { Interest, UserGender, UserYear } from '@/lib/accountFields';

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

/**
 * How many upheld removals a participant has to accumulate before they are worth an operator's
 * attention, which is what separates the two participant sections on this screen.
 *
 * Summing across the three kinds is deliberate and is the whole point of the threshold: a
 * participant can be a handful of times over the line on one kind of content, and a person
 * removed for three confessions and two love notes is a different picture from someone who was
 * reported twenty times and cleared every one of them. Only `removals` counts towards it.
 * `reports` measures how often people complained, which a dismissed complaint inflates just as
 * much as an upheld one, so a participant who was reported six times and cleared six times stays
 * in the ordinary section.
 *
 * Strictly more than this number, so the boundary case (exactly 5) reads as ordinary. Exported
 * because a threshold that lives in two places would be a threshold that eventually disagrees
 * with itself.
 */
export const FLAGGED_REMOVAL_THRESHOLD = 5;

/**
 * The id of the section holding participants above that threshold.
 *
 * Named rather than written as an index because the header line reads its count from here: a
 * section inserted between the flagged group and the participants would silently change what
 * `sections[2]` means, and the header would report the wrong number.
 */
const FLAGGED_SECTION_ID = 'flagged';

interface AdminUserProfile {
  id: string;
  regno?: string | null;
  name?: string | null;
  email?: string | null;
  /**
   * Which year of the programme this participant is in, and which department they are in. Two
   * fields where there was one `class_name`, because matchmaking groups by them independently and
   * a single free-text column could be grouped by neither.
   *
   * `year` is a number: the server stores an integer bounded by its own limits, and one
   * deployment's "2nd Year" is another's "Class 2", so the wire format is the number they share.
   *
   * Both are absent on a staff row: there is no year or department on `admin` or `moderator`, and
   * the server omits the keys rather than sending null for a value the row does not have.
   */
  year?: number | null;
  department?: string | null;
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
  /**
   * One of `USER_YEAR_OPTIONS`' values, or `''` for "not chosen yet".
   *
   * The empty string rather than a number, because a `<select>` has no "nothing selected" value of
   * its own and this is the state the operator is in when the dialog opens. It is a union with the
   * vocabulary rather than a cast of `''` to it: the old cast made an empty string type-check as a
   * year, so a form submitted before anybody picked one carried a value the compiler believed was
   * valid. The required-field check is what actually stops that, and it reads `''` as missing.
   */
  year: UserYear | '';
  /** Free text. The departments that exist belong to the institution, so there is no list. */
  department: string;
  /** One of `USER_GENDER_OPTIONS`' values. The server accepts nothing else. */
  gender: UserGender;
  emoji: string;
  bio: string;
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

/**
 * The twelve interests a participant may hold, as a checkbox set.
 *
 * A checkbox grid rather than a comma-separated box because these are a closed list: the old
 * text field let somebody type "music", "Music" or "musics" and store all three as different
 * interests that no screen could tell apart. Choosing from a list makes that impossible rather
 * than merely discouraged.
 */
const InterestCheckboxes = ({
  value,
  onChange,
  idPrefix,
}: {
  value: Interest[];
  onChange: (next: Interest[]) => void;
  idPrefix: string;
}) => {
  const toggle = (interest: Interest) => {
    onChange(
      value.includes(interest) ? value.filter((item) => item !== interest) : [...value, interest]
    );
  };

  return (
    <div
      className="grid grid-cols-2 gap-2 rounded-md border p-3 sm:grid-cols-3"
      role="group"
      aria-label="Interests"
    >
      {INTEREST_VALUES.map((interest) => {
        const id = `${idPrefix}-${interest.toLowerCase()}`;
        return (
          <div key={interest} className="flex items-center gap-2">
            <Checkbox
              id={id}
              checked={value.includes(interest)}
              onCheckedChange={() => toggle(interest)}
            />
            <Label htmlFor={id} className="cursor-pointer font-normal">
              {interest}
            </Label>
          </div>
        );
      })}
    </div>
  );
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

/**
 * How a rejected create field is named back to the operator.
 *
 * "Please fill in all required fields" was true and useless: a moderator form asking for a
 * department said the same thing whether the department or the name was the one missing, and there
 * is no department to fill in. Naming the field points at the input that needs attention.
 */
const CREATE_FIELD_LABELS: Partial<Record<keyof CreateUserForm, string>> = {
  name: 'Full name',
  regno: 'Register number',
  email: 'Email',
  year: 'Year',
  department: 'Department',
  gender: 'Gender',
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

/**
 * Whether this row is a staff account.
 *
 * Derived from the table the row came from, the same way the rest of this app distinguishes
 * accounts: there is no role column, so `account_type` is the only thing that says what kind of
 * account a row is. Anything the server did not label as admin or moderator is treated as a
 * participant, because the alternative -- dropping an unrecognised value -- would silently hide
 * an account from the only screen an operator uses to find it.
 */
const isStaffAccount = (profile: AdminUserProfile) =>
  profile.account_type === 'admin' || profile.account_type === 'moderator';

/**
 * Whether this row is an administrator, and so cannot be blocked at all.
 *
 * Narrower than `isStaffAccount` on purpose: moderators are blockable by an admin, so folding
 * them in here would hide a control that works. The server refuses to block an admin with a
 * CHECK constraint on the `admin` table (`ck_admin_not_blockable`), so this is the UI agreeing
 * with the database rather than a second, weaker copy of the rule -- if the two ever disagreed,
 * the database would be the one that held and this button would be the thing that was wrong.
 */
const isAdminAccount = (profile: AdminUserProfile) => profile.account_type === 'admin';

/**
 * How a staff row reads in a sentence.
 *
 * Uses the table name, the same discriminator as `isStaffAccount`, and falls back to "staff" for a
 * value this app does not recognise. Narrowing it with `asAccountType` instead would resolve
 * anything unknown to `users` and call an unknown row a participant, which is the one thing it is
 * not.
 */
const staffNoun = (profile: AdminUserProfile): string =>
  profile.account_type === 'admin' || profile.account_type === 'moderator'
    ? ACCOUNT_TYPE_LABELS[profile.account_type].toLowerCase()
    : 'staff';

/**
 * Every upheld removal against this participant's content, across all three kinds.
 *
 * A confession and a comment on one already share a kind on the server, so summing the three
 * counters counts each piece of taken-down content once. Reads zero for a staff row, which has
 * no reportable content at all -- see `isStaffAccount` for why the tally is omitted rather than
 * sent as zeros.
 */
const totalRemovalsFor = (profile: AdminUserProfile) =>
  REPORT_KIND_ORDER.reduce(
    (sum, kind) => sum + (profile.report_history?.[kind]?.removals ?? 0),
    0,
  );

interface ProfileSection {
  id: string;
  title: string;
  /** What an operator should understand about who belongs here. */
  description: string;
  /** Said instead of a grid when the section is empty, so an empty list still explains itself. */
  emptyNote: string;
  icon: LucideIcon;
  badgeVariant: 'default' | 'secondary' | 'outline' | 'destructive';
  accounts: AdminUserProfile[];
  /**
   * Present only where an operator may create an account of this kind.
   *
   * The button lives in the section it creates into, rather than in one toolbar above all four,
   * because "new account" was the wrong control to begin with: one button opened one form that
   * asked for a participant's class and a moderator's regno in the same breath, and the operator
   * had to remember which of the three kinds they were making. Sitting next to the group it
   * belongs to, the button and the form it opens say the same thing twice.
   *
   * The Admins group has none. Only this screen creates accounts, so an admin cannot be made
   * from it at all -- say so in the section description rather than leaving the absence to be
   * discovered.
   */
  create?: { label: string; accountType: AccountType };
}

/**
 * One account as a card.
 *
 * The grid is the point: a page of full-width rows makes an operator scroll a long way to compare
 * two people, and the comparison is what this screen exists for. A card carries the same facts as
 * the row it replaces, laid out to be read as one unit.
 *
 * The upper block opens the editor and is the only part that is clickable; the block button sits
 * below it in the card footer so pressing it cannot also open the dialog. Keyboard activation is
 * handled explicitly rather than left to a native button, because the clickable area is a `div`
 * holding an `Avatar`, a tally and a button-adjacent footer.
 */
const ProfileCard = ({
  profile,
  blocked,
  busy,
  flagged,
  onOpen,
  onToggleBlock,
}: {
  profile: AdminUserProfile;
  blocked: boolean;
  busy: boolean;
  /** Above `FLAGGED_REMOVAL_THRESHOLD` upheld removals, which is what put them in that section. */
  flagged: boolean;
  onOpen: (profile: AdminUserProfile) => void;
  onToggleBlock: (profile: AdminUserProfile) => void;
}) => {
  // Staff accounts have no reportable content, so the tally is absent on their rows rather than
  // present-and-zero. Three rows of zeros would read as a clean record rather than as a field
  // that does not apply to them.
  const isParticipant = !isStaffAccount(profile);
  const totalRemovals = totalRemovalsFor(profile);

  return (
    <Card className="flex flex-col overflow-hidden transition hover:border-primary/40 hover:shadow-md">
      <div
        role="button"
        tabIndex={0}
        onClick={() => onOpen(profile)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onOpen(profile);
          }
        }}
        aria-label={`View profile for ${profile.name ?? 'user'}`}
        className="flex cursor-pointer flex-col gap-3 p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
      >
        <div className="flex items-start gap-3">
          <Avatar className="h-11 w-11 shrink-0">
            <AvatarFallback>{makeInitials(profile.name)}</AvatarFallback>
          </Avatar>
          <div className="grid min-w-0 flex-1 gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-sm font-semibold">{profile.name ?? 'Unknown user'}</span>
              {blocked && <Badge variant="destructive">Blocked</Badge>}
              {/* The section this card is in already says the count is above the line; this
                  says the same thing on the card itself, so a card still reads correctly once
                  it has been scrolled away from its heading. */}
              {flagged && <Badge variant="outline">Repeated removals</Badge>}
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {profile.regno && (
                <span className="flex items-center gap-1">
                  <User className="h-3 w-3 shrink-0" />
                  {profile.regno}
                </span>
              )}
              {cohortLabel(profile.year, profile.department) && (
                <Badge variant="outline">
                  {cohortLabel(profile.year, profile.department)}
                </Badge>
              )}
            </div>
            {profile.email && (
              <span className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
                <Mail className="h-3 w-3 shrink-0" />
                <span className="truncate">{profile.email}</span>
              </span>
            )}
          </div>
        </div>

        {isParticipant && (
          <div className="space-y-1 rounded-md border border-border/60 bg-muted/20 p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium text-foreground">Report history</span>
              {totalRemovals > 0 && (
                <Badge variant="destructive">{totalRemovals} removed</Badge>
              )}
            </div>
            {REPORT_KIND_ORDER.map((kind) => {
              const tally = profile.report_history?.[kind];
              return (
                <div key={kind} className="flex items-center justify-between gap-2 text-xs">
                  <span className="text-muted-foreground">{REPORT_KIND_LABELS[kind]}</span>
                  <span className="font-mono tabular-nums">
                    {/* Removals first and in the foreground: it is the number a decision should
                        be based on, and `reports` beside it is the number of complaints that
                        survived scrutiny. Each figure is its own element so the pair reads as two
                        facts rather than one string. */}
                    <span
                      className={
                        (tally?.removals ?? 0) > 0 ? 'font-semibold text-destructive' : undefined
                      }
                    >
                      {tally?.removals ?? 0}
                    </span>
                    <span className="text-muted-foreground">removed</span>
                    <span aria-hidden="true" className="px-1 text-muted-foreground">
                      /
                    </span>
                    <span className="text-muted-foreground">{tally?.reports ?? 0} reported</span>
                  </span>
                </div>
              );
            })}
            <p className="text-[11px] leading-snug text-muted-foreground">
              Removals are the authoritative count: reports includes complaints a moderator
              dismissed as unfounded.
            </p>
          </div>
        )}

        <div className="grid gap-0.5 text-[11px] text-muted-foreground">
          <span>Last login: {formatDate(profile.last_login_at)}</span>
          {profile.last_login_ip && <span>Last IP: {profile.last_login_ip}</span>}
        </div>
      </div>

      <div className="mt-auto flex flex-col gap-2 border-t border-border p-3">
        {/* An unblocked admin gets no block button rather than a disabled one. A permanently
            greyed-out control with no explanation reads as a bug or a permissions problem, and
            the reason is not obvious from the row; saying so is more honest than showing a
            dead button. An admin that is somehow already blocked still gets the button, because
            unblocking is permitted and may be the thing that has to be done. */}
        {blocked || !isAdminAccount(profile) ? (
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            className={
              blocked
                ? 'border-green-500 text-green-500 hover:bg-green-500 hover:text-white'
                : 'border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground'
            }
            onClick={() => onToggleBlock(profile)}
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
        ) : (
          <p className="text-[11px] text-muted-foreground">
            Admins cannot be blocked, so that another admin always retains access.
          </p>
        )}
        <div className="text-[11px] text-muted-foreground">
          Matchmaking: {profile.matchmaking_enabled ? 'Enabled' : 'Disabled'} · Notifications:{' '}
          {profile.notifications_enabled ? 'On' : 'Off'} · Love notes receive:{' '}
          {profile.love_notes_receive_enabled ? 'On' : 'Off'}
        </div>
      </div>
    </Card>
  );
};

export const ProfileReview = () => {
  const buildDefaultCreateForm = (accountType: AccountType): CreateUserForm => ({
    regno: '',
    name: '',
    email: '',
    // Empty rather than the first year option. `year` is a NOT NULL column held to a closed
    // vocabulary by a CHECK constraint, and the select has no blank option, so an empty value
    // here could only ever be a form the operator had not finished -- which the required-field
    // check then reports by name. Preselecting 1 would submit a claim about this
    // participant that nobody made, so the select stays empty until one is actually picked.
    //
    // The cast is gone: the field is a union of the vocabulary and `''`, so an unselected year is
    // representable without pretending to be a year. `department` beside it is `''` for the same
    // reason and needed no cast either.
    year: '',
    department: '',
    // `other` rather than an empty string. Gender is a non-null column on all three account
    // tables and the select has no blank option, so an empty value here could only ever be a
    // form the operator had not finished -- which the required-field check then reports by name
    // instead of the select silently showing nothing.
    gender: 'other',
    emoji: '',
    bio: '',
    matchmaking_enabled: true,
    notifications_enabled: true,
    love_notes_receive_enabled: true,
    love_notes_send_enabled: false,
    account_type: accountType,
  });

  const [profiles, setProfiles] = useState<AdminUserProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  const [blocking, setBlocking] = useState<string | null>(null);
  /**
   * The block/unblock awaiting confirmation, or null when no dialog is open.
   *
   * Held as the profile *and* the state being moved to, rather than just the profile, so the
   * dialog and the request cannot disagree: the action is decided once, at the point the button
   * was pressed, and everything afterwards describes that decision. Deriving `!profile.is_blocked`
   * again at confirm time would read a list that may have been refetched in between.
   */
  const [pendingBlock, setPendingBlock] = useState<{
    profile: AdminUserProfile;
    nextState: boolean;
  } | null>(null);
  const [selectedProfile, setSelectedProfile] = useState<AdminUserProfile | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState<Partial<AdminUserProfile> | null>(null);
  /**
   * The ticked interests, held separately from `editForm`.
   *
   * Separate because it is a set rather than a string: `editForm` is the shape the server
   * returned, and threading `interests` through it as a comma-joined string would mean
   * re-parsing a value the operator did not type. It is also what the save compares against the
   * stored list to decide whether interests actually changed.
   */
  const [editInterests, setEditInterests] = useState<Interest[]>([]);
  /**
   * The file chosen in the editor, or null. Never a URL: the picture is uploaded to the
   * server, which puts it in object storage and writes back the address it generated.
   */
  const [editPictureFile, setEditPictureFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateUserForm>(() => buildDefaultCreateForm('users'));
  const [createInterests, setCreateInterests] = useState<Interest[]>([]);
  const [createPictureFile, setCreatePictureFile] = useState<File | null>(null);
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
        // Both halves, so searching "CSE" and searching 2 both find the participant. A
        // single joined string would make the second of those depend on the order the server
        // happened to render the two halves in.
        profile.department,
        profile.year,
      ]
        .filter(Boolean)
        .join(' ') // join to single string
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [profiles, searchTerm]);

  /**
   * The four groups this screen shows, in the order an operator should meet them.
   *
   * The order is a reading order rather than an alphabetical accident. Admins first because a
   * staff account with the wrong privileges is the most consequential mistake on the page, and
   * moderators next because they are the accounts that actually do the reviewing. Participants
   * come last, split by the one thing that separates a routine account from one that needs a
   * decision: how much of their content has actually been taken down.
   *
   * Every row lands in exactly one section. The two staff sections name a table, so they cannot
   * overlap; the two participant sections are complements of the same predicate, so a participant
   * is in one or the other and never both.
   *
   * Within the flagged section, most removals first: that is the order in which a moderator
   * should work through them. The other sections keep the server's ordering, which is by name.
   */
  const sections = useMemo<ProfileSection[]>(() => {
    const admins = filteredProfiles.filter((profile) => profile.account_type === 'admin');
    const moderators = filteredProfiles.filter((profile) => profile.account_type === 'moderator');
    const participants = filteredProfiles.filter((profile) => !isStaffAccount(profile));
    const flagged = participants
      .filter((profile) => totalRemovalsFor(profile) > FLAGGED_REMOVAL_THRESHOLD)
      .sort((a, b) => totalRemovalsFor(b) - totalRemovalsFor(a));
    const ordinary = participants.filter(
      (profile) => totalRemovalsFor(profile) <= FLAGGED_REMOVAL_THRESHOLD,
    );

    return [
      {
        id: 'admins',
        title: 'Admins',
        description:
          'Accounts holding the full set of capabilities. Listed first, because a staff account with the wrong privileges is the most consequential mistake on this page. Admin accounts cannot be created here; this screen is the only place that creates them.',
        emptyNote: 'No admin accounts exist yet.',
        icon: Crown,
        badgeVariant: 'default',
        accounts: admins,
      },
      {
        id: 'moderators',
        title: 'Moderators',
        description:
          'Accounts that review and rule on reports. They reach fewer moderation screens than an admin, and they hold no participant content of their own, so a moderator needs no year, department, bio or interests.',
        emptyNote: 'No moderator accounts exist yet.',
        icon: ShieldHalf,
        badgeVariant: 'secondary',
        accounts: moderators,
        create: { label: 'Create moderator', accountType: 'moderator' },
      },
      {
        id: FLAGGED_SECTION_ID,
        title: `Participants with more than ${FLAGGED_REMOVAL_THRESHOLD} removals`,
        description: `Participants whose content has been taken down more than ${FLAGGED_REMOVAL_THRESHOLD} times in total, counting confessions and their comments together, love notes and chat messages. Complaints that were dismissed do not count towards this.`,
        emptyNote: `No participant is above ${FLAGGED_REMOVAL_THRESHOLD} upheld removals.`,
        icon: ShieldAlert,
        badgeVariant: 'destructive',
        accounts: flagged,
      },
      {
        id: 'participants',
        title: 'Participants',
        description:
          'Everyone else on the platform, at or below the removal threshold. Blocked accounts stay here unless removals put them in the section above.',
        emptyNote: 'No participants match.',
        icon: Users,
        badgeVariant: 'outline',
        accounts: ordinary,
        create: { label: 'Create participant', accountType: 'users' },
      },
    ];
  }, [filteredProfiles]);

  const flaggedCount =
    sections.find((section) => section.id === FLAGGED_SECTION_ID)?.accounts.length ?? 0;

  /**
   * Whether the create dialog is currently making a participant.
   *
   * The form is driven off the account type it was opened with rather than off a control inside
   * it. A staff row has no year, department, bio, interests, matchmaking or love-note columns, so
   * offering those inputs would be asking for values the server silently discards -- and the
   * operator has no way to see that they were discarded. The dialog has one job per open, stated
   * in its title.
   */
  const createIsParticipant = createForm.account_type === 'users';

  /**
   * Whether the account open in the editor is a participant.
   *
   * The same split the create form makes, and for the same reason. A moderator and an admin have
   * no year, department, bio, interests, matchmaking or love-note columns, so the editor used to
   * offer those inputs and the server silently dropped whatever came out of them: the operator
   * moved a toggle, pressed save, was told it worked, and the change was gone on the next fetch.
   * Not offering a field that cannot be saved is the whole fix.
   */
  const editingIsParticipant = selectedProfile !== null && !isStaffAccount(selectedProfile);

  /**
   * Opening the confirmation is deliberately separate from performing the action.
   *
   * Blocking is not a cosmetic toggle: it revokes the account's existing session immediately --
   * the backend rejects a blocked account's bearer token on every request, so there is no grace
   * period and no undo from the other end. Unblocking is likewise immediate and hands the
   * person their account back. Both were one click away on a button that sits in a list of
   * profiles being clicked through quickly, which is exactly the context in which a mis-click
   * locks somebody out of the platform.
   *
   * So `toggleBlock` only opens the dialog and the request is made in `confirmBlock`. The
   * account is held in state rather than recomputed, so the dialog describes the action that
   * was actually asked for even if the list is refetched underneath it.
   */
  const toggleBlock = (profile: AdminUserProfile) => {
    // An admin cannot be blocked -- the database refuses it outright (`ck_admin_not_blockable`),
    // so there is nothing to confirm and nothing this dialog could usefully ask. The button is
    // also not rendered for an admin; this is the belt to that braces, so a stale row or a
    // keyboard-reachable remnant cannot open a dialog whose only outcome is a 400.
    if (!profile.id || (profile.is_blocked === false && isAdminAccount(profile))) return;
    setPendingBlock({ profile, nextState: !profile.is_blocked });
  };

  const confirmBlock = async () => {
    if (!pendingBlock) return;
    const { profile, nextState } = pendingBlock;
    if (!profile.id) {
      setPendingBlock(null);
      return;
    }

    try {
      setBlocking(profile.id);
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
      setPendingBlock(null);
    }
  };

  const openProfileEditor = (profile: AdminUserProfile) => {
    const isParticipant = !isStaffAccount(profile);
    setSelectedProfile(profile);
    setEditForm({
      name: profile.name ?? '',
      regno: profile.regno ?? '',
      email: profile.email ?? '',
      // A stored value from before the gender vocabulary was closed will not match any option,
      // and a select with no matching option displays the wrong thing as though it were chosen.
      // Falling back keeps the dialog honest about what is stored instead of showing a blank
      // control the operator would have to guess about.
      gender: isUserGender(profile.gender ?? '') ? (profile.gender as UserGender) : 'other',
      notifications_enabled: Boolean(profile.notifications_enabled),
      // Seeded only for a participant. A staff row carries none of these, so putting defaults for
      // them into the form would put values there that no visible field could ever be compared
      // against -- and a toggle seeded to `false` on a moderator is a control that looks like it
      // says something about matchmaking when it says nothing at all.
      ...(isParticipant
        ? {
            // Same normalisation as gender below: a stored year from before the range was
            // closed matches no option, and a select with no matching option displays the wrong
            // thing as though it were chosen. `null` rather than `''` because the field is a
            // number here, and null is a value it can actually hold.
            year: isUserYear(profile.year) ? profile.year : null,
            department: profile.department ?? '',
            bio: profile.bio ?? '',
            emoji: profile.emoji ?? '',
            // Read every toggle the way the change detector below reads it. Seeding one any other
            // way makes an untouched field register as a change on save: a participant whose
            // payload omits `love_notes_receive_enabled` would be shown the toggle on, then have
            // it quietly written off the moment the operator changed something else.
            matchmaking_enabled: Boolean(profile.matchmaking_enabled),
            love_notes_receive_enabled: Boolean(profile.love_notes_receive_enabled),
            love_notes_send_enabled: Boolean(profile.love_notes_send_enabled),
          }
        : {}),
    });
    // Seeded from the stored list, filtered to the twelve the server accepts. A legacy value
    // that is no longer in the vocabulary is dropped rather than shown as an unticked box the
    // operator would have to recognise, and it stops appearing the next time they save.
    setEditInterests(isParticipant ? toInterests(profile.interests) : []);
    // Deliberately null on open. Re-opening the editor must not silently re-upload the file
    // chosen last time; a picture is only sent when the operator picks one in this session.
    setEditPictureFile(null);
    setEditOpen(true);
  };

  const resetEditor = () => {
    setEditOpen(false);
    setSelectedProfile(null);
    setEditForm(null);
    setEditInterests([]);
    setEditPictureFile(null);
    setSaving(false);
  };

  const handleSaveProfile = async () => {
    if (!selectedProfile || !editForm) {
      return;
    }

    const isParticipant = !isStaffAccount(selectedProfile);
    const payload: Record<string, unknown> = {};

    // Only the fields the target table actually has. The server drops the participant-only keys
    // from a staff update, so sending them would either be ignored (the operator's change
    // silently vanishes on the next fetch) or, worse, be sent with a value the form invented.
    const textFields: Array<keyof AdminUserProfile> = [
      'name',
      'regno',
      'email',
      ...(isParticipant ? (['department', 'bio', 'emoji'] as const) : []),
    ];
    const booleanFields: Array<keyof AdminUserProfile> = [
      'notifications_enabled',
      ...(isParticipant
        ? ([
            'matchmaking_enabled',
            'love_notes_receive_enabled',
            'love_notes_send_enabled',
          ] as const)
        : []),
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

    // Gender gets its own comparison because of how it is seeded. `openProfileEditor` puts a
    // stored value that is not in the vocabulary into the form as `other`, so the form value is
    // never blank and a naive comparison against the raw stored value would report a change on
    // every save of an account that holds one -- rewriting a field the operator never touched,
    // which is how a legacy `female` quietly became `other` on the first unrelated edit.
    //
    // Comparing against the same normalisation that seeded it means a gender is sent only when
    // the operator actually picked a different one.
    const genderBefore = isUserGender(selectedProfile.gender ?? '')
      ? selectedProfile.gender
      : 'other';
    if (editForm.gender !== genderBefore) {
      payload.gender = editForm.gender;
    }

    // Year joins gender in needing its own comparison, for the same reason: `openProfileEditor`
    // puts a stored year outside the range into the form as null, so the form value is never the
    // raw stored value and a naive comparison would report a change on every save of such an
    // account -- rewriting a field the operator never touched.
    if (isParticipant) {
      const yearBefore = isUserYear(selectedProfile.year) ? selectedProfile.year : null;
      if ((editForm.year ?? null) !== yearBefore) {
        payload.year = editForm.year;
      }
    }

    // Interests are compared as a whole set rather than field by field, and compared against
    // the vocabulary-filtered stored list -- the same list the boxes were seeded from. Comparing
    // against the raw stored array would report a difference whenever a legacy value had been
    // filtered out on open, and the save would then "change" interests the operator never touched.
    if (isParticipant) {
      const currentInterests = toInterests(selectedProfile.interests);
      const changed =
        editInterests.length !== currentInterests.length ||
        editInterests.some((item) => !currentInterests.includes(item));
      // An emptied list is sent as `[]` rather than being skipped: unticking the last box is a
      // deliberate request to clear them, and omitting the key would mean "leave alone".
      if (changed) {
        payload.interests = editInterests;
      }
    }

    // A chosen picture is not part of the JSON payload at all. It is a second request, sent
    // after the field updates, because the endpoint takes multipart bytes and the other takes
    // JSON. Both are needed before the dialog can report success.
    const hasPicture = editPictureFile !== null;

    if (Object.keys(payload).length === 0 && !hasPicture) {
      toast.info('No changes to save.');
      return;
    }

    try {
      setSaving(true);
      // The update runs first and the upload second. The reverse order would mean a rejected
      // name change left a new picture on an account whose other edits were refused, which is
      // harder to notice and harder to undo than the other way round.
      const updatedProfile =
        Object.keys(payload).length > 0
          ? await updateAdminUser(selectedProfile.id, payload)
          : selectedProfile;

const pictured = hasPicture
        ? await uploadAdminUserPicture(selectedProfile.id, editPictureFile as File)
        : updatedProfile;

      // Both responses are merged, in order. Spreading `pictured` straight over the stored profile
      // looked right and was wrong: the upload response carries only `profile_picture_url`, so it
      // overrode nothing and the fields the update had just changed reverted to their old values
      // on screen. An operator who renamed someone and added a picture at once saw the old name
      // until the next refresh, from a save that had reported success.
      setProfiles((prev) =>
        prev.map((profile) =>
          profile.id === selectedProfile.id
            ? { ...profile, ...updatedProfile, ...pictured }
            : profile,
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

  const openCreateDialog = (accountType: AccountType) => {
    setCreateForm(buildDefaultCreateForm(accountType));
    setCreateInterests([]);
    setCreatePictureFile(null);
    setCreateOpen(true);
  };

  const resetCreateDialog = () => {
    setCreateOpen(false);
    setCreateForm(buildDefaultCreateForm('users'));
    setCreateInterests([]);
    setCreatePictureFile(null);
    setCreating(false);
  };

  const handleCreateUser = async () => {
    const isParticipant = createForm.account_type === 'users';

    // Required fields are a function of the table being written to, not one list for all three.
    // Name, regno, email and gender are non-null columns on all three account tables, so all four
    // are required of every account type. Email used to be a participant-only requirement --
    // a staff account could be created with a regno and a name alone -- which left a moderator
    // nobody could contact, and no error at the time to notice.
    const requiredFields: Array<[keyof CreateUserForm, string | number]> = [
      ['name', createForm.name],
      ['regno', createForm.regno],
      ['email', createForm.email],
      ['gender', createForm.gender],
    ];
    if (isParticipant) {
      // Both halves of the old `class_name`, and both required: the database holds them as NOT
      // NULL, so a create that omitted either would be a 500 from the insert rather than
      // something the operator could be told about.
      requiredFields.push(['year', createForm.year], ['department', createForm.department]);
    }

    // Not one `.trim()` call across the whole list, because the list is no longer all strings.
    // A whitespace-only name is missing and so is a year the server would refuse, and they are
    // different tests on different types. Year is checked against the vocabulary rather than for
    // emptiness: `''` is the unselected state, but 0 and 13 are not "not chosen", they are wrong,
    // and a truthiness test would wave 0 through to a 422.
    const isMissing = (value: string | number): boolean =>
      typeof value === 'number' ? !isUserYear(value) : !value.trim();

    const missingField = requiredFields.find(([, value]) => isMissing(value));
    if (missingField) {
      toast.error(`${CREATE_FIELD_LABELS[missingField[0]]} is required for this account type.`);
      return;
    }

    const payload: AdminUserCreatePayload = {
      regno: createForm.regno.trim(),
      name: createForm.name.trim(),
      email: createForm.email.trim(),
      gender: createForm.gender,
      account_type: createForm.account_type,
      notifications_enabled: createForm.notifications_enabled,
    };

    // Participant columns. Sent only for a participant: the staff tables have no such columns and
    // the server drops these keys for a staff row, so a form that never offered them must not
    // send empty ones behind the operator's back either.
    if (isParticipant) {
      // Narrowed rather than cast. The required-field check above has already returned unless
      // this is a real year, but the compiler cannot see that through `find`, so the vocabulary is
      // re-checked here instead of asserted. It is the same check, so the two cannot disagree.
      payload.year = isUserYear(createForm.year) ? createForm.year : undefined;
      payload.department = createForm.department.trim();
      payload.matchmaking_enabled = createForm.matchmaking_enabled;
      payload.love_notes_receive_enabled = createForm.love_notes_receive_enabled;
      payload.love_notes_send_enabled = createForm.love_notes_send_enabled;
      if (createForm.emoji.trim()) {
        payload.emoji = createForm.emoji.trim();
      }
      if (createForm.bio.trim()) {
        payload.bio = createForm.bio.trim();
      }
      // Sent even when empty. An admin who ticked nothing has said "no interests", which is a
      // different statement from staying silent, and the column is NOT NULL -- omitting the key
      // would leave the server to guess.
      payload.interests = createInterests;
    }

    try {
      setCreating(true);
      let createdProfile = await createAdminUser(payload);
      let pictureFailed = false;

      // The picture is a second request, after the account exists. It cannot be part of the
      // create body: that is JSON, and this is bytes, and the row has to have an id before
      // there is anything to attach an image to.
      //
      // A failure here does not undo the account, and that is deliberate. Rolling back would
      // mean deleting a row an operator can see was created, to recover from a failed image
      // upload -- so the account is kept, the toast says exactly what happened, and the
      // operator can retry the picture from the editor.
      if (createPictureFile) {
        try {
          createdProfile = await uploadAdminUserPicture(createdProfile.id, createPictureFile);
        } catch (uploadError) {
          console.error('Failed to upload picture for the new account', uploadError);
          pictureFailed = true;
          toast.warning(
            'Account created, but the picture could not be uploaded. Open the profile to retry.',
          );
        }
      }

      setProfiles((prev) => [createdProfile, ...prev]);
      // Confirmed by a flag set inside the upload block, not by `!createPictureFile`: when a file
      // was chosen and the upload succeeded, that condition is false and the create reported
      // nothing at all. The account was there, with the right picture, and the dialog closed in
      // silence.
      if (!pictureFailed) {
        toast.success(
          `${ACCOUNT_TYPE_LABELS[createForm.account_type]} account created successfully.`,
        );
      }
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

      <div className="mx-auto flex w-full max-w-7xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">Profile Review</h1>
          <p className="text-sm text-muted-foreground">
            Every account on the platform, grouped by who it is and by how much of its content has
            been taken down. Click a card to edit an account, or block one to suspend it instantly.
          </p>
          <p className="text-xs text-muted-foreground/80">
            Total users: {profiles.length} · Blocked: {profiles.filter((p) => p.is_blocked).length} ·
            Over {FLAGGED_REMOVAL_THRESHOLD} removals: {flaggedCount}
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
          <Badge variant="secondary" className="w-max gap-2">
            <Users className="h-4 w-4" />
            Showing {filteredProfiles.length} profiles
          </Badge>
        </div>

        {loading ? (
          <Card>
            <CardContent className="flex h-40 items-center justify-center text-sm text-muted-foreground">
              Loading user profiles...
            </CardContent>
          </Card>
        ) : filteredProfiles.length === 0 && searchTerm.trim() ? (
          /* One message for the whole page rather than four empty sections. When a search matches
             nothing, four headings over empty grids is four false impressions that the platform has
             no admins, no moderators and no participants. Reachable only with a search in the box:
             on a genuinely empty platform the sections must still render, or the create buttons
             living in them would be the one thing missing. */
          <Card>
            <CardHeader className="flex flex-col gap-1">
              <CardTitle className="text-lg">No accounts match</CardTitle>
              <CardDescription>
                No account matches this search. Clear the search to see every group again.
              </CardDescription>
            </CardHeader>
          </Card>
        ) : (
          sections.map((section) => {
            const Icon = section.icon;
            const accountCount = section.accounts.length;
            const create = section.create;
            return (
              <section key={section.id} aria-labelledby={`profile-section-${section.id}`}>
                <Card>
                  <CardHeader className="gap-2 pb-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <CardTitle
                        id={`profile-section-${section.id}`}
                        className="flex items-center gap-2 text-lg"
                      >
                        <Icon className="h-4 w-4" />
                        {section.title}
                      </CardTitle>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={section.badgeVariant}>
                          {accountCount} {accountCount === 1 ? 'account' : 'accounts'}
                        </Badge>
                        {/* Sits with the group it creates into, so the control and the form it
                            opens name the same kind of account. */}
                        {create && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => openCreateDialog(create.accountType)}
                          >
                            <Plus className="mr-2 h-4 w-4" />
                            {create.label}
                          </Button>
                        )}
                      </div>
                    </div>
                    <CardDescription>{section.description}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    {accountCount === 0 ? (
                      /* Said in words rather than left blank. An operator who expected a moderator
                         to be on this screen needs to see that the group was checked and is empty,
                         which an empty grid does not tell them. */
                      <p className="rounded-lg border border-dashed border-border py-8 text-center text-sm text-muted-foreground">
                        {section.emptyNote}
                      </p>
                    ) : (
                      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                        {section.accounts.map((profile) => (
                          <ProfileCard
                            key={profile.id}
                            profile={profile}
                            blocked={Boolean(profile.is_blocked)}
                            busy={blocking === profile.id}
                            flagged={section.id === FLAGGED_SECTION_ID}
                            onOpen={openProfileEditor}
                            onToggleBlock={toggleBlock}
                          />
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              </section>
            );
          })
        )}
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
                  {editingIsParticipant
                    ? "Update the selected user's information. Leave a field untouched to keep the current value."
                    : `A ${staffNoun(selectedProfile)} account has no year, department, bio, interests, matchmaking or love-note settings, so those are not offered here.`}
                </DialogDescription>
              </DialogHeader>

              <ScrollArea className="max-h-[65vh] pr-2">
                <div className="grid gap-6 py-4">
                {/* Shared columns: every account table has these five. */}
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
                    <Label htmlFor="edit-gender">Gender</Label>
                    <Select
                      value={(editForm.gender as UserGender) ?? 'other'}
                      onValueChange={(value) =>
                        setEditForm((prev) =>
                          prev ? { ...prev, gender: value as UserGender } : prev
                        )
                      }
                    >
                      <SelectTrigger id="edit-gender">
                        <SelectValue placeholder="Select a gender" />
                      </SelectTrigger>
                      <SelectContent>
                        {USER_GENDER_OPTIONS.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-avatar">Profile picture</Label>
                    <Input
                      id="edit-avatar"
                      type="file"
                      accept="image/*"
                      onChange={(event) => setEditPictureFile(event.target.files?.[0] ?? null)}
                    />
                    <p className="text-[11px] text-muted-foreground">
                      The file is uploaded to the server and stored there; the address is written
                      by the server. {selectedProfile.profile_picture_url
                        ? 'Saving with a file chosen replaces the current picture.'
                        : 'No picture is set yet.'}
                    </p>
                  </div>
                </div>

                {/* Participant columns. */}
                {editingIsParticipant && (
                  <>
                    {/* Year is a select and department is a text box, not two text boxes and not
                        one joined field. Year is held to a closed vocabulary on the server, so a
                        free-text box could only ever produce a value it refuses; department has no
                        such list, because the departments that exist belong to the institution
                        rather than to this codebase. */}
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="grid gap-2">
                        <Label htmlFor="edit-year">Year</Label>
                        <Select
                          value={
                            editForm.year == null
                              ? ''
                              : String(editForm.year)
                          }
                          onValueChange={(value) =>
                            setEditForm((prev) =>
                              prev ? { ...prev, year: Number(value) } : prev
                            )
                          }
                        >
                          <SelectTrigger id="edit-year">
                            <SelectValue placeholder="Select a year" />
                          </SelectTrigger>
                          <SelectContent>
                            {USER_YEAR_OPTIONS.map((option) => (
                              <SelectItem key={option.value} value={String(option.value)}>
                                {option.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="grid gap-2">
                        <Label htmlFor="edit-department">Department/Division</Label>
                        <Input
                          id="edit-department"
                          value={editForm.department ?? ''}
                          onChange={(event) =>
                            setEditForm((prev) =>
                              prev ? { ...prev, department: event.target.value } : prev
                            )
                          }
                          placeholder="CSE, ECE, MECH, etc."
                        />
                      </div>
                    </div>

                    <div className="grid gap-2">
                      <Label htmlFor="edit-emoji">Emoji tag</Label>
                      <Input
                        id="edit-emoji"
                        value={editForm.emoji ?? ''}
                        onChange={(event) =>
                          setEditForm((prev) => (prev ? { ...prev, emoji: event.target.value } : prev))
                        }
                        placeholder="e.g. ??"
                      />
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
                      <Label>Interests</Label>
                      <InterestCheckboxes
                        value={editInterests}
                        onChange={setEditInterests}
                        idPrefix="edit-interest"
                      />
                      <p className="text-xs text-muted-foreground">
                        Interests help matchmaking results. These are the only values the server
                        accepts, and the participant can change them afterwards.
                      </p>
                    </div>
                  </>
                )}

                <div className="grid gap-3 md:grid-cols-2">
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
                  {/* A switch that cannot be written is worse than no switch: it reads as a
                      setting the operator has just set. */}
                  {editingIsParticipant && (
                    <>
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
                    </>
                  )}
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
            <DialogTitle>
              Create {ACCOUNT_TYPE_LABELS[createForm.account_type].toLowerCase()} account
            </DialogTitle>
            <DialogDescription>
              {createIsParticipant
                ? 'Provision a fresh student profile. Required fields are marked and the account is active immediately.'
                : 'Provision a fresh staff profile. A staff account holds no participant content of its own, so only the shared fields are asked for.'}
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
                  <Label htmlFor="create-gender">Gender *</Label>
                  <Select
                    value={createForm.gender}
                    onValueChange={(value) =>
                      setCreateForm((prev) => ({ ...prev, gender: value as UserGender }))
                    }
                  >
                    <SelectTrigger id="create-gender">
                      <SelectValue placeholder="Select a gender" />
                    </SelectTrigger>
                    <SelectContent>
                      {USER_GENDER_OPTIONS.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-2">
                  {/* Required for every account type. It used to be marked optional for staff,
                      which left a moderator with no address anybody could reach. */}
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
                {createIsParticipant && (
                  <>
                    <div className="grid gap-2">
                      <Label htmlFor="create-year">Year *</Label>
                      {/* No default. The server refuses any year outside its range, so the select
                          is the whole set of legal answers and picking one is the only way to
                          produce a valid value. A preselected year would be a claim about this
                          participant that nobody made. */}
                      {/* The empty string is the control's "nothing selected" value, not a
                          stand-in for "leave undefined": Radix renders the placeholder for `''`,
                          and passing `undefined` instead would make the control uncontrolled,
                          which React then warns about the moment the first year is picked. */}
                      <Select
                        value={createForm.year === '' ? '' : String(createForm.year)}
                        onValueChange={(value) =>
                          setCreateForm((prev) => ({ ...prev, year: Number(value) }))
                        }
                      >
                        <SelectTrigger id="create-year">
                          <SelectValue placeholder="Select a year" />
                        </SelectTrigger>
                        <SelectContent>
                          {USER_YEAR_OPTIONS.map((option) => (
                            <SelectItem key={option.value} value={String(option.value)}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="create-department">Department/Division *</Label>
                      <Input
                        id="create-department"
                        value={createForm.department}
                        onChange={(event) =>
                          setCreateForm((prev) => ({ ...prev, department: event.target.value }))
                        }
                        placeholder="CSE, ECE, MECH, etc."
                      />
                    </div>
                  </>
                )}
                {createIsParticipant && (
                  <div className="grid gap-2">
                    <Label htmlFor="create-emoji">Emoji tag</Label>
                    <Input
                      id="create-emoji"
                      value={createForm.emoji}
                      onChange={(event) =>
                        setCreateForm((prev) => ({ ...prev, emoji: event.target.value }))
                      }
                      placeholder="eg. ??"
                    />
                  </div>
                )}
                <div className="grid gap-2">
                  <Label htmlFor="create-avatar">Profile picture</Label>
                  <Input
                    id="create-avatar"
                    type="file"
                    accept="image/*"
                    onChange={(event) => setCreatePictureFile(event.target.files?.[0] ?? null)}
                  />
                  <p className="text-[11px] text-muted-foreground">
                    Optional. The file is uploaded to the server after the account is created, so
                    the address stored is one the server generated.
                  </p>
                </div>
              </div>

              {createIsParticipant && (
                <>
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
                    <Label>Interests</Label>
                    <InterestCheckboxes
                      value={createInterests}
                      onChange={setCreateInterests}
                      idPrefix="create-interest"
                    />
                    <p className="text-xs text-muted-foreground">
                      Interests enrich matchmaking recommendations. Tick any that apply; the
                      participant can change them afterwards.
                    </p>
                  </div>
                </>
              )}

              <div className="grid gap-3 md:grid-cols-2">
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
                {/* Matchmaking and love notes are participant columns. They are not shown as
                    disabled controls for a staff account either: a switch that cannot be turned on
                    is a switch that reads as a setting the operator has not set yet. */}
                {createIsParticipant && (
                  <>
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
                  </>
                )}
              </div>
            </div>
          </ScrollArea>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={resetCreateDialog} disabled={creating}>
              Cancel
            </Button>
            <Button onClick={handleCreateUser} disabled={creating}>
              {creating
                ? 'Creating...'
                : `Create ${ACCOUNT_TYPE_LABELS[createForm.account_type].toLowerCase()} account`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Destructive and hard to reverse, so it is confirmed rather than performed on click.
          Block and unblock are worded separately because they are not two states of one benign
          toggle: one ends somebody's access to the platform immediately and the other restores
          it, and an operator who misread which they were about to do should be told before they
          do it, not after. */}
      <AlertDialog
        open={pendingBlock !== null}
        onOpenChange={(open) => {
          // Only a dismissal closes it. While the request is in flight the dialog stays put and
          // its action is disabled, so a second click cannot fire the same request twice.
          if (!open && blocking === null) setPendingBlock(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pendingBlock?.nextState ? 'Block this account?' : 'Unblock this account?'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingBlock?.nextState ? (
                <>
                  <strong className="text-foreground">
                    {pendingBlock.profile.name || pendingBlock.profile.regno}
                  </strong>{' '}
                  will be signed out immediately and refused sign-in, and will be excluded from
                  matchmaking and messaging. Any session they already hold stops working at once
                  &mdash; there is no grace period.
                </>
              ) : (
                <>
                  <strong className="text-foreground">
                    {pendingBlock?.profile.name || pendingBlock?.profile.regno}
                  </strong>{' '}
                  will be able to sign in and message again immediately, and will reappear in
                  matchmaking.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={blocking !== null}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmBlock}
              disabled={blocking !== null}
              className={
                pendingBlock?.nextState
                  ? 'bg-destructive hover:bg-destructive/90'
                  : 'bg-green-600 hover:bg-green-600/90'
              }
            >
              {pendingBlock?.nextState ? 'Block account' : 'Unblock account'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
