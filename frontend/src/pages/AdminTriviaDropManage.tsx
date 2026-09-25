import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { ArrowLeft, CircleHelp, X } from 'lucide-react';
import { toast } from 'sonner';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import {
  cancelTriviaQuestion,
  createTriviaQuestion,
  deployTriviaQuestion,
  getAdminTriviaQuestions,
  getCurrentAdminTriviaQuestion,
  rescheduleTriviaQuestion,
  scheduleTriviaQuestion,
  updateTriviaDraft,
  type TriviaQuestion,
} from '@/services/api';

/** Now as UTC `YYYY-MM-DDTHH:mm` for the `datetime-local` release control. */
const nowLocalInput = () => {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  return date.toISOString().slice(0, 16);
};

/** A stored UTC instant as the `datetime-local` value the control speaks. */
const utcInputValue = (iso: string | null): string =>
  iso ? new Date(iso).toISOString().slice(0, 16) : nowLocalInput();

/** A stored UTC instant as a readable label, always naming the zone. */
const utcLabel = (iso: string | null): string =>
  iso ? `${new Date(iso).toISOString().replace('T', ' ').slice(0, 16)} UTC` : '—';

/** Client-side mirror of the server rules, so problems surface before saving. */
const validateForm = (
  questionText: string,
  options: string[],
  correct: number[],
): string | null => {
  if (questionText.trim().length === 0) return 'The question text must not be empty.';
  if (questionText.trim().length > 500) return 'The question is at most 500 characters.';
  if (options.length !== 4 || options.some((option) => option.trim().length === 0)) {
    return 'All four options must be present and non-empty.';
  }
  if (options.some((option) => option.trim().length > 200)) {
    return 'Each option is at most 200 characters.';
  }
  const seen = new Set<string>();
  for (const option of options) {
    const normalized = option.trim().toLowerCase();
    if (seen.has(normalized)) {
      return `Duplicate option "${option.trim()}": each option may appear only once.`;
    }
    seen.add(normalized);
  }
  if (correct.length === 0) return 'Designate at least one correct option.';
  if (correct.some((index) => index < 0 || index > 3)) {
    return 'Each correct option must be one of the four options.';
  }
  return null;
};

interface FormState {
  questionText: string;
  options: [string, string, string, string];
  correct: number[];
}

/**
 * Authoring one question: text, four options, and the correct set as
 * checkboxes -- one or more may be checked. The single vs multiple
 * distinction is derived from how many are checked, never configured: the
 * form carries no mode, so no mode can disagree with the key. Validation
 * errors are reported without clearing anything typed.
 */
