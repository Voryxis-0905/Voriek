import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { PlayNarrative } from './PlayNarrative';

function props(turns: any[]) {
  return {
    playState: null,
    turns,
    input: '',
    setInput: () => {},
    loading: false,
    error: null,
    setError: () => {},
    outputLength: 'medium',
    setOutputLength: () => {},
    narrationMode: 'classic',
    setNarrationMode: () => {},
    expandedTurns: { 0: true },
    toggleTurnExpanded: () => {},
    collapseAllPrevious: () => {},
    expandAllTurns: () => {},
    preludeText: null,
    draft: null,
    handleDismissDraft: () => {},
    handleRetryDraft: () => {},
    epilogue: null,
    lifecycleStatus: 'active',
    epilogueChoices: [],
    handleLoadEndgameChoices: () => {},
    handleChooseEnding: () => {},
    chatEndRef: { current: null },
    handleSend: () => {},
    timeSkipOpen: false,
    setTimeSkipOpen: () => {},
    timeSkipPreview: null,
    timeSkipLoading: false,
    handlePreviewTimeSkip: () => {},
    handleExecuteTimeSkip: () => {},
    handleStartChapter: () => {},
    handleRegenerate: () => {},
    handleGeneratePrelude: () => {},
    handleConfirmPrelude: () => {},
  } as any;
}

describe('PlayNarrative story rendering', () => {
  it('offers Begin Story after a confirmed prelude reload', () => {
    const start = vi.fn();
    render(<PlayNarrative {...props([])} playState={{ prelude_confirmed: true } as any}
      handleStartChapter={start} />);
    fireEvent.click(screen.getByRole('button', { name: 'Begin Story' }));
    expect(start).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: 'Confirm Prelude & Play' })).toBeNull();
  });
  it('shows the three time skip inputs and known deadline warning', () => {
    const preview = vi.fn();
    render(<PlayNarrative {...props([])} timeSkipOpen={true} handlePreviewTimeSkip={preview} timeSkipPreview={{ requested_minutes: 1440, granted_minutes: 180, requested_ticks: 24, granted_ticks: 3, start_tick: 0, end_tick: 3, warnings: [{ kind: 'known_deadline', event_id: 'storm', title: 'Storm arrives', deadline_tick: 3, ticks_away: 3 }], will_interrupt: true, stopped_reason: 'known_deadline', requires_confirmation: true, activity: 'Study', forced: false }} />);
    expect(screen.getByLabelText('Time skip amount')).toBeInTheDocument();
    expect(screen.getByLabelText('Time skip activity')).toBeInTheDocument();
    expect(screen.getByLabelText('Interruption policy')).toBeInTheDocument();
    expect(screen.getByText(/Storm arrives/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Preview'));
    expect(preview).toHaveBeenCalledWith(expect.objectContaining({ amount: 1, unit: 'hours' }));
  });
  it('renders model output as text and does not execute injected HTML', () => {
    (window as any).__pwned = false;
    const output = 'Safe line\n<img src=x onerror="window.__pwned=true"><script>window.__pwned=true</script>';
    const { container } = render(
      <PlayNarrative {...props([{
        input: 'go',
        output,
        chapterIndex: 1,
        turnIndex: 1,
        checkpointId: 'cp_0',
        timestamp: Date.now(),
      }])} />
    );

    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as any).__pwned).toBe(false);
    expect(container.textContent).toContain('<img src=x onerror="window.__pwned=true">');
  });

  it('lets the player revise the latest action before rerolling it', async () => {
    const handleRegenerate = vi.fn().mockResolvedValue(true);
    render(<PlayNarrative {...props([{
      input: 'Wait by the door.',
      output: 'The hall is quiet.',
      chapterIndex: 1,
      turnIndex: 1,
      checkpointId: 'cp_0',
      timestamp: Date.now(),
    }])} handleRegenerate={handleRegenerate} />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit latest action and reroll' }));
    const editor = screen.getByRole('textbox', { name: 'Revised latest action' });
    expect(editor).toHaveValue('Wait by the door.');
    fireEvent.change(editor, { target: { value: 'Ask Nao to join Tarou for lunch.' } });
    expect(screen.getByText(/Only the latest turn is replaced/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reroll with revised action' }));

    await waitFor(() => expect(handleRegenerate).toHaveBeenCalledWith('Ask Nao to join Tarou for lunch.'));
  });

  it('shows a not-saved draft with its text and a retry path', () => {
    const handleSend = vi.fn();
    const handleRetryDraft = vi.fn();
    const handleDismissDraft = vi.fn();
    render(
      <PlayNarrative {...props([])} draft={{
        worldName: 'WorldA',
        userInput: 'open the door',
        text: 'The draft scene that was not committed.',
        status: 'failed',
        reason: 'consistency_check_failed',
        message: 'Kept as a draft, retry the action.',
        createdAt: 1,
      }} handleDismissDraft={handleDismissDraft} handleRetryDraft={handleRetryDraft} handleSend={handleSend} />
    );

    expect(screen.getByText(/not saved/i)).toBeInTheDocument();
    expect(screen.getByText('The draft scene that was not committed.')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Retry action'));
    expect(handleRetryDraft).toHaveBeenCalledTimes(1);
    expect(handleSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Dismiss'));
    expect(handleDismissDraft).toHaveBeenCalledTimes(1);
  });

  it('labels the pacing switch as experimental and says the turn still saves', () => {
    const setNarrationMode = vi.fn();
    const { rerender } = render(
      <PlayNarrative {...props([])} narrationMode="classic" setNarrationMode={setNarrationMode} />,
    );

    const toggle = screen.getByRole('checkbox', { name: 'Experimental pacing' });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/Experimental pacing/)).toBeInTheDocument();
    // Off by default, so the reassurance is not shown until it is relevant.
    expect(screen.queryByText(/still save normally/)).toBeNull();

    fireEvent.click(toggle);
    expect(setNarrationMode).toHaveBeenCalledWith('experimental');

    rerender(<PlayNarrative {...props([])} narrationMode="experimental" setNarrationMode={setNarrationMode} />);
    expect(screen.getByRole('checkbox', { name: 'Experimental pacing' })).toBeChecked();
    // The switch must never read as "this turn will not be saved".
    expect(screen.getByText(/still save normally/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Experimental pacing' }));
    expect(setNarrationMode).toHaveBeenLastCalledWith('classic');
  });
});
