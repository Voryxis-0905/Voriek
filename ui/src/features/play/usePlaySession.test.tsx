import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { usePlaySession } from './usePlaySession';
import { WORLD_RESTORED_EVENT } from './types';

const mocks = vi.hoisted(() => ({
  api: {
    play: {
      state: vi.fn(),
      getChapters: vi.fn(),
      continue: vi.fn(),
      start: vi.fn(),
      regenerate: vi.fn(),
      previewTimeSkip: vi.fn(),
      executeTimeSkip: vi.fn(),
      prelude: { generate: vi.fn(), confirm: vi.fn(), regenerate: vi.fn() },
    },
    map: { status: vi.fn(), get: vi.fn(), previewTravel: vi.fn() },
    worlds: { affinityGraph: vi.fn(), updateConfig: vi.fn() },
    discovery: { questBoard: vi.fn(), journal: vi.fn() },
  },
}));

vi.mock('../../api/client', () => ({ api: mocks.api }));

const api = mocks.api;

function playState(overrides: Record<string, any> = {}) {
  return {
    protagonist: { id: 'char_xueli', name: 'Xueli', location: 'Sect', power_stat: {}, traits: {}, knowledge_flags: [], inventory: [], alive: true, relationships: {}, age: '' },
    arc_progress: { current_checkpoint_id: 'cp_0', current_index: 0, total_checkpoints: 1, completed: [] },
    unlocked_cards: [],
    story_clock: {},
    foreshadowing_tracker: [],
    style_card: {},
    output_length: 'Standard',
    ...overrides,
  };
}

function chapter(overrides: Record<string, any> = {}) {
  return { chapter_index: 1, turn_index: 1, chapter_closed: false, chapter_title: null, checkpoint_id: 'cp_0', user_input: 'go', chapter_text: 'The road opens.', ...overrides };
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  api.play.state.mockResolvedValue(playState());
  api.play.getChapters.mockResolvedValue([]);
  api.play.continue.mockResolvedValue({ chapter: chapter() });
  api.play.start.mockResolvedValue({ chapter: chapter() });
  api.play.regenerate.mockResolvedValue({});
  api.play.previewTimeSkip.mockResolvedValue({ requested_minutes: 1440, granted_minutes: 180, requested_ticks: 24, granted_ticks: 3, start_tick: 0, end_tick: 3, warnings: [{ kind: 'known_deadline', event_id: 'storm', title: 'Storm', deadline_tick: 3, ticks_away: 3 }], will_interrupt: true, requires_confirmation: true, activity: 'Study', forced: false });
  api.play.executeTimeSkip.mockResolvedValue({ chapter: chapter({ user_input: 'Time skip 1 days: Study' }), revision: 2 });
  api.play.prelude.generate.mockResolvedValue({});
  api.play.prelude.confirm.mockResolvedValue({});
  api.map.status.mockResolvedValue({ locations: [] });
  api.map.get.mockResolvedValue({ locations: [] });
  api.map.previewTravel.mockResolvedValue({ status: 'available', destination: 'Old Forest', route: ['Sect', 'Old Forest'], legs: [], elapsed_minutes: 90, estimated_ticks: 2, risk: { level: 'low', known_tags: [] } });
  api.worlds.affinityGraph.mockResolvedValue({ nodes: [], edges: [] });
  api.worlds.updateConfig.mockResolvedValue({});
  api.discovery.questBoard.mockResolvedValue({ quests: [], enabled: true });
  api.discovery.journal.mockResolvedValue({ entries: [] });
});

function render(world = 'WorldA') {
  return renderHook(({ name }) => usePlaySession(name), { initialProps: { name: world } });
}

