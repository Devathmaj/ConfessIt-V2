import { Fragment, useEffect, useMemo, useState } from 'react';
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { toast } from 'sonner';
import { createAdminUser, getAdminUsers, setAdminUserBlocked, updateAdminUser } from '@/services/api';
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
import { describeApiError } from '@/lib/apiError';
import {
  firstInvalidField,
  hasFieldIssues,
  validateIdentityFields,
  type FieldIssues,
  type IdentityField,
} from '@/lib/fieldValidation';
import { AccountType } from '@/lib/capabilities';
import {
  cohortLabel,
  isUserGender,
  isUserYear,
  USER_GENDER_OPTIONS,
  USER_YEAR_OPTIONS,
} from '@/lib/accountFields';
import type { UserGender, UserYear } from '@/lib/accountFields';
import { ADMIN_DEPARTMENT_MAX, ADMIN_NAME_MAX, ADMIN_REGNO_MAX } from '@/lib/limits';

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

/**
 * The id of the ordinary participants section, where the filter controls sit directly above it.
 *
 * A named constant for the same reason as `FLAGGED_SECTION_ID`: the render places the controls by
 * comparing against this, and an inline `'participants'` literal in that comparison would be one
 * more place the id has to be kept correct, and one more place a rename would silently leave the
 * controls somewhere else on the page.
 */
const PARTICIPANTS_SECTION_ID = 'participants';

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
  /** One of `USER_GENDER_OPTIONS`' values, or `''` for "not chosen yet".

   A union with the vocabulary rather than a bare `UserGender`, because the dialog opens with
   nothing chosen and `''` is what says so. The earlier version defaulted this to `'other'`,
   which was not a neutral placeholder: the vocabulary is male/female/other, so every account
   created without a deliberate choice was recorded as *other* -- a claim about the person that
   nobody made, on a column that is NOT NULL on all three tables. The select now starts on its
   placeholder and the required check names Gender by name, so the account is written only once
   somebody has actually said which it is.

   The same reasoning does not apply to the *edit* dialog's seed, which is a fallback for a
   stored value outside the vocabulary rather than a default: there, showing something is the
   honest option, because the row already holds an answer. */
  gender: UserGender | '';
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

/**
 * How a rejected create field is named back to the operator.
 *
 * "Please fill in all required fields" was true and useless: a moderator form asking for a
 * department said the same thing whether the department or the name was the one missing, and there
 * is no department to fill in. Naming the field points at the input that needs attention.
 */
/**
 * The id of a field's message, so a screen reader hears the message with the field.
 *
 * Derived from the field name rather than passed separately, because the two drifting apart is
 * exactly what breaks the association: an `aria-describedby` pointing at nothing is silently
 * worse than no `aria-describedby` at all.
 */
const errorIdFor = (dialog: 'create' | 'edit', field: IdentityField): string =>
  `${dialog}-${field}-error`;

/**
 * Move focus to the first field that needs attention.
 *
 * Not a scroll: `focus()` scrolls too, and it puts the caret where the next keystroke goes. A
 * dialog that reports three problems without moving focus leaves the operator at whichever field
 * they happened to be on, reading a message about a different one.
 */
const focusField = (field: IdentityField | null): void => {
  if (!field) return;
  const candidates = [
    document.getElementById(`create-${field}`),
    document.getElementById(`edit-${field}`),
  ];
  candidates.find((element) => element !== null)?.focus();
};

/** Drop one field's message as soon as the operator starts correcting it. */
const clearIssue = (
  setter: (issues: FieldIssues) => void,
  field: IdentityField,
) => {
  setter((prev) => {
    if (prev[field] === undefined) return prev;
    const next = { ...prev };
    delete next[field];
    return next;
  });
};

/**
 * The message under a field, in place rather than in a toast.
 *
 * `role="alert"` so it is announced when it appears. The alternative -- rendering it silently
 * and relying on the operator to notice red text -- leaves the change invisible to anyone using
 * a screen reader, and the submit that revealed it has already been refused.
 */
