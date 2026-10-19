import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, Loader2, Search } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ALL_COHORTS, cohortFromKey, cohortKey, cohortLabel, type Cohort } from '@/lib/accountFields';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  getPartnerCohorts,
  type CouplePartnerCandidate,
  type CouplePartnersPage,
  type CouplePartnersQuery,
} from '@/services/api';

/**
 * Choosing a partner, shared by all four couple games.
 *
 * The picker never downloads the participant table: every page, search and
 * filter is answered by the server (SQL LIMIT/OFFSET plus WHERE), and the rows
 * on screen are always the contiguous [0, length) window of the server's
 * name-ordered answer. A "load more" click appends the next page beneath the
 * rows already read; the search only runs when submitted (Enter or the search
 * button), never per keystroke; the cohort select is the single "CSE 1" filter
 * -- a year and a department at once -- and the gender button is the same
 * discovery aid the screens always had, now answered in SQL so a page is never
 * mostly filtered out under the reader.
 *
 * Choosing itself stays the screen's own write: the picker awaits the
 * callback, disables its buttons while it is in flight, and marks the row the
 * screen reports as the current pick.
 */

/** How many candidates one page holds. The backend clamps anything larger. */
const PARTNER_PAGE_SIZE = 20;

/** The rows inside an answer, whether it is the page envelope or a legacy bare array. */
const partnerRowsOf = (
  answer: CouplePartnersPage | CouplePartnerCandidate[],
): CouplePartnerCandidate[] => {
  if (Array.isArray(answer)) return answer;
  return Array.isArray(answer?.partners) ? answer.partners : [];
};

/** The matching-row count beside an answer, falling back to the rows at hand. */
const partnerTotalOf = (
  answer: CouplePartnersPage | CouplePartnerCandidate[],
  fallback: number,
): number => {
  if (Array.isArray(answer)) return answer.length;
  const total = answer?.total;
  return typeof total === 'number' && Number.isFinite(total) ? total : fallback;
};

export interface PartnerPickerProps {
  /** The game's own partner page loader, so each screen keeps its endpoint. */
  loadPartners: (
    query: CouplePartnersQuery,
  ) => Promise<CouplePartnersPage | CouplePartnerCandidate[]>;
  /** Id of the caller's current pick; its row is marked and shown as chosen. */
  selectedId?: string | null;
  /** The screen's own selection write. Disables the buttons while it runs. */
  onChoose: (candidateId: string) => Promise<void> | void;
  /** `grid` keeps Plot Twist's two-column arrangement; `list` is the row list. */
  layout?: 'list' | 'grid';
  /** Copy for an empty answer. */
  emptyMessage?: string;
}