describe('usePlaySession input/retry loop', () => {
  it('starts Chapter 1 when a confirmed prelude already advanced the checkpoint', async () => {
    api.play.state.mockResolvedValue(playState({
      prelude_confirmed: true,
      arc_progress: { current_checkpoint_id: 'cp_1', current_index: 1, total_checkpoints: 5, completed: ['cp_0'] },
    }));
    api.play.getChapters.mockResolvedValue([chapter({ chapter_index: 0, chapter_text: 'Prelude', checkpoint_id: 'cp_0' })]);
    const { result } = render();
    await waitFor(() => expect(result.current.playState?.prelude_confirmed).toBe(true));

    await act(async () => { await result.current.handleStartChapter(); });

    expect(api.play.start).toHaveBeenCalledWith('WorldA', { opening_mode: 'ai_generate' });
    expect(result.current.turns).toHaveLength(1);
  });

  it('previews and executes a time skip without overwriting typed action text', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());
    act(() => result.current.setInput('keep this action'));
    const request: any = { amount: 1, unit: 'days', activity: 'Study', interruption_policy: 'important_events' };
    await act(async () => { await result.current.handlePreviewTimeSkip(request); });
    expect(result.current.timeSkipPreview?.granted_minutes).toBe(180);
    expect(result.current.input).toBe('keep this action');
    await act(async () => { await result.current.handleExecuteTimeSkip(request); });
    expect(api.play.executeTimeSkip).toHaveBeenCalledWith('WorldA', expect.objectContaining({ activity: 'Study', expected_revision: undefined }));
    expect(result.current.input).toBe('keep this action');
  });
  it('turns a map destination into an editable travel intent', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());
    act(() => result.current.handleTravelTo({ id: 'forest', name: 'Old Forest' }));
    expect(result.current.input).toBe('Travel to Old Forest.');
    expect(result.current.activeDrawer).toBeNull();
    expect(api.play.continue).not.toHaveBeenCalled();
  });

  it('previews travel without sending or changing the input', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());
    act(() => result.current.setInput('keep this draft'));
    await act(async () => { await result.current.handlePreviewTravel({ id: 'forest', name: 'Old Forest' }); });
    expect(api.map.previewTravel).toHaveBeenCalledWith('WorldA', 'Old Forest');
    expect(result.current.travelPreview?.elapsed_minutes).toBe(90);
    expect(result.current.input).toBe('keep this draft');
    expect(api.play.continue).not.toHaveBeenCalled();
  });

  it('turns an inventory command into editable input without mutating state', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());
    act(() => result.current.handleItemAction('Equip', { instance_id: 'blade_1', name: 'Moon Blade' } as any));
    expect(result.current.input).toBe('Equip Moon Blade.');
    expect(api.play.continue).not.toHaveBeenCalled();
  });

  it('keeps the typed input when a send fails and retries exactly once', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    api.play.continue.mockRejectedValueOnce(new Error('network down'));
    act(() => result.current.setInput('look around the altar'));
    await act(async () => { await result.current.handleSend(); });

    expect(api.play.continue).toHaveBeenCalledTimes(1);
    expect(result.current.input).toBe('look around the altar');
    expect(result.current.error).toMatch(/network down/);

    await act(async () => { await result.current.handleSend(); });
    expect(api.play.continue).toHaveBeenCalledTimes(2);
    expect(result.current.input).toBe('');
    expect(result.current.turns).toHaveLength(1);
  });

  it('does not send twice when the button is clicked again while in flight', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    let release: (value: any) => void = () => {};
    api.play.continue.mockImplementation(() => new Promise((resolve) => { release = resolve; }));

    act(() => result.current.setInput('run forward'));
    await act(async () => { result.current.handleSend(); });
    await act(async () => { result.current.handleSend(); });

    expect(api.play.continue).toHaveBeenCalledTimes(1);
    await act(async () => { release({ chapter: chapter() }); });
  });

  it('keeps newer typing instead of overwriting it after a successful send', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    let release: (value: any) => void = () => {};
    api.play.continue.mockImplementation(() => new Promise((resolve) => { release = resolve; }));

    act(() => result.current.setInput('first action'));
    await act(async () => { result.current.handleSend(); });
    act(() => result.current.setInput('next action typed while waiting'));
    await act(async () => { release({ chapter: chapter() }); });

    expect(result.current.input).toBe('next action typed while waiting');
  });
});