const FieldError = ({ id, message }: { id: string; message?: string }) => {
  if (!message) return null;
  return (
    <p id={id} role="alert" className="text-xs font-medium text-destructive">
      {message}
    </p>
  );
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

/**
 * The orders the ordinary participants can be laid out in.
 *
 * There is deliberately no "most removals" option here. That order is not a choice for this screen
 * to offer: it is how the flagged section is always arranged, and offering it as one option among
 * several would let an operator sort the flagged section away from the order they are meant to work
 * through it in. Equally, these orders are not applied to the staff sections -- a moderator list
 * sorted by year would be sorted by a column those rows do not have.
 *
 * Name ascending is the default because it is what the server already sends, so loading the page
 * and touching nothing lays the ordinary participants out exactly as they were before the picker
 * existed.
 */
type ProfileSortOption = 'name' | 'name-desc' | 'year' | 'year-desc';

/**
 * The value the year filter carries for "do not filter by year".
 *
 * A year is a number on the wire, so a bare `'all'` cannot be one -- the option list holds years
 * the participants are actually in, and none of them stringify to this.
 */
const ALL_YEARS = 'all';

/**
 * What a department filter's `<Select>` value looks like, and how the name goes into one and comes
 * back out.
 *
 * Prefixed rather than carried bare because `department` has no closed vocabulary -- it is free
 * text belonging to the institution, which is the whole argument in `accountFields.ts` for it not
 * having one. So "all" is a department somebody could genuinely have typed, and a bare value would
 * let that department and the "do not filter" option be the same string; picking the real
 * department would then read as "show me everybody". The prefix is not something a name can begin
 * with by accident of typing, because it ends in a colon.
 */
const DEPARTMENT_VALUE_PREFIX = 'department:';

/**
 * The value the department filter carries for "do not filter by department".
 *
 * Read through `departmentFromValue`, which only recognises the prefixed form, so this cannot be
 * mistaken for a department however it is spelled.
 */
const ALL_DEPARTMENTS = 'all';

const departmentValue = (department: string) => `${DEPARTMENT_VALUE_PREFIX}${department}`;

/** The department a filter value names, or null when it is the "do not filter" sentinel. */
const departmentFromValue = (value: string): string | null =>
  value.startsWith(DEPARTMENT_VALUE_PREFIX) ? value.slice(DEPARTMENT_VALUE_PREFIX.length) : null;

/**
 * Compares two optional values, putting the ones that are absent last in whichever direction is
 * being sorted.
 *
 * Absent is not zero and not the empty string: an admin has no year, and a row can have no name.
 * Neither is a value anybody sorts on, so neither is treated as the smallest of anything --
 * reading a missing year as 0 would put an admin ahead of every first-year on a year sort, and
 * `0` is outside the column's own bounds anyway. The direction is a parameter rather than a
 * negation at the call site because `-1` applied to the "absent goes last" result would turn it
 * into "absent goes first", which is the opposite of what was meant.
 */
const compareWithMissingLast = <T,>(
  a: T | null,
  b: T | null,
  compare: (a: T, b: T) => number,
  direction: 1 | -1 = 1
): number => {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return direction * compare(a, b);
};

/**
 * Names, alphabetically.
 *
 * `sensitivity: 'base'` so `Ada` and `ada` land next to each other rather than being split by
 * everything that sorts between them in code-point order, and `numeric` so a name holding a number
 * is ordered by that number rather than by spelling it out.
 */
const compareByName = (a: AdminUserProfile, b: AdminUserProfile, direction: 1 | -1 = 1) =>
  compareWithMissingLast(
    a.name?.trim() || null,
    b.name?.trim() || null,
    (left, right) => left.localeCompare(right, undefined, { sensitivity: 'base', numeric: true }),
    direction
  );

/**
 * A row's year as a number, or null when it has none: a staff account, or a participant whose
 * year is missing or is not a number.
 */
const yearOf = (profile: AdminUserProfile): number | null =>
  typeof profile.year === 'number' && Number.isFinite(profile.year) ? profile.year : null;

const compareByYear = (a: AdminUserProfile, b: AdminUserProfile, direction: 1 | -1 = 1) =>
  compareWithMissingLast(yearOf(a), yearOf(b), (left, right) => left - right, direction);

/**
 * Most removals first.
 *
 * The order the flagged section is always in, and not something the sort picker can talk it out
 * of. That section is a work queue: the person with the most content taken down is the one a
 * moderator opens first, and reordering the queue by surname would defeat the reason the section
 * is separated from the ordinary participants in the first place.
 */
const byMostRemovals = (a: AdminUserProfile, b: AdminUserProfile) =>
  totalRemovalsFor(b) - totalRemovalsFor(a);

/**
 * One comparator per offered order, keyed by the option that chooses it.
 *
 * A record rather than a switch so that the compiler checks the two against each other: an option
 * added to `ProfileSortOption` without a comparator here fails to build, where a `switch` would
 * quietly fall through to whatever its `default` happened to be.
 */
const SORT_COMPARATORS: Record<ProfileSortOption, (a: AdminUserProfile, b: AdminUserProfile) => number> =
  {
    'name': (a, b) => compareByName(a, b),
    'name-desc': (a, b) => compareByName(a, b, -1),
    'year': (a, b) => compareByYear(a, b),
    'year-desc': (a, b) => compareByYear(a, b, -1),
  };

/**
 * Orders one section's accounts, copying the list first.
 *
 * The copy is the point. Each list handed here is built by a `filter`, but `sort` mutates in
 * place, and `profiles` is component state: sorting one of those in place would reorder the state
 * array itself, so the next render would show the order the operator happened to pick as though it
 * were the fetched order, and every other memo reading that array would see it move under them.
 */
const sortProfiles = (
  accounts: AdminUserProfile[],
  sort: ProfileSortOption
): AdminUserProfile[] => [...accounts].sort(SORT_COMPARATORS[sort]);

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
    // Nothing rather than `'other'`. Gender is a non-null column on all three account tables, so a
    // value has to be chosen -- but choosing it here would mean writing a gender onto every
    // account created without a deliberate answer. `'other'` was doing that silently; now the
    // select shows its placeholder and the required check names the field.
    gender: '',
    account_type: accountType,
  });

  const [profiles, setProfiles] = useState<AdminUserProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  /**
   * The year and the department the cards are narrowed to, or the sentinel for "not narrowed".
   *
   * Two filters rather than one combined cohort, because they are two axes and an operator reaches
   * for them separately: "every second-year in the department I am reviewing" and "everyone in my
   * department, whatever year they are in" are both real questions, and a single cohort picker can
   * only ask the first. Choosing both is the combined cohort, so nothing is lost by splitting them.
   *
   * Strings rather than a number and a department, because Radix `Select` carries a string and
   * coercing at the edge would mean the state could hold something the option list never offered.
   */
  const [yearFilter, setYearFilter] = useState(ALL_YEARS);
  const [departmentFilter, setDepartmentFilter] = useState(ALL_DEPARTMENTS);
  /** Which order the ordinary participants are laid out in. See `ProfileSortOption`. */
  const [sortOption, setSortOption] = useState<ProfileSortOption>('name');
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
  const [saving, setSaving] = useState(false);
  /**
   * What is wrong with each dialog's fields, rendered under the field itself.
   *
   * Held per dialog rather than derived on every render so that a field stops showing its
   * message the moment the operator starts fixing it, instead of the message reappearing on
   * each keystroke of a value that is still incomplete. Both records are cleared when their
   * dialog opens or closes.
   */
  const [editIssues, setEditIssues] = useState<FieldIssues>({});
  const [createIssues, setCreateIssues] = useState<FieldIssues>({});
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateUserForm>(() => buildDefaultCreateForm('users'));
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

  /**
   * The years and departments the cards can be filtered to, read off the accounts already loaded.
   *
   * Derived from the rows rather than from `USER_YEAR_OPTIONS` or a second request, because an
   * option for a cohort nobody is in can only ever produce an empty list: an operator who picks
   * "Year 5" and is shown nothing is told the platform has no fifth-years, when the truth is that
   * the filter was narrower than they thought. One memo over data the page already has is also one
   * request fewer than asking the server for its own cohort list.
   *
   * Staff are skipped, for the same reason the filters below skip them: there is no year or
   * department on an `admin` or `moderator` row for an option to be built out of.
   */
  const availableYears = useMemo(() => {
    const years = new Set<number>();
    for (const profile of profiles) {
      const year = yearOf(profile);
      if (year !== null && !isStaffAccount(profile)) years.add(year);
    }
    return [...years].sort((a, b) => a - b);
  }, [profiles]);

  const availableDepartments = useMemo(() => {
    // Keyed case-insensitively so a department registered two ways at different times is one
    // option and not two that each hide half the people in it. `trim` first, so the same name
    // typed with a trailing space collapses too.
    const byLowerCase = new Map<string, string>();
    for (const profile of profiles) {
      if (isStaffAccount(profile)) continue;
      const department = profile.department?.trim();
      if (!department) continue;
      const key = department.toLowerCase();
      if (!byLowerCase.has(key)) byLowerCase.set(key, department);
    }
    return [...byLowerCase.values()].sort((a, b) =>
      a.localeCompare(b, undefined, { sensitivity: 'base' })
    );
  }, [profiles]);

  /**
   * The accounts left after the search box and the two cohort filters.
   *
   * All three are applied here rather than in parallel memos feeding the sections, because the
   * header count, the "no accounts match" message and the sections themselves all have to be
   * describing the same set of rows. Three memos that each filtered independently would agree
   * until the day one of them was not wired into all three places.
   */
  const filteredProfiles = useMemo(() => {
    const query = searchTerm.trim().toLowerCase();
    const year = yearFilter === ALL_YEARS ? null : Number(yearFilter);
    const department = departmentFromValue(departmentFilter);
    const narrowing = year !== null || department !== null;

    return profiles.filter((profile) => {
      if (query) {
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
        if (!haystack.includes(query)) return false;
      }

      if (!narrowing) return true;

      // Admins and moderators are not narrowed by cohort. They have no year or department to
      // match on, so there is nothing to compare -- and dropping them would hide the one account
      // class an operator must never lose sight of behind a filter they set to find somebody else.
      if (isStaffAccount(profile)) return true;

      if (year !== null && profile.year !== year) return false;
      if (
        department !== null &&
        (profile.department ?? '').trim().toLowerCase() !== department.toLowerCase()
      ) {
        return false;
      }
      return true;
    });
  }, [profiles, searchTerm, yearFilter, departmentFilter]);

  /** Whether anything at all is narrowing the list, which is what the clear button is for. */
  const filtersNarrowed =
    searchTerm.trim() !== '' || yearFilter !== ALL_YEARS || departmentFilter !== ALL_DEPARTMENTS;

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
   * Only the ordinary participants are handed to the sort picker, and the staff sections keep the
   * server's by-name order untouched: a year or a surname is not what an operator is reading a list
   * of administrators for, and a staff row has no year to order by in the first place.
   */
  const sections = useMemo<ProfileSection[]>(() => {
    const admins = filteredProfiles.filter((profile) => profile.account_type === 'admin');
    const moderators = filteredProfiles.filter((profile) => profile.account_type === 'moderator');
    const participants = filteredProfiles.filter((profile) => !isStaffAccount(profile));
    // Most removals first, unconditionally. This is a work queue and the order a moderator works
    // through it in is not theirs to rearrange, so `sortOption` is deliberately not consulted.
    const flagged = participants
      .filter((profile) => totalRemovalsFor(profile) > FLAGGED_REMOVAL_THRESHOLD)
      .sort(byMostRemovals);
    const ordinary = participants.filter(
      (profile) => totalRemovalsFor(profile) <= FLAGGED_REMOVAL_THRESHOLD
    );

    return [
      {
        id: 'admins',
        title: 'Admins',
        description:
          'Accounts holding the full set of capabilities. Listed first, because a staff account with the wrong privileges is the most consequential mistake on this page. Admin accounts cannot be created here; this screen is the only place that creates them.',
        emptyNote: filtersNarrowed
          ? 'No admin accounts match the current search.'
          : 'No admin accounts exist yet.',
        icon: Crown,
        badgeVariant: 'default',
        accounts: admins,
      },
      {
        id: 'moderators',
        title: 'Moderators',
        description:
          'Accounts that review and rule on reports. They reach fewer moderation screens than an admin, and they hold no participant content of their own, so a moderator needs no year, department, bio or interests.',
        emptyNote: filtersNarrowed
          ? 'No moderator accounts match the current search.'
          : 'No moderator accounts exist yet.',
        icon: ShieldHalf,
        badgeVariant: 'secondary',
        accounts: moderators,
        create: { label: 'Create moderator', accountType: 'moderator' },
      },
      {
        id: FLAGGED_SECTION_ID,
        title: `Participants with more than ${FLAGGED_REMOVAL_THRESHOLD} removals`,
        description: `Participants whose content has been taken down more than ${FLAGGED_REMOVAL_THRESHOLD} times in total, counting confessions and their comments together, love notes and chat messages. Complaints that were dismissed do not count towards this.`,
        emptyNote: filtersNarrowed
          ? `No participant above ${FLAGGED_REMOVAL_THRESHOLD} removals matches the current filters.`
          : `No participant is above ${FLAGGED_REMOVAL_THRESHOLD} upheld removals.`,
        icon: ShieldAlert,
        badgeVariant: 'destructive',
        accounts: flagged,
      },
      {
        id: PARTICIPANTS_SECTION_ID,
        title: 'Participants',
        description:
          'Everyone else on the platform, at or below the removal threshold. Blocked accounts stay here unless removals put them in the section above.',
        emptyNote: filtersNarrowed
          ? 'No participant matches the current filters.'
          : 'No participants match.',
        icon: Users,
        badgeVariant: 'outline',
        accounts: sortProfiles(ordinary, sortOption),
        create: { label: 'Create participant', accountType: 'users' },
      },
    ];
  }, [filteredProfiles, filtersNarrowed, sortOption]);

  /**
   * Whether a filter has hidden every account on the platform.
   *
   * Distinguishes "the filters matched nothing" from "the platform is empty", which look identical
   * in the row count and need to be rendered differently: the first is a dead end the operator has
   * to be able to back out of, the second is a page whose create buttons are the point.
   */
  const nothingMatches = filteredProfiles.length === 0 && filtersNarrowed;

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
      // Seeded only for a participant. A staff row has no cohort, so putting defaults for these
      // into the form would put values there that no visible field could ever be compared
      // against -- and a year seeded to something on a moderator is a control that looks like it
      // says something about their year when it says nothing at all.
      ...(isParticipant
        ? {
            // Same normalisation as gender below: a stored year from before the range was
            // closed matches no option, and a select with no matching option displays the wrong
            // thing as though it were chosen. `null` rather than `''` because the field is a
            // number here, and null is a value it can actually hold.
            year: isUserYear(profile.year) ? profile.year : null,
            department: profile.department ?? '',
          }
        : {}),
    });
    setEditIssues({});
    setEditOpen(true);
  };

  const resetEditor = () => {
    setEditOpen(false);
    setSelectedProfile(null);
    setEditForm(null);
    setEditIssues({});
    setSaving(false);
  };

  const handleSaveProfile = async () => {
    if (!selectedProfile || !editForm) {
      return;
    }

    const isParticipant = !isStaffAccount(selectedProfile);

    // Checked here rather than only by the server, because the server's answer arrives as a 422
    // after a round-trip and reads the same either way. These four fields are NOT NULL on every
    // account table and the email is `EmailStr`, so an emptied name or a mistyped address used
    // to be a submit-and-wait rather than something the dialog could say while it was still open.
    const issues = validateIdentityFields(
      {
        name: editForm.name,
        regno: editForm.regno,
        email: editForm.email,
        gender: editForm.gender,
        year: editForm.year ?? null,
        department: editForm.department,
      },
      { isParticipant },
    );
    setEditIssues(issues);
    if (hasFieldIssues(issues)) {
      focusField(firstInvalidField(issues));
      return;
    }
    const payload: Record<string, unknown> = {};

    // Only the fields the target table actually has. The server drops the participant-only keys
    // from a staff update, so sending them would either be ignored (the operator's change
    // silently vanishes on the next fetch) or, worse, be sent with a value the form invented.
    const textFields: Array<keyof AdminUserProfile> = [
      'name',
      'regno',
      'email',
      ...(isParticipant ? (['department'] as const) : []),
    ];

    textFields.forEach((field) => {
      const newValue = (editForm as Record<string, unknown>)[field];
      if (newValue === undefined) return;
      const currentValue = selectedProfile[field];
      if ((newValue ?? '') !== (currentValue ?? '')) {
        payload[field] = typeof newValue === 'string' ? newValue : '';
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

    if (Object.keys(payload).length === 0) {
      toast.info('No changes to save.');
      return;
    }

    try {
      setSaving(true);
      const updatedProfile = await updateAdminUser(selectedProfile.id, payload);

      setProfiles((prev) =>
        prev.map((profile) =>
          profile.id === selectedProfile.id ? { ...profile, ...updatedProfile } : profile,
        )
      );
      toast.success('Profile updated successfully.');
      resetEditor();
    } catch (error) {
      console.error('Failed to update profile', error);
      // The same reasoning as the create handler: this payload is capped and forbidden from
      // extras, so a 422 here is routine and has to be readable. The editor writes the same
      // four identity fields the create dialog does, so it has the same ways to be wrong.
      toast.error(describeApiError(error, 'Unable to save changes for this user.'));
    } finally {
      setSaving(false);
    }
  };

  const openCreateDialog = (accountType: AccountType) => {
    setCreateForm(buildDefaultCreateForm(accountType));
    // Cleared here as well as on close, so reopening the dialog after a refused attempt starts
    // from a blank form rather than from the messages of the previous one.
    setCreateIssues({});
    setCreateOpen(true);
  };

  const resetCreateDialog = () => {
    setCreateOpen(false);
    setCreateForm(buildDefaultCreateForm('users'));
    setCreateIssues({});
    setCreating(false);
  };

  const handleCreateUser = async () => {
    const isParticipant = createForm.account_type === 'users';

    // Checked here rather than only by the server. Which fields are required is a function of
    // the table being written to, not one list for all three: name, regno, email and gender are
    // non-null columns on every account table, and the cohort pair exists only on `users`.
    //
    // The messages render under the field they belong to. They used to be a single toast naming
    // one field, which meant a form with two things wrong had to be submitted twice, and an
    // invalid email could not be told apart from a missing one at all -- `type="email"` reports
    // in the browser's own words, or not at all.
    const issues = validateIdentityFields(createForm, { isParticipant });
    setCreateIssues(issues);
    if (hasFieldIssues(issues)) {
      focusField(firstInvalidField(issues));
      return;
    }

    const payload: AdminUserCreatePayload = {
      regno: createForm.regno.trim(),
      name: createForm.name.trim(),
      email: createForm.email.trim(),
      // Narrowed rather than cast: the checks above have already returned unless this is one of
      // the three, but the compiler cannot see that through the `''` union member, so the
      // vocabulary is re-checked instead of asserted. Same check, so the two cannot disagree.
      gender: isUserGender(createForm.gender) ? createForm.gender : 'other',
      account_type: createForm.account_type,
    };

    // The cohort, and only for a participant: the staff tables have no year or department column
    // and the server refuses those keys for a staff row, so a form that never offered them must
    // not send empty ones behind the operator's back either.
    if (isParticipant) {
      // Same narrowing, same reason, as the gender above.
      payload.year = isUserYear(createForm.year) ? createForm.year : undefined;
      payload.department = createForm.department.trim();
    }

    try {
      setCreating(true);
      const createdProfile = await createAdminUser(payload);

      setProfiles((prev) => [createdProfile, ...prev]);
      toast.success(`${ACCOUNT_TYPE_LABELS[createForm.account_type]} account created successfully.`);
      resetCreateDialog();
    } catch (error: unknown) {
      console.error('Failed to create user', error);
      // Not `error.response.data.detail` directly: a 422 carries an array of Pydantic error
      // objects there, and handing that to a toast renders objects as React children and takes
      // the page down. Every field is capped on the server and extras are forbidden, so this is
      // the ordinary failure for a mistyped register number, not an edge case.
      toast.error(describeApiError(error, 'Unable to create this account.'));
    } finally {
      setCreating(false);
    }
  };

  /**
   * One section: its heading, its count, the button that creates into it, and its grid of cards.
   *
   * A function rather than inline in the `sections.map` it used to live in, because the filter
   * controls are inserted into that map -- immediately above the ordinary participants -- and so
   * each section and the controls have to be emitted together in one pass.
   */
  const renderSection = (section: ProfileSection) => {
    const Icon = section.icon;
    const accountCount = section.accounts.length;
    const create = section.create;
    return (
      <section aria-labelledby={`profile-section-${section.id}`}>
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
  };

  /**
   * The search box, the two cohort filters, the sort picker and the way back out of all of them.
   *
   * Held as a value rather than written into the tree so the same controls can be emitted in the
   * two places that need them: immediately above the ordinary participants, which is where they
   * belong, and above the no-match card when a filter has hidden everything, where they are the
   * only way back.
   */
  const filterControls = (
    <>
      <div className="flex flex-col gap-3 lg:flex-row lg:flex-wrap lg:items-center lg:gap-4">
        <div className="relative w-full lg:max-w-md">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search by name, register number, email, or class"
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
            className="pl-9"
          />
        </div>

        {/* `htmlFor` onto the trigger's `id` rather than an `aria-label` on the trigger: the
            visible word is then the accessible name, so a screen reader announces "Year" and not
            something the operator cannot see. */}
        <div className="flex items-center gap-2">
          <Label
            htmlFor="profile-year-filter"
            className="text-xs font-medium uppercase text-muted-foreground"
          >
            Year
          </Label>
          <Select value={yearFilter} onValueChange={setYearFilter}>
            <SelectTrigger id="profile-year-filter" className="w-32">
              <SelectValue placeholder="Year" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_YEARS}>All years</SelectItem>
              {availableYears.map((year) => (
                <SelectItem key={year} value={String(year)}>
                  {year}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-2">
          <Label
            htmlFor="profile-department-filter"
            className="text-xs font-medium uppercase text-muted-foreground"
          >
            Department
          </Label>
          <Select value={departmentFilter} onValueChange={setDepartmentFilter}>
            <SelectTrigger id="profile-department-filter" className="w-48">
              <SelectValue placeholder="Department" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_DEPARTMENTS}>All departments</SelectItem>
              {availableDepartments.map((department) => (
                <SelectItem key={department} value={departmentValue(department)}>
                  {department}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-2">
          <Label
            htmlFor="profile-sort"
            className="text-xs font-medium uppercase text-muted-foreground"
          >
            Sort
          </Label>
          <Select
            value={sortOption}
            onValueChange={(value) => setSortOption(value as ProfileSortOption)}
          >
            <SelectTrigger id="profile-sort" className="w-48">
              <SelectValue placeholder="Sort" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="name">Name (A-Z)</SelectItem>
              <SelectItem value="name-desc">Name (Z-A)</SelectItem>
              <SelectItem value="year">Year (low to high)</SelectItem>
              <SelectItem value="year-desc">Year (high to low)</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {/* Only rendered when something is actually narrowing the list, so a control that
            clears nothing is not sitting there implying that something is. */}
        {filtersNarrowed && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setSearchTerm('');
              setYearFilter(ALL_YEARS);
              setDepartmentFilter(ALL_DEPARTMENTS);
            }}
          >
            Clear filters
          </Button>
        )}
      </div>

      {/* Said rather than left for the operator to infer. Three sections are above these
          controls and one is below, and the controls do not treat the four the same way, so
          without this the placement reads as an arbitrary split rather than as a deliberate
          one. It also draws the line exactly where the line is: the search box reaches every
          section, the two cohort selects reach only the participants, and the sort picker is
          offered only for the ordinary participants. */}
      <p className="-mt-4 text-xs text-muted-foreground">
        The search box applies to all four sections. The year and department filters apply to both
        participant sections only: admins and moderators have neither to match on, so those two
        sections stay complete whatever you pick here.
      </p>
    </>
  );

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
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-xs text-muted-foreground/80">
              Total users: {profiles.length} · Blocked: {profiles.filter((p) => p.is_blocked).length} ·
              Over {FLAGGED_REMOVAL_THRESHOLD} removals: {flaggedCount}
            </p>
            {/* Kept up here rather than travelling with the controls, because it answers a
                question about the whole platform and not about the participants below them. */}
            <Badge variant="secondary" className="w-max gap-2">
              <Users className="h-4 w-4" />
              Showing {filteredProfiles.length} of {profiles.length} profiles
            </Badge>
          </div>
        </header>

        {loading ? (
          <Card>
            <CardContent className="flex h-40 items-center justify-center text-sm text-muted-foreground">
              Loading user profiles...
            </CardContent>
          </Card>
        ) : nothingMatches ? (
          /* Every section is replaced by the one message rather than left rendering an empty grid.
             A search that has just filtered out the only admin on the platform must not leave a
             heading behind reading "No admin accounts exist yet" -- that is a false claim about
             the platform, and it is the reading the original note on this screen was written to
             prevent. The controls come with the message, so this is a dead end the operator can
             undo rather than one they have to reload out of. On a genuinely empty platform nothing
             is filtered, so the sections still render and the create buttons living in them are
             not the one thing missing from the page. */
          <>
            {filterControls}
            <Card>
              <CardHeader className="flex flex-col gap-1">
                <CardTitle className="text-lg">No accounts match</CardTitle>
                <CardDescription>
                  No account matches the current search and filters.
                </CardDescription>
                <Button
                  variant="outline"
                  size="sm"
                  className="w-max"
                  onClick={() => {
                    setSearchTerm('');
                    setYearFilter(ALL_YEARS);
                    setDepartmentFilter(ALL_DEPARTMENTS);
                  }}
                >
                  Show all accounts
                </Button>
              </CardHeader>
            </Card>
          </>
        ) : (
          /* One pass, so a section inserted above the participants would push the controls down
             with them rather than leave them stranded in the middle of the page. */
          sections.map((section) => (
            <Fragment key={section.id}>
              {/* Directly above the ordinary participants: the section these controls exist to
                  narrow and order. Sitting higher up the page it would have appeared to filter
                  the work queue and the staff lists above it as well, and it filters neither --
                  the queue's own order is fixed, and a staff row has no year to match on. */}
              {section.id === PARTICIPANTS_SECTION_ID && filterControls}
              {renderSection(section)}
            </Fragment>
          ))
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
                    ? "Update the selected user's identity and cohort. Leave a field untouched to keep the current value."
                    : `A ${staffNoun(selectedProfile)} account has no year or department, so those are not offered here.`}
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
                      aria-invalid={Boolean(editIssues.name)}
                      aria-describedby={editIssues.name ? errorIdFor('edit', 'name') : undefined}
                      onChange={(event) => {
                        setEditForm((prev) =>
                          prev ? { ...prev, name: event.target.value } : prev,
                        );
                        clearIssue(setEditIssues, 'name');
                      }}
                      placeholder="Enter full name"
                      maxLength={ADMIN_NAME_MAX}
                    />
                    <FieldError id={errorIdFor('edit', 'name')} message={editIssues.name} />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-email">Email</Label>
                    <Input
                      id="edit-email"
                      type="email"
                      value={editForm.email ?? ''}
                      aria-invalid={Boolean(editIssues.email)}
                      aria-describedby={editIssues.email ? errorIdFor('edit', 'email') : undefined}
                      onChange={(event) => {
                        setEditForm((prev) =>
                          prev ? { ...prev, email: event.target.value } : prev,
                        );
                        clearIssue(setEditIssues, 'email');
                      }}
                      placeholder="user@example.com"
                    />
                    <FieldError id={errorIdFor('edit', 'email')} message={editIssues.email} />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-regno">Register number</Label>
                    <Input
                      id="edit-regno"
                      value={editForm.regno ?? ''}
                      aria-invalid={Boolean(editIssues.regno)}
                      aria-describedby={editIssues.regno ? errorIdFor('edit', 'regno') : undefined}
                      onChange={(event) => {
                        setEditForm((prev) =>
                          prev ? { ...prev, regno: event.target.value } : prev,
                        );
                        clearIssue(setEditIssues, 'regno');
                      }}
                      placeholder="Enter register number"
                      maxLength={ADMIN_REGNO_MAX}
                    />
                    <FieldError id={errorIdFor('edit', 'regno')} message={editIssues.regno} />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="edit-gender">Gender</Label>
                    <Select
                      value={(editForm.gender as UserGender) ?? 'other'}
                      onValueChange={(value) => {
                        setEditForm((prev) =>
                          prev ? { ...prev, gender: value as UserGender } : prev,
                        );
                        clearIssue(setEditIssues, 'gender');
                      }}
                    >
                      <SelectTrigger
                        id="edit-gender"
                        aria-invalid={Boolean(editIssues.gender)}
                        aria-describedby={
                          editIssues.gender ? errorIdFor('edit', 'gender') : undefined
                        }
                      >
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
                    <FieldError id={errorIdFor('edit', 'gender')} message={editIssues.gender} />
                  </div>
                </div>

                {/* Participant columns. */}
                {editingIsParticipant && (
                  /* Year is a select and department is a text box, not two text boxes and not
                     one joined field. Year is held to a closed vocabulary on the server, so a
                     free-text box could only ever produce a value it refuses; department has no
                     such list, because the departments that exist belong to the institution
                     rather than to this codebase. */
                  <div className="grid gap-4 md:grid-cols-2">
                    <div className="grid gap-2">
                      <Label htmlFor="edit-year">Year</Label>
                      <Select
                        value={
                          editForm.year == null
                            ? ''
                            : String(editForm.year)
                        }
                        onValueChange={(value) => {
                          setEditForm((prev) =>
                            prev ? { ...prev, year: Number(value) } : prev,
                          );
                          clearIssue(setEditIssues, 'year');
                        }}
                      >
                        <SelectTrigger
                          id="edit-year"
                          aria-invalid={Boolean(editIssues.year)}
                          aria-describedby={editIssues.year ? errorIdFor('edit', 'year') : undefined}
                        >
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
                      <FieldError id={errorIdFor('edit', 'year')} message={editIssues.year} />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="edit-department">Department/Division</Label>
                      <Input
                        id="edit-department"
                        value={editForm.department ?? ''}
                        aria-invalid={Boolean(editIssues.department)}
                        aria-describedby={
                          editIssues.department ? errorIdFor('edit', 'department') : undefined
                        }
                        onChange={(event) => {
                          setEditForm((prev) =>
                            prev ? { ...prev, department: event.target.value } : prev,
                          );
                          clearIssue(setEditIssues, 'department');
                        }}
                        placeholder="CSE, ECE, MECH, etc."
                        maxLength={ADMIN_DEPARTMENT_MAX}
                      />
                      <FieldError
                        id={errorIdFor('edit', 'department')}
                        message={editIssues.department}
                      />
                    </div>
                  </div>
                )}

                <p className="text-xs text-muted-foreground">
                  Profile picture, emoji tag, bio, interests and the matchmaking, notification and
                  love-note settings belong to the account, not to this form. The participant sets
                  them from their own profile; the server refuses them here even if a request is
                  made by hand.
                </p>
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
                    aria-invalid={Boolean(createIssues.name)}
                    aria-describedby={
                      createIssues.name ? errorIdFor('create', 'name') : undefined
                    }
                    onChange={(event) => {
                      setCreateForm((prev) => ({ ...prev, name: event.target.value }));
                      clearIssue(setCreateIssues, 'name');
                    }}
                    placeholder="Enter full name"
                    maxLength={ADMIN_NAME_MAX}
                  />
                  <FieldError id={errorIdFor('create', 'name')} message={createIssues.name} />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-regno">Register number *</Label>
                  <Input
                    id="create-regno"
                    value={createForm.regno}
                    aria-invalid={Boolean(createIssues.regno)}
                    aria-describedby={
                      createIssues.regno ? errorIdFor('create', 'regno') : undefined
                    }
                    onChange={(event) => {
                      setCreateForm((prev) => ({ ...prev, regno: event.target.value }));
                      clearIssue(setCreateIssues, 'regno');
                    }}
                    placeholder="eg. 21CS100"
                    maxLength={ADMIN_REGNO_MAX}
                  />
                  <FieldError id={errorIdFor('create', 'regno')} message={createIssues.regno} />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-gender">Gender *</Label>
                  {/* Nothing preselected. The earlier default of `other` was a claim about the
                      person rather than a placeholder: the vocabulary is male/female/other, so
                      every account created without a deliberate answer was written down as
                      *other*. Radix renders the placeholder for `''`, and the required check
                      below names this field rather than letting it be inferred. */}
                  <Select
                    value={createForm.gender}
                    onValueChange={(value) => {
                      setCreateForm((prev) => ({ ...prev, gender: value as UserGender }));
                      clearIssue(setCreateIssues, 'gender');
                    }}
                  >
                    <SelectTrigger
                      id="create-gender"
                      aria-invalid={Boolean(createIssues.gender)}
                      aria-describedby={
                        createIssues.gender ? errorIdFor('create', 'gender') : undefined
                      }
                    >
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
                  <FieldError
                    id={errorIdFor('create', 'gender')}
                    message={createIssues.gender}
                  />
                </div>
                <div className="grid gap-2">
                  {/* Required for every account type. It used to be marked optional for staff,
                      which left a moderator with no address anybody could reach. */}
                  <Label htmlFor="create-email">Email *</Label>
                  <Input
                    id="create-email"
                    type="email"
                    value={createForm.email}
                    aria-invalid={Boolean(createIssues.email)}
                    aria-describedby={
                      createIssues.email ? errorIdFor('create', 'email') : undefined
                    }
                    onChange={(event) => {
                      setCreateForm((prev) => ({ ...prev, email: event.target.value }));
                      clearIssue(setCreateIssues, 'email');
                    }}
                    placeholder="user@example.com"
                  />
                  <FieldError id={errorIdFor('create', 'email')} message={createIssues.email} />
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
                        onValueChange={(value) => {
                          setCreateForm((prev) => ({ ...prev, year: Number(value) }));
                          clearIssue(setCreateIssues, 'year');
                        }}
                      >
                        <SelectTrigger
                          id="create-year"
                          aria-invalid={Boolean(createIssues.year)}
                          aria-describedby={
                            createIssues.year ? errorIdFor('create', 'year') : undefined
                          }
                        >
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
                      <FieldError
                        id={errorIdFor('create', 'year')}
                        message={createIssues.year}
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="create-department">Department/Division *</Label>
                      <Input
                        id="create-department"
                        value={createForm.department}
                        aria-invalid={Boolean(createIssues.department)}
                        aria-describedby={
                          createIssues.department
                            ? errorIdFor('create', 'department')
                            : undefined
                        }
                        onChange={(event) => {
                          setCreateForm((prev) => ({
                            ...prev,
                            department: event.target.value,
                          }));
                          clearIssue(setCreateIssues, 'department');
                        }}
                        placeholder="CSE, ECE, MECH, etc."
                        maxLength={ADMIN_DEPARTMENT_MAX}
                      />
                      <FieldError
                        id={errorIdFor('create', 'department')}
                        message={createIssues.department}
                      />
                    </div>
                  </>
                )}
              </div>

              <p className="text-xs text-muted-foreground">
                The account starts with no picture, no emoji tag, no bio, no interests, and with
                matchmaking, email notifications and receiving love notes switched on while
                sending love notes is switched off. Those belong to the account, which sets them
                from its own profile; the server refuses them on this request even if one is
                sent by hand.
              </p>
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
