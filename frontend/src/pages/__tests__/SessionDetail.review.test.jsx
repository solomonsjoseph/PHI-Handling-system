import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axios from 'axios';
import SessionDetail from '../SessionDetail';
import { getSession } from '../../lib/api';

jest.mock('axios');
jest.mock('sonner', () => ({
  toast: { error: jest.fn(), info: jest.fn(), success: jest.fn() },
}));
jest.mock('react-router-dom', () => ({
  useNavigate: () => jest.fn(),
  useParams: () => ({ sid: 'session-1' }),
  useSearchParams: () => [{ get: () => null }],
}));
jest.mock('../../lib/api', () => ({
  API: '/api',
  getSession: jest.fn(),
  streamUrl: jest.fn(sid => `/api/sessions/${sid}/stream`),
}));

let eventSources;

class MockEventSource {
  constructor(url) {
    this.url = url;
    this.close = jest.fn();
    eventSources.push(this);
  }
}

const humanReviewDecision = (column, overrides = {}) => ({
  file_id: 'dataset-1',
  column,
  action: 'human_review',
  reason: `Review ${column}`,
  suggested_action: 'drop',
  ...overrides,
});

function renderDetail(decisions) {
  getSession.mockResolvedValue({
    status: 'awaiting_human_review',
    files: [],
  });
  axios.get.mockImplementation(url => {
    if (url.includes('/results')) return Promise.resolve({ data: { decisions } });
    if (url.includes('/agent-trace')) return Promise.resolve({ data: { messages: [], next_cursor: null } });
    return Promise.reject(new Error(`Unexpected GET: ${url}`));
  });
  axios.post.mockResolvedValue({ data: { status: 'submitted' } });

  return render(<SessionDetail />);
}

beforeEach(() => {
  jest.clearAllMocks();
  eventSources = [];
  window.EventSource = MockEventSource;
});

afterEach(() => {
  delete window.EventSource;
});

test('renders one review row for every human-review decision', async () => {
  renderDetail([
    humanReviewDecision('subject_id'),
    humanReviewDecision('visit_date'),
    { file_id: 'dataset-1', column: 'study_id', action: 'keep' },
  ]);

  await screen.findByTestId('human-review-panel');

  expect(screen.getByTestId('review-row-subject_id')).toBeInTheDocument();
  expect(screen.getByTestId('review-row-visit_date')).toBeInTheDocument();
  expect(screen.queryByTestId('review-row-study_id')).not.toBeInTheDocument();
});

test('blocks review submission until every review row has a mode', async () => {
  const user = userEvent.setup();
  renderDetail([
    humanReviewDecision('subject_id'),
    humanReviewDecision('visit_date'),
  ]);

  await screen.findByTestId('human-review-panel');
  const submit = screen.getByTestId('btn-submit-human-review');

  expect(submit).toBeDisabled();
  await user.type(screen.getByTestId('reviewer-id'), 'reviewer-1');
  await user.click(screen.getByTestId('btn-approve-subject_id'));

  expect(submit).toBeDisabled();

  await user.click(screen.getByTestId('btn-defer-visit_date'));

  expect(submit).toBeEnabled();
});

test('requires explicit confirmation before a pending comment interpretation can be submitted', async () => {
  const user = userEvent.setup();
  renderDetail([
    humanReviewDecision('patient_identifier', {
      pending_confirmation: { action: 'drop', reason: 'Direct identifier' },
      reviewer_comment: 'Please remove this value.',
    }),
  ]);

  await screen.findByTestId('review-row-confirm-patient_identifier');
  await user.type(screen.getByTestId('reviewer-id'), 'reviewer-1');
  await user.click(screen.getByTestId('actual-knowledge-ack'));

  const submit = screen.getByTestId('btn-submit-human-review');
  expect(submit).toBeDisabled();
  expect(axios.post).not.toHaveBeenCalled();

  await user.click(screen.getByTestId('btn-confirm-patient_identifier'));

  expect(submit).toBeEnabled();
  await user.click(submit);

  await waitFor(() => {
    expect(axios.post).toHaveBeenCalledWith(
      '/api/sessions/session-1/human-review',
      expect.objectContaining({
        resolutions: [{ file_id: 'dataset-1', column: 'patient_identifier', mode: 'approve', comment: '' }],
        client_event_id: expect.any(String),
      }),
    );
  });
});

test('refetches session state when the stream delivers an event', async () => {
  renderDetail([humanReviewDecision('subject_id')]);

  await screen.findByTestId('human-review-panel');
  await waitFor(() => expect(eventSources).toHaveLength(1));
  getSession.mockClear();

  eventSources[0].onmessage({ data: '{"phase":"review"}' });

  await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
});