describe('usePlaySession world isolation', () => {
  it('ignores a response from the previous world after switching', async () => {
    const { result, rerender } = render('WorldA');
    await waitFor(() => expect(api.play.state).toHaveBeenCalledWith('WorldA'));

    let release: (value: any) => void = () => {};
    api.play.continue.mockImplementation(() => new Promise((resolve) => { release = resolve; }));

    act(() => result.current.setInput('act in A'));
    await act(async () => { result.current.handleSend(); });

    rerender({ name: 'WorldB' });
    await waitFor(() => expect(api.play.state).toHaveBeenCalledWith('WorldB'));

    await act(async () => { release({ chapter: chapter({ chapter_text: 'Old world reply' }) }); });

    expect(result.current.turns).toHaveLength(0);
    expect(result.current.playState).toEqual(playState());
  });

  it('a late success from a previous world does not unlock the new world request', async () => {
    const { result, rerender } = render('WorldA');
    await waitFor(() => expect(api.play.state).toHaveBeenCalledWith('WorldA'));

    let releaseA: (value: any) => void = () => {};
    api.play.continue.mockImplementationOnce(() => new Promise((resolve) => { releaseA = resolve; }));
    act(() => result.current.setInput('act A'));
    await act(async () => { result.current.handleSend(); });

    rerender({ name: 'WorldB' });
    await waitFor(() => expect(api.play.state).toHaveBeenCalledWith('WorldB'));

    let releaseB: (value: any) => void = () => {};
    api.play.continue.mockImplementationOnce(() => new Promise((resolve) => { releaseB = resolve; }));
    act(() => result.current.setInput('act B'));
    await act(async () => { result.current.handleSend(); });

    await act(async () => { releaseA({ chapter: chapter({ chapter_text: 'Old A reply' }) }); });

    expect(result.current.loading).toBe(true);
    await act(async () => { result.current.handleSend(); });
    expect(api.play.continue).toHaveBeenCalledTimes(2);

    await act(async () => { releaseB({ chapter: chapter({ chapter_text: 'B reply' }) }); });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.turns).toHaveLength(1);
  });

  it('a late error from a previous world does not set the new world error', async () => {
    const { result, rerender } = render('WorldA');
    await waitFor(() => expect(api.play.state).toHaveBeenCalledWith('WorldA'));

    let rejectA: (reason: any) => void = () => {};
    api.play.continue.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectA = reject; }));
    act(() => result.current.setInput('act A'));
    await act(async () => { result.current.handleSend(); });

    rerender({ name: 'WorldB' });
    await waitFor(() => expect(api.play.state).toHaveBeenCalledWith('WorldB'));

    await act(async () => { rejectA(new Error('late A failure')); });

    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });
});

describe('usePlaySession committed data refresh', () => {
  it('reloads map and affinity graph after a committed turn', async () => {
    const { result } = render();
    await waitFor(() => expect(api.map.status).toHaveBeenCalledTimes(1));
    const mapCalls = api.map.status.mock.calls.length;
    const graphCalls = api.worlds.affinityGraph.mock.calls.length;

    act(() => result.current.setInput('go'));
    await act(async () => { await result.current.handleSend(); });

    expect(api.map.status.mock.calls.length).toBe(mapCalls + 1);
    expect(api.worlds.affinityGraph.mock.calls.length).toBe(graphCalls + 1);
  });

  it('reloads chapters, map and affinity graph after regenerate', async () => {
    const { result } = render();
    await waitFor(() => expect(api.map.status).toHaveBeenCalledTimes(1));
    const chapterCalls = api.play.getChapters.mock.calls.length;
    const mapCalls = api.map.status.mock.calls.length;
    const graphCalls = api.worlds.affinityGraph.mock.calls.length;

    await act(async () => { await result.current.handleRegenerate(); });

    expect(api.play.regenerate).toHaveBeenCalledTimes(1);
    expect(api.play.getChapters.mock.calls.length).toBe(chapterCalls + 1);
    expect(api.map.status.mock.calls.length).toBe(mapCalls + 1);
    expect(api.worlds.affinityGraph.mock.calls.length).toBe(graphCalls + 1);
  });

  it('loads quests and journal and refreshes them after a committed turn', async () => {
    api.discovery.questBoard.mockResolvedValue({
      quests: [{ quest_id: 'q1', title: 'Find the relic', status: 'active' }], enabled: true,
    });
    api.discovery.journal.mockResolvedValue({
      entries: [{ quest_id: 'q1', title: 'Find the relic', status: 'active', source: { kind: 'start' }, discovered_at_tick: 0 }],
    });
    const { result } = render();
    await waitFor(() => expect(result.current.quests).toHaveLength(1));
    expect(result.current.journal).toHaveLength(1);

    const questCalls = api.discovery.questBoard.mock.calls.length;
    act(() => result.current.setInput('go'));
    await act(async () => { await result.current.handleSend(); });
    await waitFor(() => expect(api.discovery.questBoard.mock.calls.length).toBe(questCalls + 1));
  });

  it('reloads on a restore event for the matching world', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalledTimes(1));

    await act(async () => {
      window.dispatchEvent(new CustomEvent(WORLD_RESTORED_EVENT, { detail: { worldName: 'WorldA' } }));
    });

    expect(api.play.state.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(result.current).toBeTruthy();
  });
});