const QuestionForm = ({
  idPrefix,
  initial,
  releaseControl,
  submitLabel,
  busy,
  onSubmit,
}: {
  idPrefix: string;
  initial?: FormState;
  releaseControl?: React.ReactNode;
  submitLabel: string;
  busy: boolean;
  onSubmit: (form: FormState) => void;
}) => {
  const [questionText, setQuestionText] = useState(initial?.questionText ?? '');
  const [options, setOptions] = useState<[string, string, string, string]>(
    initial?.options ?? ['', '', '', ''],
  );
  const [correct, setCorrect] = useState<number[]>(initial?.correct ?? []);
  const [error, setError] = useState<string | null>(null);

  const setOption = (index: number, value: string) => {
    setOptions((previous) =>
      previous.map((option, position) => (position === index ? value : option)) as [
        string,
        string,
        string,
        string,
      ],
    );
    setError(null);
  };

  const toggleCorrect = (index: number) => {
    setCorrect((previous) =>
      previous.includes(index)
        ? previous.filter((entry) => entry !== index)
        : [...previous, index],
    );
    setError(null);
  };

  const handleSubmit = () => {
    const problem = validateForm(questionText, [...options], correct);
    if (problem) {
      setError(problem);
      return;
    }
    onSubmit({ questionText, options, correct: [...correct].sort((a, b) => a - b) });
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-text`}>Question</Label>
        <Input
          id={`${idPrefix}-text`}
          value={questionText}
          maxLength={500}
          placeholder="Which planet is known as the Red Planet?"
          autoComplete="off"
          disabled={busy}
          onChange={(event) => {
            setQuestionText(event.target.value);
            setError(null);
          }}
        />
      </div>

      <fieldset className="flex flex-col gap-2">
        <legend className="px-1 text-sm font-medium">
          Options — check every correct one
        </legend>
        {options.map((option, index) => {
          const inputId = `${idPrefix}-option-${index}`;
          const checkId = `${idPrefix}-correct-${index}`;
          return (
            <div key={index} className="flex items-center gap-2">
              <input
                id={checkId}
                type="checkbox"
                checked={correct.includes(index)}
                disabled={busy}
                onChange={() => toggleCorrect(index)}
                aria-label={`Mark option ${index + 1} as correct`}
                className="h-4 w-4 shrink-0 accent-rose-500"
              />
              <Label htmlFor={inputId} className="sr-only">
                Option {index + 1}
              </Label>
              <Input
                id={inputId}
                value={option}
                maxLength={200}
                placeholder={`Option ${index + 1}`}
                autoComplete="off"
                disabled={busy}
                onChange={(event) => setOption(index, event.target.value)}
              />
            </div>
          );
        })}
        <p className="text-xs text-muted-foreground">
          One checked option is a single-answer question; several checked is
          multiple-answer. The mode follows the checks — there is nothing else
          to configure.
        </p>
      </fieldset>

      {releaseControl}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={handleSubmit} disabled={busy}>
          {busy ? 'Saving...' : submitLabel}
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm font-medium text-destructive">
          {error}
        </p>
      )}
    </div>
  );
};

/**
 * The Trivia Drop console: the live question, drafts, scheduled releases, and
 * history -- plus this game's own availability (no reset cadence: questions
 * release individually, so the status controls' cadence picker does not apply
 * and is hidden for this game).
 *
 * Deploying ends the live question and activates the new one in the same
 * transaction; scheduling never ends anything early. Live and answered
 * questions are immutable -- corrections arrive as new questions, because
 * rewriting a live key would invalidate recorded correctness.
 */
export const AdminTriviaDropManage = () => {
  const { logActivity } = useGamesConfig();

  const [questions, setQuestions] = useState<TriviaQuestion[]>([]);
  const [live, setLive] = useState<TriviaQuestion | null>(null);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [releaseMode, setReleaseMode] = useState<'now' | 'scheduled' | 'draft'>('draft');
  const [releaseAt, setReleaseAt] = useState<string>(nowLocalInput);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [schedulingId, setSchedulingId] = useState<string | null>(null);
  const [scheduleAt, setScheduleAt] = useState<string>(nowLocalInput);
  const [rowBusy, setRowBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setQuestions(await getAdminTriviaQuestions());
      setListError(null);
    } catch (error) {
      setListError(describeApiError(error, 'Could not load the questions.'));
    } finally {
      setLoading(false);
    }
    try {
      setLive(await getCurrentAdminTriviaQuestion());
    } catch {
      setLive(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCreate = async (form: FormState) => {
    if (releaseMode === 'scheduled' && !releaseAt) {
      setCreateError('A scheduled release needs a release date and time (UTC).');
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createTriviaQuestion({
        question_text: form.questionText.trim(),
        options: form.options.map((option) => option.trim()),
        correct_indexes: form.correct,
        release_mode: releaseMode,
        ...(releaseMode === 'scheduled' ? { scheduled_for: new Date(`${releaseAt}:00Z`).toISOString() } : {}),
      });
      toast.success(
        created.status === 'active'
          ? 'Question deployed — it is live now.'
          : created.status === 'scheduled'
            ? `Question scheduled for ${utcLabel(created.scheduled_for)}.`
            : 'Draft saved.',
      );
      logActivity({
        kind: 'puzzle',
        gameId: 'trivia-drop',
        message: `Trivia Drop question ${created.status}: ${created.question_text.slice(0, 60)}`,
      });
      await refresh();
    } catch (error) {
      setCreateError(describeApiError(error, 'Could not save the question.'));
    } finally {
      setCreating(false);
    }
  };

  const act = async (label: string, run: () => Promise<TriviaQuestion>) => {
    setRowBusy(true);
    setRowError(null);
    try {
      const updated = await run();
      toast.success(`${label}: ${updated.status}.`);
      logActivity({
        kind: 'puzzle',
        gameId: 'trivia-drop',
        message: `Trivia Drop question ${updated.status}: ${updated.question_text.slice(0, 60)}`,
      });
      setEditingId(null);
      setSchedulingId(null);
      await refresh();
    } catch (error) {
      setRowError(describeApiError(error, `Could not ${label.toLowerCase()}.`));
    } finally {
      setRowBusy(false);
    }
  };

  const handleSaveEdit = async (id: string, form: FormState) => {
    setSavingEdit(true);
    try {
      await act('Draft updated', () =>
        updateTriviaDraft(id, {
          question_text: form.questionText.trim(),
          options: form.options.map((option) => option.trim()),
          correct_indexes: form.correct,
        }),
      );
    } finally {
      setSavingEdit(false);
    }
  };

  const drafts = questions.filter((question) => question.status === 'draft');
  const scheduled = questions.filter((question) => question.status === 'scheduled');
  const history = questions.filter(
    (question) => question.status === 'ended' || question.status === 'cancelled',
  );

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-2">
          <NavLink
            to="/admin/games/manage"
            className="inline-flex w-fit items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-3 w-3" />
            Back to Manage games
          </NavLink>
          <h1 className="text-3xl font-semibold">Trivia Drop questions</h1>
          <p className="text-sm text-muted-foreground">
            One live question at a time. Deploying ends the live question in
            the same transaction; scheduling never ends anything early.
          </p>
        </div>
        {live && <Badge>Live now</Badge>}
      </header>

      <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-xl">
            <CircleHelp className="h-5 w-5" />
            Trivia Drop
          </CardTitle>
          <CardDescription>
            Four options, one or more checked as correct. Availability follows
            the shared game controls; there is no reset cadence — questions
            release individually.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <AdminGameStatusControls gameId="trivia-drop" gameName="Trivia Drop" hideResetCadence />

          {live && (
            <section
              aria-label="Live question"
              className="rounded-xl border border-romantic/50 bg-romantic/5 p-4"
            >
              <p className="text-xs font-bold uppercase tracking-wider text-romantic">
                Live now — accepting answers
              </p>
              <p className="mt-2 font-semibold">{live.question_text}</p>
              <ul className="mt-2 flex flex-col gap-1 text-sm">
                {live.options.map((option) => (
                  <li key={option.option_index}>
                    {option.option_text}
                    {option.is_correct && (
                      <span className="ml-2 text-xs font-bold text-green-600">✓ correct</span>
                    )}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-muted-foreground">
                {live.answer_mode === 'single' ? 'Single-answer' : 'Multiple-answer'} · released{' '}
                {utcLabel(live.released_at)}
              </p>
            </section>
          )}

          <div className="flex flex-col gap-4">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              New question
            </h4>
            <QuestionForm
              idPrefix="trivia-new"
              submitLabel={
                releaseMode === 'now'
                  ? 'Deploy now'
                  : releaseMode === 'scheduled'
                    ? 'Schedule release'
                    : 'Save draft'
              }
              busy={creating}
              releaseControl={
                <div className="flex flex-col gap-2">
                  <div className="flex gap-2" role="group" aria-label="Release mode">
                    {(
                      [
                        ['draft', 'Draft'],
                        ['scheduled', 'Schedule release'],
                        ['now', 'Deploy now'],
                      ] as const
                    ).map(([mode, label]) => (
                      <Button
                        key={mode}
                        type="button"
                        variant={releaseMode === mode ? 'default' : 'outline'}
                        size="sm"
                        aria-pressed={releaseMode === mode}
                        onClick={() => {
                          setReleaseMode(mode);
                          setCreateError(null);
                        }}
                      >
                        {label}
                      </Button>
                    ))}
                  </div>
                  {releaseMode === 'scheduled' && (
                    <div className="flex flex-col gap-2">
                      <Label htmlFor="trivia-new-release">Release date and time (UTC)</Label>
                      <Input
                        id="trivia-new-release"
                        type="datetime-local"
                        value={releaseAt}
                        className="max-w-xs"
                        onChange={(event) => {
                          setReleaseAt(event.target.value);
                          setCreateError(null);
                        }}
                      />
                      <p className="text-xs text-muted-foreground">
                        Stored and released on UTC. The live question, if any,
                        stays live until this release actually activates.
                      </p>
                    </div>
                  )}
                  {releaseMode === 'now' && (
                    <p className="text-xs text-muted-foreground">
                      Deploying ends the currently live question immediately, in
                      the same transaction.
                    </p>
                  )}
                </div>
              }
              onSubmit={handleCreate}
            />
            {createError && (
              <p role="alert" className="text-sm font-medium text-destructive">
                {createError}
              </p>
            )}
          </div>

          {rowError && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {rowError}
            </p>
          )}

          <QuestionGroup
            title="Drafts"
            empty="No drafts. Saved drafts wait here until deployed or scheduled."
            questions={drafts}
            loading={loading}
            listError={listError}
            renderActions={(question) => (
              <>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={rowBusy}
                  onClick={() => {
                    setEditingId(editingId === question.id ? null : question.id);
                    setRowError(null);
                  }}
                >
                  {editingId === question.id ? 'Close editor' : 'Edit'}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={rowBusy}
                  onClick={() => {
                    setSchedulingId(schedulingId === question.id ? null : question.id);
                    setScheduleAt(utcInputValue(question.scheduled_for));
                    setRowError(null);
                  }}
                >
                  Schedule…
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={rowBusy}
                  onClick={() => void act('Question deployed', () => deployTriviaQuestion(question.id))}
                >
                  Deploy now
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={rowBusy}
                  aria-label={`Cancel draft ${question.question_text.slice(0, 30)}`}
                  onClick={() => void act('Draft cancelled', () => cancelTriviaQuestion(question.id))}
                >
                  <X />
                </Button>
              </>
            )}
            renderExpanded={(question) => (
              <>
                {editingId === question.id && (
                  <div className="rounded-xl border border-border/60 bg-muted/40 p-4">
                    <QuestionForm
                      idPrefix={`trivia-edit-${question.id}`}
                      initial={{
                        questionText: question.question_text,
                        options: [
                          question.options[0]?.option_text ?? '',
                          question.options[1]?.option_text ?? '',
                          question.options[2]?.option_text ?? '',
                          question.options[3]?.option_text ?? '',
                        ],
                        correct: [...question.correct_indexes],
                      }}
                      submitLabel="Save changes"
                      busy={savingEdit}
                      onSubmit={(form) => void handleSaveEdit(question.id, form)}
                    />
                  </div>
                )}
                {schedulingId === question.id && (
                  <div className="flex flex-wrap items-end gap-3 rounded-xl border border-border/60 bg-muted/40 p-4">
                    <div className="flex flex-col gap-2">
                      <Label htmlFor={`trivia-schedule-${question.id}`}>
                        Release date and time (UTC)
                      </Label>
                      <Input
                        id={`trivia-schedule-${question.id}`}
                        type="datetime-local"
                        value={scheduleAt}
                        onChange={(event) => setScheduleAt(event.target.value)}
                      />
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      disabled={rowBusy || !scheduleAt}
                      onClick={() =>
                        void act('Release scheduled', () =>
                          scheduleTriviaQuestion(
                            question.id,
                            new Date(`${scheduleAt}:00Z`).toISOString(),
                          ),
                        )
                      }
                    >
                      Schedule
                    </Button>
                  </div>
                )}
              </>
            )}
          />

          <QuestionGroup
            title="Scheduled"
            empty="Nothing scheduled. A scheduled release never ends the live question early."
            questions={scheduled}
            loading={loading}
            listError={listError}
            renderActions={(question) => (
              <>
                <span className="text-xs text-muted-foreground">
                  Releases {utcLabel(question.scheduled_for)}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={rowBusy}
                  onClick={() => {
                    setSchedulingId(schedulingId === question.id ? null : question.id);
                    setScheduleAt(utcInputValue(question.scheduled_for));
                    setRowError(null);
                  }}
                >
                  Reschedule…
                </Button>
                <Button
                  type="button"
                  size="sm"
                  disabled={rowBusy}
                  onClick={() => void act('Question deployed', () => deployTriviaQuestion(question.id))}
                >
                  Deploy now
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={rowBusy}
                  aria-label={`Cancel scheduled release ${question.question_text.slice(0, 30)}`}
                  onClick={() => void act('Release cancelled', () => cancelTriviaQuestion(question.id))}
                >
                  <X />
                </Button>
              </>
            )}
            renderExpanded={(question) =>
              schedulingId === question.id ? (
                <div className="flex flex-wrap items-end gap-3 rounded-xl border border-border/60 bg-muted/40 p-4">
                  <div className="flex flex-col gap-2">
                    <Label htmlFor={`trivia-reschedule-${question.id}`}>
                      New release date and time (UTC)
                    </Label>
                    <Input
                      id={`trivia-reschedule-${question.id}`}
                      type="datetime-local"
                      value={scheduleAt}
                      onChange={(event) => setScheduleAt(event.target.value)}
                    />
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    disabled={rowBusy || !scheduleAt}
                    onClick={() =>
                      void act('Release rescheduled', () =>
                        rescheduleTriviaQuestion(
                          question.id,
                          new Date(`${scheduleAt}:00Z`).toISOString(),
                        ),
                      )
                    }
                  >
                    Save new time
                  </Button>
                </div>
              ) : null
            }
          />

          <QuestionGroup
            title="History"
            empty="No ended questions yet."
            questions={history}
            loading={loading}
            listError={listError}
            renderActions={(question) => (
              <span className="text-xs text-muted-foreground">
                {question.status === 'ended'
                  ? `Ended ${utcLabel(question.ended_at)}`
                  : 'Cancelled'}
              </span>
            )}
          />
        </CardContent>
      </Card>
    </div>
  );
};

const QuestionGroup = ({
  title,
  empty,
  questions,
  loading,
  listError,
  renderActions,
  renderExpanded,
}: {
  title: string;
  empty: string;
  questions: TriviaQuestion[];
  loading: boolean;
  listError: string | null;
  renderActions: (question: TriviaQuestion) => React.ReactNode;
  renderExpanded?: (question: TriviaQuestion) => React.ReactNode;
}) => (
  <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
    <div className="flex items-center justify-between gap-3">
      <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h4>
      <span className="text-xs text-muted-foreground">
        {loading ? 'Loading...' : `${questions.length}`}
      </span>
    </div>
    {listError && (
      <p role="alert" className="text-sm font-medium text-destructive">
        {listError}
      </p>
    )}
    {!loading && !listError && questions.length === 0 && (
      <p className="text-sm text-muted-foreground">{empty}</p>
    )}
    {questions.length > 0 && (
      <ul className="flex flex-col gap-2">
        {questions.map((question) => (
          <li
            key={question.id}
            className="flex flex-col gap-2 rounded-xl border border-border/60 p-3"
          >
            <div className="flex flex-wrap items-center gap-3">
              <span className="min-w-0 flex-1 text-sm font-medium">{question.question_text}</span>
              <Badge variant="outline">
                {question.answer_mode === 'single' ? 'Single-answer' : 'Multiple-answer'}
              </Badge>
              <Badge variant="outline">
                {question.correct_indexes.length} correct
              </Badge>
            </div>
            <ul className="flex flex-col gap-0.5 text-sm text-muted-foreground">
              {question.options.map((option) => (
                <li key={option.option_index}>
                  {option.option_text}
                  {option.is_correct && (
                    <span className="ml-2 text-xs font-bold text-green-600">✓ correct</span>
                  )}
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-2">{renderActions(question)}</div>
            {renderExpanded?.(question)}
          </li>
        ))}
      </ul>
    )}
  </div>
);

export default AdminTriviaDropManage;