export const PartnerPicker = ({
  loadPartners,
  selectedId = null,
  onChoose,
  layout = 'list',
  emptyMessage = 'Nobody matches that filter yet. Try the search or the filters.',
}: PartnerPickerProps) => {
  const [partners, setPartners] = useState<CouplePartnerCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  /** The search the server has actually answered; typing alone searches nothing. */
  const [appliedSearch, setAppliedSearch] = useState('');
  const [showSameGender, setShowSameGender] = useState(false);
  const [cohorts, setCohorts] = useState<Cohort[]>([]);
  const [selectedCohortKey, setSelectedCohortKey] = useState(ALL_COHORTS);
  const [choosingId, setChoosingId] = useState<string | null>(null);

  /**
   * The read that is allowed to answer.
   *
   * Two reads of the picker can be in flight at once: the reader changes a filter
   * while the previous filter's read is still on the wire. Nothing stopped the
   * slower one from being written, so the rows that arrived last were the ones
   * for the filter the reader had already changed away from -- the list showed
   * the people they had just filtered *out*, and said so with the right count.
   *
   * Each read claims a number and only the most recent claim may write, which is
   * the same guard the search on the confessions feed already had
   * (``searchRequestId``) and the same reasoning.
   */
  const latestReadId = useRef(0);

  /**
   * One page from the server: the next `PARTNER_PAGE_SIZE` people the current
   * filters match, starting at `skip`. `append` grows the window downwards, so
   * a "load more" click adds beneath the rows already on screen and the
   * reader's place never moves.
   */
  const readPage = useCallback(
    async (
      skip: number,
      append: boolean,
      filters: { search: string; sameGender: boolean; cohortKey: string },
    ) => {
      // Claimed before anything awaits, so a read that is overtaken knows it was.
      const readId = latestReadId.current + 1;
      latestReadId.current = readId;
      if (append) {
        setLoadingMore(true);
      } else {
        setLoading(true);
      }
      try {
        const cohort = cohortFromKey(filters.cohortKey);
        const answer = await loadPartners({
          skip,
          limit: PARTNER_PAGE_SIZE,
          ...(filters.search.trim() ? { q: filters.search.trim() } : {}),
          genderMode: filters.sameGender ? 'same' : 'different',
          ...(cohort ? { year: cohort.year, department: cohort.department } : {}),
        });
        // A newer read has answered since: this one is for a filter the reader has
        // already changed, so its rows are not the ones on screen.
        if (latestReadId.current !== readId) return;
        const rows = partnerRowsOf(answer);
        setPartners((previous) => (append ? [...previous, ...rows] : rows));
        setTotal(partnerTotalOf(answer, rows.length));
        setError(null);
      } catch (loadError) {
        if (latestReadId.current !== readId) return;
        setError(describeApiError(loadError, 'Could not load people to choose from.'));
      } finally {
        // Only the read that is still current may clear the flags: a stale read
        // that finished last would report "loaded" over a read that is still
        // running, and the spinner would go out while the list was empty.
        if (latestReadId.current === readId) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [loadPartners],
  );

  // The first page opens the picker. The loader is a stable module function on
  // every screen, so this runs once per mount.
  useEffect(() => {
    void readPage(0, false, { search: '', sameGender: false, cohortKey: ALL_COHORTS });
  }, [readPage]);

  // The cohort palette is static for the session. Failing to fetch it leaves
  // the filter on "All" rather than blocking the picker.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await getPartnerCohorts();
        if (!cancelled && Array.isArray(list)) setCohorts(list);
      } catch {
        /* The filter still works with "All" only. */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const submitSearch = () => {
    setAppliedSearch(searchTerm);
    void readPage(0, false, {
      search: searchTerm,
      sameGender: showSameGender,
      cohortKey: selectedCohortKey,
    });
  };

  const toggleSameGender = () => {
    const next = !showSameGender;
    setShowSameGender(next);
    void readPage(0, false, {
      search: appliedSearch,
      sameGender: next,
      cohortKey: selectedCohortKey,
    });
  };

  const changeCohort = (key: string) => {
    setSelectedCohortKey(key);
    void readPage(0, false, {
      search: appliedSearch,
      sameGender: showSameGender,
      cohortKey: key,
    });
  };

  const loadMore = () => {
    if (loadingMore) return;
    void readPage(partners.length, true, {
      search: appliedSearch,
      sameGender: showSameGender,
      cohortKey: selectedCohortKey,
    });
  };

  const handleChoose = async (candidateId: string) => {
    if (choosingId !== null) return;
    setChoosingId(candidateId);
    try {
      await onChoose(candidateId);
    } catch {
      /* The screen that owns the write reports the failure; the picker stays usable. */
    } finally {
      setChoosingId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex min-w-44 flex-1 flex-col gap-2">
          <Label htmlFor="partner-picker-search">Find someone</Label>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              id="partner-picker-search"
              value={searchTerm}
              placeholder="Type a name"
              autoComplete="off"
              className="pl-10"
              onChange={(event) => setSearchTerm(event.target.value)}
              // Enter submits the same way the button does, so a search is a
              // deliberate act either way -- the box does not re-query per keystroke.
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  submitSearch();
                }
              }}
            />
          </div>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={submitSearch}>
          <Search className="h-4 w-4" />
          Search
        </Button>
        <div className="flex min-w-40 flex-col gap-2">
          <Label htmlFor="partner-picker-cohort">Year &amp; division</Label>
          <Select value={selectedCohortKey} onValueChange={changeCohort}>
            <SelectTrigger
              id="partner-picker-cohort"
              aria-label="Filter by year and division"
            >
              <SelectValue placeholder="All years & divisions" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_COHORTS}>All</SelectItem>
              {cohorts.map((cohort) => {
                const key = cohortKey(cohort);
                return (
                  <SelectItem key={key} value={key}>
                    {cohortLabel(cohort.year, cohort.department)}
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </div>
        <Button
          type="button"
          variant={showSameGender ? 'default' : 'outline'}
          size="sm"
          aria-pressed={showSameGender}
          onClick={toggleSameGender}
        >
          {showSameGender ? 'Showing same gender' : 'Show same gender'}
        </Button>
      </div>

      {error && (
        <p role="alert" className="text-sm font-medium text-destructive">
          {error}
        </p>
      )}

      {loading && !error && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading people…
        </p>
      )}

      {!loading && !error && partners.length === 0 && (
        <p className="text-sm text-muted-foreground">{emptyMessage}</p>
      )}

      {!loading && partners.length > 0 && (
        <ul className={cn('gap-2', layout === 'grid' ? 'grid sm:grid-cols-2' : 'flex flex-col')}>
          {partners.map((candidate) => {
            const isCurrent = selectedId === candidate.id;
            return (
              <li
                key={candidate.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border/60 p-3"
              >
                <div className="min-w-0">
                  <p className="truncate font-semibold">{candidate.name}</p>
                  <p className="truncate text-xs capitalize text-muted-foreground">
                    {candidate.gender} · {candidate.department} · Year {candidate.year}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {isCurrent && <Badge variant="secondary">Your pick</Badge>}
                  <Button
                    type="button"
                    size="sm"
                    variant={isCurrent ? 'ghost' : 'outline'}
                    disabled={choosingId !== null}
                    onClick={() => void handleChoose(candidate.id)}
                    aria-label={`Choose ${candidate.name} as partner`}
                  >
                    {choosingId === candidate.id ? 'Saving…' : isCurrent ? 'Chosen' : 'Choose'}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {!loading && !error && partners.length < total && (
        <div className="flex justify-center">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={loadMore}
            disabled={loadingMore}
            aria-label="Load more people"
          >
            <ChevronDown className={cn('h-4 w-4', loadingMore && 'animate-bounce')} />
            {loadingMore ? 'Loading…' : `Load more (${total - partners.length} remaining)`}
          </Button>
        </div>
      )}
    </div>
  );
};

export default PartnerPicker;