describe('usePlaySession output length', () => {
  it('loads the saved length and persists a new one to the backend', async () => {
    api.play.state.mockResolvedValue(playState({ output_length: 'Detailed' }));
    const { result } = render();
    await waitFor(() => expect(result.current.outputLength).toBe('long'));

    act(() => result.current.setOutputLength('short'));
    expect(result.current.outputLength).toBe('short');
    expect(api.worlds.updateConfig).toHaveBeenCalledWith('WorldA', { output_length: 'Concise' });
  });
});

describe('usePlaySession request ids', () => {
  it('reuses the id for a retry of the same action and uses a new one for a new action', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    api.play.continue.mockRejectedValueOnce(new Error('network down'));
    act(() => result.current.setInput('open the door'));
    await act(async () => { await result.current.handleSend(); });
    const firstId = api.play.continue.mock.calls[0][2].requestId;
    expect(firstId).toBeTruthy();

    await act(async () => { await result.current.handleSend(); });
    const retryId = api.play.continue.mock.calls[1][2].requestId;
    expect(retryId).toBe(firstId);

    act(() => result.current.setInput('run away'));
    await act(async () => { await result.current.handleSend(); });
    const newId = api.play.continue.mock.calls[2][2].requestId;
    expect(newId).toBeTruthy();
    expect(newId).not.toBe(firstId);
  });
});

