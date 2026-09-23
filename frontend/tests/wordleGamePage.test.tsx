import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { toast } from 'sonner';

import { WordleGamePage } from '@/pages/WordleGamePage';
import type { WordleCurrentGame, WordleParticipation } from '@/services/api';

/**
 * The daily Wordle screen against its API boundary.
 *
 * The contract pinned here is the one the player experiences: the board renders whatever the
 * server remembers, a guess goes to the server as one lowercase five-letter word, and a word
 * the server refuses is refused *visibly* -- the letters stay, the row shakes, a reason is
 * said -- instead of silently vanishing. The screen keeps the full local state from
 * `GET /games/wordle` (participation may be null for a player who has not started), and a
 * successful guess replaces that state with the POST's response, so the board a returning
 * player opens is always the board the last guess left behind.
 */

vi.mock('@/services/api', () => ({
    getCurrentWordle: vi.fn(),
    submitWordleGuess: vi.fn(),
}));

vi.mock('sonner', () => ({
    toast: Object.assign(vi.fn(), {
        success: vi.fn(),
        error: vi.fn(),
        info: vi.fn(),
        warning: vi.fn(),
    }),
    Toaster: () => null,
}));

const { getCurrentWordle, submitWordleGuess } = await import('@/services/api');

const makeParticipation = (overrides: Partial<WordleParticipation> = {}): WordleParticipation => ({
    wordle_game_id: 'game-1',
    game_date: '2026-10-07',
    status: 'in_progress',
    success: false,
    attempt_count: 0,
    max_attempts: 6,
    guesses: [],
    results: [],
    started_at: '2026-10-07T00:00:00Z',
    last_guess_at: null,
    completed_at: null,
    solved_at: null,
    answer: null,
    ...overrides,
});

const makeCurrentGame = (overrides: Partial<WordleCurrentGame> = {}): WordleCurrentGame => ({
    game_id: 'game-1',
    game_date: '2026-10-07',
    participation: makeParticipation(),
    max_attempts: 6,
    word_length: 5,
    ...overrides,
});

const renderGame = () =>
    render(
        <MemoryRouter initialEntries={['/games/wordle']}>
            <Routes>
                <Route path="/games/wordle" element={<WordleGamePage />} />
                <Route path="/games" element={<div>games home</div>} />
            </Routes>
        </MemoryRouter>
    );

/** The tile for one letter, by its position in `rowIndex` and `columnIndex`. */
const tile = (rowIndex: number, columnIndex: number) =>
    within(screen.getAllByRole('row')[rowIndex]).getAllByRole('gridcell')[columnIndex];

describe('opening the board', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('renders the empty board from the shape the server sends', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());

        renderGame();

        expect(await screen.findByRole('grid', { name: 'Wordle board' })).toBeInTheDocument();
        // max_attempts rows, word_length tiles each -- drawn from the response, not hardcoded
        expect(screen.getAllByRole('row')).toHaveLength(6);
        expect(within(screen.getAllByRole('row')[0]).getAllByRole('gridcell')).toHaveLength(5);
        expect(screen.getByTestId('attempt-label')).toHaveTextContent('0 of 6 guesses used');
    });

    it('shows the puzzle is missing rather than a broken board', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame({ game_id: null, game_date: null, participation: null }));

        renderGame();

        expect(await screen.findByText('No puzzle available today.')).toBeInTheDocument();
        expect(screen.queryByRole('grid', { name: 'Wordle board' })).not.toBeInTheDocument();
    });

    it('resumes the exact board the player left behind', async () => {
        const participation = makeParticipation({
            attempt_count: 2,
            guesses: ['abaca', 'cigar'],
            results: [
                ['absent', 'correct', 'absent', 'absent', 'absent'],
                ['correct', 'correct', 'correct', 'correct', 'correct'],
            ],
        });
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame({ participation }));

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        expect(tile(0, 1)).toHaveTextContent('B');
        expect(tile(0, 1)).toHaveAttribute('data-verdict', 'correct');
        expect(tile(1, 3)).toHaveAttribute('data-verdict', 'correct');
        expect(screen.getByTestId('attempt-label')).toHaveTextContent('2 of 6 guesses used');
    });
});

