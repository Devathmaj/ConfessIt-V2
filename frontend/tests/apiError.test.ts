import { describe, expect, it } from 'vitest';
import { describeApiError, describeValidationDetail } from '@/lib/apiError';

/**
 * A 422 from FastAPI is the case that matters here.
 *
 * The admin payloads set `extra="forbid"` and cap every identity field, so a rejected admin
 * create or edit is an ordinary outcome rather than a rare one. Its `detail` is an array of
 * Pydantic error objects, and passing that array straight to a toast asks React to render
 * plain objects as children -- which unmounts the view. These tests pin the text that reaches
 * the toast instead, because the failure they guard against is a blank page and not a message.
 */
const pydanticError = (
  loc: (string | number)[],
  msg: string,
): { loc: (string | number)[]; msg: string } => ({ loc, msg });

describe('describeValidationDetail', () => {
  it('returns a plain string detail unchanged', () => {
    expect(describeValidationDetail('You cannot unblock yourself.')).toBe(
      'You cannot unblock yourself.',
    );
  });

  it('turns a 422 error array into one readable sentence', () => {
    const detail = describeValidationDetail([
      pydanticError(['body', 'regno'], 'String should have at most 100 characters'),
    ]);

    expect(detail).toBe('regno: String should have at most 100 characters');
  });

  it('names every failed field rather than stopping at the first', () => {
    // An admin create can be wrong in more than one way at once, and one field per round-trip
    // teaches the operator to fix and resubmit repeatedly for something they could have been
    // told once.
    const detail = describeValidationDetail([
      pydanticError(['body', 'regno'], 'String should have at most 100 characters'),
      pydanticError(['body', 'department'], 'String should have at most 80 characters'),
    ]);

    expect(detail).toContain('regno:');
    expect(detail).toContain('department:');
  });

  it('names the field an extra key was refused on', () => {
    // The `extra="forbid"` case. Without the field name the operator is told only that
    // something was refused, which is the ambiguity this whole change set out to remove.
    const detail = describeValidationDetail([
      pydanticError(['body', 'bio'], 'Extra inputs are not permitted'),
      pydanticError(['body', 'is_blocked'], 'Extra inputs are not permitted'),
    ]);

    expect(detail).toBe(
      'bio: Extra inputs are not permitted; is_blocked: Extra inputs are not permitted',
    );
  });

  it('drops the transport segment so the path reads as the field the operator typed', () => {
    expect(describeValidationDetail([pydanticError(['body', 'regno'], 'required')])).toBe(
      'regno: required',
    );
    expect(describeValidationDetail([pydanticError(['query', 'limit'], 'bad')])).toBe(
      'limit: bad',
    );
  });

  it('keeps a nested path so the reader knows which object is at fault', () => {
    expect(
      describeValidationDetail([
        pydanticError(['body', 'preferences', 'notifications_enabled'], 'bad'),
      ]),
    ).toBe('preferences.notifications_enabled: bad');
  });

  it('strips the developer-facing "Value error," prefix', () => {
    // A model validator's message arrives prefixed for a developer reading a traceback. Shown
    // to an operator it reads as though the page had a bug.
    expect(
      describeValidationDetail([
        pydanticError(
          ['body'],
          'Value error, Year and department are required for a participant account',
        ),
      ]),
    ).toBe('Year and department are required for a participant account');
  });

  it('reports a message-only error even without a location', () => {
    expect(describeValidationDetail([{ msg: 'Not allowed' }])).toBe('Not allowed');
  });

  it('collapses a repeated cause on one field to a single mention', () => {
    // Too long and the wrong character are separate errors on the same field; repeating the
    // field name just makes the toast longer without adding information.
    const detail = describeValidationDetail([
      pydanticError(['body', 'name'], 'bad'),
      pydanticError(['body', 'name'], 'bad'),
    ]);

    expect(detail).toBe('name: bad');
  });

  it('reports nothing usable for an empty array rather than an empty string', () => {
    expect(describeValidationDetail([])).toBeNull();
  });

  it('reports nothing for a detail that is missing or the wrong shape', () => {
    expect(describeValidationDetail(undefined)).toBeNull();
    expect(describeValidationDetail('   ')).toBeNull();
  });
});

describe('describeApiError', () => {
  it('returns a plain string detail from a raised HTTPException', () => {
    const error = { response: { data: { detail: 'Account not found.' } } };

    expect(describeApiError(error, 'fallback')).toBe('Account not found.');
  });

  it('renders a 422 body as text, which is the whole point', () => {
    const error = {
      response: {
        data: {
          detail: [pydanticError(['body', 'email'], 'value is not a valid email address: An email address must have an @-sign.')],
        },
      },
    };

    const described = describeApiError(error, 'Unable to create this account.');

    expect(described).toBe(
      'email: value is not a valid email address: An email address must have an @-sign.',
    );
    // The assertion that the caller cannot make for itself: whatever comes back must be
    // renderable as a child.
    expect(typeof described).toBe('string');
  });

  it('falls back rather than showing a proxy HTML error page', () => {
    // A 502 from a proxy in front of the app arrives as HTML. Rendered into a toast it is an
    // unreadable wall of markup.
    const error = { response: { data: { detail: '<html><body>Bad Gateway</body></html>' } } };

    expect(describeApiError(error, 'Unable to create this account.')).toBe(
      'Unable to create this account.',
    );
  });

  it('falls back on a network failure, where there is no response at all', () => {
    expect(describeApiError(new Error('Network Error'), 'Unable to create this account.')).toBe(
      'Unable to create this account.',
    );
  });

  it('uses the error message when the server sent no detail', () => {
    expect(describeApiError({ message: 'Request timed out' }, 'fallback')).toBe(
      'Request timed out',
    );
  });

  it('falls back for a non-object, so a thrown string is still handled', () => {
    expect(describeApiError('something went wrong', 'fallback')).toBe('fallback');
    expect(describeApiError(null, 'fallback')).toBe('fallback');
    expect(describeApiError(undefined, 'fallback')).toBe('fallback');
  });

  it('falls back when detail is an object rather than a string or an array', () => {
    expect(describeApiError({ response: { data: { detail: { code: 7 } } } }, 'fallback')).toBe(
      'fallback',
    );
  });

  it('always produces a non-empty string, whatever it is handed', () => {
    // The invariant every call site depends on: `toast.error` is never given undefined.
    const inputs: unknown[] = [
      undefined,
      null,
      0,
      '',
      [],
      {},
      { response: {} },
      { response: { data: {} } },
      { response: { data: { detail: [] } } },
    ];

    for (const input of inputs) {
      expect(describeApiError(input, 'fallback')).toBe('fallback');
    }
  });
});