describe('usePlaySession stale travel previews', () => {
  // The route drawn on the map and the "Travel here" button are both derived
  // from `travelPreview`. A result that no longer describes the destination the
  // player is looking at — or that was computed from older world state — must
  // never be rendered, and must never be able to reinstall itself later.
  const preview = (dest: string, minutes: number) => ({
    status: 'available', destination: dest, route: ['Sect', dest], legs: [],
    elapsed_minutes: minutes, estimated_ticks: 1, risk: { level: 'low', known_tags: [] },
  });

  const settled = async (result: { current: ReturnType<typeof usePlaySession> }) => {
    // Echo the requested destination, like the engine does.
    api.map.previewTravel.mockImplementation(async (_w: string, dest: string) => preview(dest, 90));
    await waitFor(() => expect(api.map.status).toHaveBeenCalled());
    return result;
  };

  it('invalidate the old route immediately when re-requesting the SAME destination', async () => {
    const { result } = renderHook(() => usePlaySession('W'));
    await settled(result);

    await act(async () => { await result.current.handlePreviewTravel({ name: 'Old Forest' }); });
    expect(result.current.travelPreview?.elapsed_minutes).toBe(90);

    // Second request for the SAME destination, deliberately held open. The
    // first answer was computed from older state, so it is already worthless.
    let release: (v: any) => void = () => {};
    api.map.previewTravel.mockImplementationOnce(() => new Promise(r => { release = r; }));
    act(() => { result.current.handlePreviewTravel({ name: 'Old Forest' }); });

    expect(result.current.travelPreview, 'stale preview still shown while re-requesting the same destination').toBeNull();
    expect(result.current.previewLoading).toBe(true);

    await act(async () => { release(preview('Old Forest', 99)); });
    expect(result.current.travelPreview?.elapsed_minutes).toBe(99);
    expect(result.current.previewLoading).toBe(false);
  });

  it('ignores an out-of-order (older) response that lands after a newer one', async () => {
    const { result } = renderHook(() => usePlaySession('W'));
    await settled(result);

    let releaseOld: (v: any) => void = () => {};
    api.map.previewTravel.mockImplementationOnce(() => new Promise(r => { releaseOld = r; }));
    act(() => { result.current.handlePreviewTravel({ name: 'Old Forest' }); });

    await act(async () => { await result.current.handlePreviewTravel({ name: 'Sky Peak' }); });
    expect(result.current.travelPreview?.destination).toBe('Sky Peak');

    // The abandoned request finally answers. It must not overwrite the route to
    // the place the player is actually looking at.
    await act(async () => { releaseOld(preview('Old Forest', 5)); });
    expect(result.current.travelPreview?.destination, 'a stale response overwrote the newer one').toBe('Sky Peak');
  });

  it('lets a stale failure neither clear the live preview nor raise an error', async () => {
    const { result } = renderHook(() => usePlaySession('W'));
    await settled(result);

    let rejectOld: (e: any) => void = () => {};
    api.map.previewTravel.mockImplementationOnce(() => new Promise((_r, rej) => { rejectOld = rej; }));
    act(() => { result.current.handlePreviewTravel({ name: 'Old Forest' }); });

    await act(async () => { await result.current.handlePreviewTravel({ name: 'Sky Peak' }); });
    expect(result.current.travelPreview?.destination).toBe('Sky Peak');

    await act(async () => { rejectOld(new Error('old failure')); });
    expect(result.current.travelPreview?.destination, 'a stale failure cleared the live preview').toBe('Sky Peak');
    expect(result.current.error).toBeNull();
    expect(result.current.previewLoading, 'a stale response cleared the in-flight spinner').toBe(false);
  });

  it('keeps the newest request loading while an older one is still pending', async () => {
    const { result } = renderHook(() => usePlaySession('W'));
    await settled(result);

    let releaseOld: (v: any) => void = () => {};
    api.map.previewTravel.mockImplementationOnce(() => new Promise(r => { releaseOld = r; }));
    act(() => { result.current.handlePreviewTravel({ name: 'Old Forest' }); });

    let releaseNew: (v: any) => void = () => {};
    api.map.previewTravel.mockImplementationOnce(() => new Promise(r => { releaseNew = r; }));
    act(() => { result.current.handlePreviewTravel({ name: 'Sky Peak' }); });

    // The abandoned request settles first; it must not report "not loading" for
    // the request that is still in flight.
    await act(async () => { releaseOld(preview('Old Forest', 5)); });
    expect(result.current.previewLoading, 'stale response hid the spinner for a live request').toBe(true);
    expect(result.current.travelPreview).toBeNull();

    await act(async () => { releaseNew(preview('Sky Peak', 12)); });
    expect(result.current.travelPreview?.destination).toBe('Sky Peak');
    expect(result.current.previewLoading).toBe(false);
  });

  it('rejects a response that names a different destination than the one requested', async () => {
    const { result } = renderHook(() => usePlaySession('W'));
    await settled(result);

    // Defence in depth: the engine echoes the destination back. If it does not
    // match what we asked for, the payload is not ours to trust.
    api.map.previewTravel.mockResolvedValueOnce(preview('Somewhere Else', 42));
    await act(async () => { await result.current.handlePreviewTravel({ name: 'Sky Peak' }); });

    expect(result.current.travelPreview, 'a mismatched-destination payload was installed').toBeNull();
    expect(result.current.previewLoading).toBe(false);
  });

  it('discards a preview from the previous world when the world changes', async () => {
    const { result, rerender } = renderHook(({ w }) => usePlaySession(w), { initialProps: { w: 'W1' } });
    await settled(result);

    let release: (v: any) => void = () => {};
    api.map.previewTravel.mockImplementationOnce(() => new Promise(r => { release = r; }));
    act(() => { result.current.handlePreviewTravel({ name: 'Old Forest' }); });

    rerender({ w: 'W2' });
    await act(async () => { release(preview('Old Forest', 90)); });

    expect(result.current.travelPreview, "the previous world's preview leaked into the new world").toBeNull();
    expect(result.current.previewLoading).toBe(false);
  });

  it('clears the route when travel is actually committed', async () => {
    const { result } = renderHook(() => usePlaySession('W'));
    await settled(result);

    await act(async () => { await result.current.handlePreviewTravel({ name: 'Old Forest' }); });
    expect(result.current.travelPreview).not.toBeNull();

    act(() => { result.current.handleTravelTo({ name: 'Old Forest' }); });
    expect(result.current.travelPreview, 'the checked route survived committing the journey').toBeNull();
  });
});

