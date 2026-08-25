import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import axios from 'axios';
import SessionDetail from '../SessionDetail';
import { getSession, streamUrl } from '../../lib/api';

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

beforeEach(() => {
  jest.clearAllMocks();
  eventSources = [];
  window.EventSource = MockEventSource;
  streamUrl.mockImplementation(sid => `/api/sessions/${sid}/stream`);
  getSession.mockResolvedValue({
    status: 'awaiting_human_review',
    files: [],
  });
  axios.get.mockImplementation(url => {
    if (url.includes('/results')) {
      return Promise.resolve({
        data: {
          decisions: [{
            file_id: 'dataset-1',
            column: 'subject_id',
            action: 'human_review',
            suggested_action: 'drop',
          }],
        },
      });
    }
    if (url.includes('/agent-trace')) return Promise.resolve({ data: { messages: [], next_cursor: null } });
    return Promise.reject(new Error(`Unexpected GET: ${url}`));
  });
});

afterEach(() => {
  jest.useRealTimers();
  delete window.EventSource;
});

test('reconnects the EventSource after a stream error', async () => {
  render(<SessionDetail />);

  await screen.findByTestId('human-review-panel');
  await waitFor(() => expect(eventSources).toHaveLength(1));
  const failedStream = eventSources[0];

  jest.useFakeTimers();
  act(() => failedStream.onerror(new Event('error')));

  expect(failedStream.close).toHaveBeenCalledTimes(1);
  expect(eventSources).toHaveLength(1);

  act(() => jest.advanceTimersByTime(1000));

  expect(eventSources).toHaveLength(2);
  expect(eventSources[1].url).toBe('/api/sessions/session-1/stream');
});