describe('typing and submitting a guess', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    const typeWord = (word: string) => {
        for (const key of word) fireEvent.keyDown(window, { key });
    };

    it('types onto the row the player is currently on', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        typeWord('cigar');
        expect(tile(0, 0)).toHaveTextContent('C');
        expect(tile(0, 4)).toHaveTextContent('R');
        // It is the top row for a fresh player; no read-ahead of a fourth row.
        expect(tile(1, 0)).toHaveTextContent('');
    });

    it('sends the guess lowercase, to the puzzle the player was shown', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());
        vi.mocked(submitWordleGuess).mockResolvedValue(
            makeParticipation({ attempt_count: 1, guesses: ['cigar'] }) as never
        );

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        typeWord('cigar');
        fireEvent.keyDown(window, { key: 'Enter' });

        await waitFor(() =>
            expect(submitWordleGuess).toHaveBeenCalledWith('game-1', 'cigar')
        );
    });

    it('starts a fresh player with no need to pre-exist: participation may be null', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(
            makeCurrentGame({ participation: null })
        );
        vi.mocked(submitWordleGuess).mockResolvedValue(
            makeParticipation({ attempt_count: 1, guesses: ['abaca'] }) as never
        );

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        typeWord('abaca');
        fireEvent.keyDown(window, { key: 'Enter' });

        await waitFor(() =>
            expect(submitWordleGuess).toHaveBeenCalledWith('game-1', 'abaca')
        );
    });

    it('replaces the board with the server response after a successful guess', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());
        vi.mocked(submitWordleGuess).mockResolvedValue(
            makeParticipation({
                attempt_count: 1,
                status: 'won',
                success: true,
                guesses: ['cigar'],
                results: [['correct', 'correct', 'correct', 'correct', 'correct']],
                completed_at: '2026-10-07T00:01:00Z',
                solved_at: '2026-10-07T00:01:00Z',
                answer: 'cigar',
            }) as never
        );

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        typeWord('cigar');
        fireEvent.keyDown(window, { key: 'Enter' });

        expect(await screen.findByText('Solved')).toBeInTheDocument();
        expect(screen.getByText('cigar')).toBeInTheDocument();
        expect(tile(0, 0)).toHaveAttribute('data-verdict', 'correct');
    });

    it('keeps the letters on an on-screen keyboard press rather than a physical key', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());
        vi.mocked(submitWordleGuess).mockResolvedValue(makeParticipation({}) as never);

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        for (const key of ['C', 'I', 'G', 'A', 'R']) {
            fireEvent.click(screen.getByRole('button', { name: key }));
        }
        expect(tile(0, 0)).toHaveTextContent('C');
        expect(tile(0, 4)).toHaveTextContent('R');
    });
});

describe('a refused guess', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('keeps the letters, shakes the row and says why', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());
        vi.mocked(submitWordleGuess).mockRejectedValue({
            response: { data: { detail: 'That is not a word on our list.' } },
        });

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        for (const key of ['z', 'z', 'z', 'z', 'z']) fireEvent.keyDown(window, { key });
        fireEvent.keyDown(window, { key: 'Enter' });

        await waitFor(() =>
            expect(toast.info).toHaveBeenCalledWith('That is not a word on our list.')
        );
        expect(tile(0, 0)).toHaveTextContent('Z');
        expect(tile(0, 4)).toHaveTextContent('Z');
        await waitFor(() =>
            expect(screen.getAllByRole('row')[0]).toHaveClass('animate-wordle-shake')
        );
    });

    it('refuses too-short a word without calling the server', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(makeCurrentGame());

        renderGame();
        await screen.findByRole('grid', { name: 'Wordle board' });

        for (const key of ['c', 'a', 't']) fireEvent.keyDown(window, { key });
        fireEvent.keyDown(window, { key: 'Enter' });

        await waitFor(() => expect(toast.info).toHaveBeenCalledWith('Not enough letters.'));
        expect(submitWordleGuess).not.toHaveBeenCalled();
        expect(tile(0, 0)).toHaveTextContent('C');
    });
});

describe('a finished game', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('reveals the answer overlay and refuses further guesses', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(
            makeCurrentGame({
                participation: makeParticipation({
                    status: 'lost',
                    success: false,
                    attempt_count: 6,
                    guesses: ['cigar', 'abbey', 'medal', 'reply', 'rhyme', 'tease'],
                    results: [],
                    completed_at: '2026-10-07T00:01:00Z',
                    solved_at: null,
                    answer: 'abbey',
                }),
            })
        );

        renderGame();
        expect(await screen.findByText('Out of guesses')).toBeInTheDocument();
        expect(screen.getByText('abbey')).toBeInTheDocument();

        for (const key of ['a', 'b', 'a', 'c', 'a']) fireEvent.keyDown(window, { key });
        fireEvent.keyDown(window, { key: 'Enter' });

        expect(submitWordleGuess).not.toHaveBeenCalled();
    });

    it('offers the way back to the Games section', async () => {
        vi.mocked(getCurrentWordle).mockResolvedValue(
            makeCurrentGame({
                participation: makeParticipation({
                    status: 'won',
                    success: true,
                    attempt_count: 2,
                    guesses: ['abaca', 'cigar'],
                    results: [],
                    completed_at: '2026-10-07T00:01:00Z',
                    solved_at: '2026-10-07T00:01:00Z',
                    answer: 'cigar',
                }),
            })
        );

        renderGame();
        (await screen.findAllByText('cigar')).forEach((node) => {
            expect(node).toBeInTheDocument();
        });
        const back = screen.getAllByRole('link', { name: 'Back to Games' })[0];
        fireEvent.click(back);
        expect(screen.getByText('games home')).toBeInTheDocument();
    });
});