describe('usePlaySession not-saved drafts', () => {
  it('stores a blocked turn as a draft and clears it on dismiss', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    const blocked: any = new Error('Kept as a draft and was NOT saved.');
    blocked.detail = {
      reason: 'consistency_check_failed', status: 'failed', persisted: false,
      draft_chapter_text: 'Draft scene text', message: 'Retry the action.',
    };
    api.play.continue.mockRejectedValueOnce(blocked);

    act(() => result.current.setInput('open the door'));
    await act(async () => { await result.current.handleSend(); });

    expect(result.current.draft?.text).toBe('Draft scene text');
    expect(result.current.draft?.status).toBe('failed');
    expect(result.current.input).toBe('open the door');
    expect(sessionStorage.getItem('story-engine:draft:WorldA')).toContain('Draft scene text');
    expect(result.current.turns).toHaveLength(0);

    act(() => result.current.handleDismissDraft());
    expect(result.current.draft).toBeNull();
    expect(sessionStorage.getItem('story-engine:draft:WorldA')).toBeNull();
  });

  it('clears a stored draft after a successful commit', async () => {
    sessionStorage.setItem('story-engine:draft:WorldA', JSON.stringify({
      worldName: 'WorldA', userInput: 'old', text: 'old draft',
      status: 'unavailable', reason: 'x', message: 'y', createdAt: 1,
    }));
    const { result } = render();
    await waitFor(() => expect(result.current.draft?.text).toBe('old draft'));

    act(() => result.current.setInput('try again'));
    await act(async () => { await result.current.handleSend(); });

    expect(result.current.draft).toBeNull();
    expect(sessionStorage.getItem('story-engine:draft:WorldA')).toBeNull();
    expect(result.current.turns).toHaveLength(1);
  });

  it('retryDraft resends the stored action after reload even with an empty input', async () => {
    sessionStorage.setItem('story-engine:draft:WorldA', JSON.stringify({
      worldName: 'WorldA', userInput: 'open the door', text: 'draft scene',
      status: 'failed', reason: 'consistency_check_failed', message: 'retry', createdAt: 1,
    }));
    const { result } = render();
    await waitFor(() => expect(result.current.draft?.userInput).toBe('open the door'));
    expect(result.current.input).toBe('');

    await act(async () => { await result.current.handleRetryDraft(); });

    expect(api.play.continue).toHaveBeenCalledWith('WorldA', 'open the door', expect.anything());
    expect(result.current.draft).toBeNull();
    expect(result.current.input).toBe('');
    expect(result.current.turns).toHaveLength(1);
  });

  it('retryDraft sends the draft action and keeps what the player is typing', async () => {
    sessionStorage.setItem('story-engine:draft:WorldA', JSON.stringify({
      worldName: 'WorldA', userInput: 'open the door', text: 'draft scene',
      status: 'failed', reason: 'consistency_check_failed', message: 'retry', createdAt: 1,
    }));
    const { result } = render();
    await waitFor(() => expect(result.current.draft).not.toBeNull());

    act(() => result.current.setInput('a different action'));
    await act(async () => { await result.current.handleRetryDraft(); });

    expect(api.play.continue).toHaveBeenCalledWith('WorldA', 'open the door', expect.anything());
    expect(result.current.input).toBe('a different action');
    expect(result.current.turns).toHaveLength(1);
  });
});

/**
 * The experimental pacing switch is opt-in and per world.
 *
 * What these tests pin down, and why each one matters:
 *  - off means the request carries no mode at all, so the default request is
 *    byte-identical to what it was before the switch existed;
 *  - on means every entry point that generates prose - a typed action, the
 *    first chapter, a time skip, a reroll - carries it, so no path quietly
 *    falls back to the old guidance;
 *  - the choice is remembered per world across a remount (a page refresh) and
 *    does not leak into another world.
 */
describe('usePlaySession experimental narration mode', () => {
  const sendOne = async (result: { current: ReturnType<typeof usePlaySession> }, text = 'look around') => {
    act(() => result.current.setInput(text));
    await act(async () => { await result.current.handleSend(); });
  };

  it('sends no narration mode while the switch is off', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    expect(result.current.narrationMode).toBe('classic');
    await sendOne(result);

    const opts = api.play.continue.mock.calls[0][2];
    expect(opts).not.toHaveProperty('narrationMode');
  });

  it('sends the experimental mode for the next turn once switched on', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    act(() => result.current.setNarrationMode('experimental'));
    await sendOne(result);

    expect(api.play.continue).toHaveBeenCalledWith(
      'WorldA', 'look around', expect.objectContaining({ narrationMode: 'experimental' }),
    );
    expect(result.current.turns).toHaveLength(1);
  });

  it('sends and remembers the opt-in actor ensemble independently of classic', async () => {
    const first = render('WorldA');
    await waitFor(() => expect(first.result.current.playState).not.toBeNull());
    act(() => first.result.current.setNarrationMode('ensemble'));
    await sendOne(first.result);
    expect(api.play.continue).toHaveBeenCalledWith(
      'WorldA', 'look around', expect.objectContaining({ narrationMode: 'ensemble' }),
    );
    first.unmount();

    const again = render('WorldA');
    await waitFor(() => expect(again.result.current.playState).not.toBeNull());
    expect(again.result.current.narrationMode).toBe('ensemble');
  });

  it('carries the mode into chapter start, time skip and reroll', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());
    act(() => result.current.setNarrationMode('experimental'));

    await act(async () => { await result.current.handleStartChapter(); });
    expect(api.play.start).toHaveBeenCalledWith(
      'WorldA', expect.objectContaining({ narration_mode: 'experimental' }),
    );

    const skip: any = { amount: 1, unit: 'days', activity: 'Study', interruption_policy: 'important_events' };
    await act(async () => { await result.current.handleExecuteTimeSkip(skip); });
    expect(api.play.executeTimeSkip).toHaveBeenCalledWith(
      'WorldA', expect.objectContaining({ narration_mode: 'experimental' }),
    );

    await act(async () => { await result.current.handleRegenerate(); });
    expect(api.play.regenerate).toHaveBeenCalledWith(
      'WorldA', expect.objectContaining({ narrationMode: 'experimental' }),
    );
  });

  it('rerolls the latest turn with a revised action and reports success', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    let replaced = false;
    await act(async () => {
      replaced = await result.current.handleRegenerate('Invite Ren to lunch after class.');
    });

    expect(replaced).toBe(true);
    expect(api.play.regenerate).toHaveBeenCalledWith(
      'WorldA', expect.objectContaining({ userInput: 'Invite Ren to lunch after class.' }),
    );
  });

  it('remembers the switch per world across a remount and leaves other worlds alone', async () => {
    const first = render('WorldA');
    await waitFor(() => expect(first.result.current.playState).not.toBeNull());
    act(() => first.result.current.setNarrationMode('experimental'));
    expect(first.result.current.narrationMode).toBe('experimental');
    first.unmount();

    // A remount is what a page refresh looks like from the hook's side.
    const otherWorld = render('WorldB');
    await waitFor(() => expect(otherWorld.result.current.playState).not.toBeNull());
    expect(otherWorld.result.current.narrationMode).toBe('classic');
    otherWorld.unmount();

    const again = render('WorldA');
    await waitFor(() => expect(again.result.current.playState).not.toBeNull());
    expect(again.result.current.narrationMode).toBe('experimental');
  });

  it('turns the switch back off and stops sending the mode', async () => {
    const { result } = render();
    await waitFor(() => expect(api.play.state).toHaveBeenCalled());

    act(() => result.current.setNarrationMode('experimental'));
    act(() => result.current.setNarrationMode('classic'));
    await sendOne(result);

    expect(result.current.narrationMode).toBe('classic');
    expect(api.play.continue.mock.calls[0][2]).not.toHaveProperty('narrationMode');
    expect(localStorage.getItem('story-engine:narration-mode:WorldA')).toBeNull();
  });
});
