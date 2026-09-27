import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from '../../api/client';
import type { InventoryItem, PlayState, TravelPreview, TimeSkipRequest, TimeSkipPreview, NarrationMode } from '../../api/client';
import type { DrawerTab, Turn, PlayDraft } from './types';
import { WORLD_RESTORED_EVENT, DRAFT_STORAGE_PREFIX, narrationModeStorageKey } from './types';

export type OutputLength = 'short' | 'medium' | 'long';

const LENGTH_TO_BACKEND: Record<OutputLength, string> = {
  short: 'Concise',
  medium: 'Standard',
  long: 'Detailed',
};

const backendToLength = (value?: string): OutputLength => {
  if (value === 'Concise') return 'short';
  if (value === 'Detailed') return 'long';
  return 'medium';
};

const draftKey = (name: string) => `${DRAFT_STORAGE_PREFIX}${name}`;

/**
 * Whether the World State panel starts open.
 *
 * It is 288px of fixed chrome in the play row. At `xl` and up there is room for
 * it and it has always started open, so that is preserved. Below `xl` the panel
 * is an overlay (see `PlaySidebar`), and starting it open would drop a panel
 * over the story the player just opened - so the narrow default is closed, and
 * the rail's toggle is what brings it in. That is also what makes the toggle's
 * own label honest from the first paint.
 *
 * `matchMedia` is absent under jsdom, where the hook is exercised directly;
 * defaulting to open there keeps desktop-shaped behaviour unchanged.
 */
const sidebarStartsOpen = () =>
  typeof window === 'undefined'
  || typeof window.matchMedia !== 'function'
  || window.matchMedia('(min-width: 1280px)').matches;

export function usePlaySession(worldName: string) {
  const [playState, setPlayState] = useState<PlayState | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Controls
  const [outputLength, setOutputLengthState] = useState<OutputLength>('medium');

  // Experimental pacing. Off unless this world's stored preference says
  // otherwise; the server also defaults to the classic path when a request
  // carries no mode, so the two defaults agree.
  const [narrationMode, setNarrationModeState] = useState<NarrationMode>('classic');

  // Sidebar collapse
  const [sidebarOpen, setSidebarOpen] = useState(sidebarStartsOpen);

  // Collapsible turns history state
  const [expandedTurns, setExpandedTurns] = useState<Record<number, boolean>>({});

  const toggleTurnExpanded = (index: number) => {
    setExpandedTurns(prev => {
      const isCurrentlyExpanded = prev[index] !== undefined
        ? prev[index]
        : (index === turns.length - 1 || turns.length <= 2);
      return { ...prev, [index]: !isCurrentlyExpanded };
    });
  };

  const collapseAllPrevious = () => {
    const newExpanded: Record<number, boolean> = {};
    turns.forEach((_, idx) => {
      newExpanded[idx] = idx === turns.length - 1;
    });
    setExpandedTurns(newExpanded);
  };

  const expandAllTurns = () => {
    const newExpanded: Record<number, boolean> = {};
    turns.forEach((_, idx) => {
      newExpanded[idx] = true;
    });
    setExpandedTurns(newExpanded);
  };

  // Right Drawer tab
  const [activeDrawer, setActiveDrawer] = useState<DrawerTab>(null);

  // Extra data for drawers
  const [locations, setLocations] = useState<any[]>([]);
  const [affinityGraph, setAffinityGraph] = useState<{ nodes: any[]; edges: any[] }>({ nodes: [], edges: [] });
  const [preludeText, setPreludeText] = useState<string | null>(null);
  const [draft, setDraft] = useState<PlayDraft | null>(null);
  const [quests, setQuests] = useState<any[]>([]);
  const [journal, setJournal] = useState<any[]>([]);
  const [epilogueChoices, setEpilogueChoices] = useState<string[]>([]);
  const [travelPreview, setTravelPreview] = useState<TravelPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [timeSkipOpen, setTimeSkipOpen] = useState(false);
  const [timeSkipPreview, setTimeSkipPreview] = useState<TimeSkipPreview | null>(null);
  const [timeSkipLoading, setTimeSkipLoading] = useState(false);

  const chatEndRef = useRef<HTMLDivElement>(null);

  // Guards against duplicate submissions (a click cannot fire twice before the
  // first request settles) and against late responses of a previous world.
  const sendingRef = useRef(false);
  const worldTokenRef = useRef(0);
  // The action currently in flight (or awaiting retry). Reusing its id lets the
  // backend replay a committed turn instead of calling the model again.
  const pendingActionRef = useRef<{ id: string; userInput: string } | null>(null);
  const revisionRef = useRef<number | undefined>(undefined);

  /**
   * Monotonic ticket for travel previews. Only the newest request may install
   * its result, and only while it still belongs to the current world.
   *
   * Why a counter rather than the world token alone: two previews of the *same*
   * world race each other. Selecting one destination, changing your mind, then
   * having the first (slower) response land last would overwrite the route to
   * the place you are actually looking at with one to a place you already
   * abandoned. The ticket makes "newest wins" total, across worlds and within
   * one world.
   */
  const previewTicketRef = useRef(0);
  /** Destination of the newest preview request, so a response can be matched to it. */
  const previewDestinationRef = useRef<string | null>(null);

  const makeRequestId = () =>
    globalThis.crypto?.randomUUID?.() ?? `req_${Date.now()}_${Math.random().toString(36).slice(2)}`;

  const readStoredDraft = useCallback((name: string): PlayDraft | null => {
    try {
      const raw = sessionStorage.getItem(draftKey(name));
      return raw ? (JSON.parse(raw) as PlayDraft) : null;
    } catch {
      return null;
    }
  }, []);

  const storeDraft = useCallback((next: PlayDraft | null) => {
    try {
      if (next) {
        sessionStorage.setItem(draftKey(next.worldName), JSON.stringify(next));
      } else {
        sessionStorage.removeItem(draftKey(worldName));
      }
    } catch {
      /* sessionStorage may be unavailable (private mode, quota) — drafts are best-effort. */
    }
  }, [worldName]);

  /**
   * Read this world's stored pacing preference.
   *
   * Read per world, on world change, so switching to another world shows that
   * world's own setting instead of carrying this one over. Anything other than
   * the stored opt-in value reads as off.
   */
  const readStoredNarrationMode = useCallback((name: string): NarrationMode => {
    try {
      const saved = localStorage.getItem(narrationModeStorageKey(name));
      return saved === 'experimental' || saved === 'ensemble' ? saved : 'classic';
    } catch {
      return 'classic';
    }
  }, []);

  const setNarrationMode = (value: NarrationMode) => {
    setNarrationModeState(value);
    try {
      if (value !== 'classic') {
        localStorage.setItem(narrationModeStorageKey(worldName), value);
      } else {
        // Off is the absence of a key, so "never touched" and "turned back off"
        // read the same way on the next visit.
        localStorage.removeItem(narrationModeStorageKey(worldName));
      }
    } catch {
      /* The preference is best-effort; the in-memory value still applies. */
    }
  };

  /**
   * The mode to send with a generation request.
   *
   * Classic mode sends nothing at all: the default request body stays exactly
   * what it was before this option existed, which is what keeps the old path
   * provably unchanged. Two shapes because the API client takes camelCase
   * options for `continue`/`regenerate` and a snake_case body for `start` and
   * the time-skip execute call.
   */
  const modeOpts = () =>
    narrationMode !== 'classic' ? { narrationMode } : {};
  const modeBody = () =>
    narrationMode !== 'classic' ? { narration_mode: narrationMode } : {};

  useEffect(() => {
    // Scroll the transcript column itself, never `scrollIntoView`.
    //
    // `scrollIntoView` walks *every* scrollable ancestor, and this app has a
    // horizontal one above the transcript: PlayPage's three-column flex row.
    // At 390px that row is 1194px wide inside a 390px viewport, so the smooth
    // scroll dragged it sideways and shoved the map off the left edge
    // (measured: canvas at x = -222, 28% visible) — and it ran on every turn,
    // racing the drawer-reveal effect in PlayPage. Writing scrollTop on the
    // column keeps the scroll strictly vertical.
    const column = chatEndRef.current?.parentElement;
    if (column) column.scrollTo({ top: column.scrollHeight, behavior: 'smooth' });
  }, [turns]);

  const loadPlayState = useCallback(async () => {
    const token = worldTokenRef.current;
    try {
      const state = await api.play.state(worldName);
      if (token !== worldTokenRef.current) return;
      setPlayState(state);
      setOutputLengthState(backendToLength(state?.output_length));
      revisionRef.current = typeof state?.revision === 'number' ? state.revision : undefined;
      setError(null);
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      setError(e.message || 'Failed to load world state');
    }
  }, [worldName]);

  const loadMap = useCallback(async () => {
    const token = worldTokenRef.current;
    try {
      const mapData = await api.map.status(worldName);
      if (token !== worldTokenRef.current) return;
      setLocations(mapData.locations || []);
    } catch {
      try {
        const fallback = await api.map.get(worldName);
        if (token !== worldTokenRef.current) return;
        setLocations(fallback.locations || []);
      } catch {
        if (token !== worldTokenRef.current) return;
        setLocations([]);
      }
    }
  }, [worldName]);

  const loadAffinityGraph = useCallback(async () => {
    const token = worldTokenRef.current;
    try {
      const graph = await api.worlds.affinityGraph(worldName);
      if (token !== worldTokenRef.current) return;
      setAffinityGraph(graph || { nodes: [], edges: [] });
    } catch {
      if (token !== worldTokenRef.current) return;
      setAffinityGraph({ nodes: [], edges: [] });
    }
  }, [worldName]);

  const loadDiscovery = useCallback(async () => {
    const token = worldTokenRef.current;
    try {
      const [qb, jn] = await Promise.all([
        api.discovery.questBoard(worldName),
        api.discovery.journal(worldName),
      ]);
      if (token !== worldTokenRef.current) return;
      setQuests(qb?.quests || []);
      setJournal(jn?.entries || []);
    } catch {
      if (token !== worldTokenRef.current) return;
      setQuests([]);
      setJournal([]);
    }
  }, [worldName]);

  const loadExistingChapters = useCallback(async () => {
    const token = worldTokenRef.current;
    try {
      const chapters = await api.play.getChapters(worldName);
      if (token !== worldTokenRef.current) return;
      if (!chapters || chapters.length === 0) {
        setTurns([]);
        return;
      }
      const preludeChapter = chapters.find((c: any) => c.chapter_index === 0);
      if (preludeChapter && preludeChapter.chapter_text) {
        setPreludeText(preludeChapter.chapter_text);
      }
      const nonPrelude = chapters.filter((c: any) => c.chapter_index !== 0);
      if (nonPrelude.length === 0) {
        setTurns([]);
        return;
      }
      const formatted: Turn[] = nonPrelude.map((c: any) => ({
        input: c.user_input || 'Start Chapter',
        output: c.chapter_text,
        chapterIndex: c.chapter_index,
        turnIndex: c.turn_index,
        checkpointId: c.checkpoint_id,
        timestamp: Date.now(),
      }));
      setTurns(formatted);
    } catch {
      /* No chapters yet (fresh world) — leaving `turns` untouched is correct. */
    }
  }, [worldName]);

  useEffect(() => {
    worldTokenRef.current += 1;
    // Reset the in-flight lock together with the data, so the new world can send
    // while a late response of the previous world is still outstanding.
    sendingRef.current = false;
    pendingActionRef.current = null;
    revisionRef.current = undefined;
    setLoading(false);
    setTurns([]);
    setPlayState(null);
    setLocations([]);
    setAffinityGraph({ nodes: [], edges: [] });
    setPreludeText(null);
    setDraft(readStoredDraft(worldName));
    // The pacing switch is per world: read this world's own preference rather
    // than keeping whatever the previous world had selected.
    setNarrationModeState(readStoredNarrationMode(worldName));
    setQuests([]);
    setJournal([]);
    setEpilogueChoices([]);
    // Bump the ticket too: a preview of the previous world that is still in
    // flight must not install itself into the new world, and must not clear the
    // new world's loading flag on its way out.
    previewTicketRef.current += 1;
    previewDestinationRef.current = null;
    setTravelPreview(null);
    setPreviewLoading(false);
    setTimeSkipOpen(false);
    setTimeSkipPreview(null);
    setTimeSkipLoading(false);
    setError(null);

    if (worldName) {
      loadPlayState();
      loadMap();
      loadAffinityGraph();
      loadExistingChapters();
      loadDiscovery();
    }
  }, [worldName, readStoredDraft, readStoredNarrationMode, loadPlayState, loadMap, loadAffinityGraph, loadExistingChapters, loadDiscovery]);

  // A creator restore can happen outside this screen; refresh committed data.
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (!detail || detail.worldName === worldName) {
        loadPlayState();
        loadMap();
        loadAffinityGraph();
        loadExistingChapters();
        loadDiscovery();
      }
    };
    window.addEventListener(WORLD_RESTORED_EVENT, handler);
    return () => window.removeEventListener(WORLD_RESTORED_EVENT, handler);
  }, [worldName, loadPlayState, loadMap, loadAffinityGraph, loadExistingChapters, loadDiscovery]);

  const reloadCommittedData = async () => {
    await Promise.all([loadPlayState(), loadMap(), loadAffinityGraph(), loadDiscovery()]);
  };

  const setOutputLength = (value: OutputLength) => {
    setOutputLengthState(value);
    api.worlds.updateConfig(worldName, { output_length: LENGTH_TO_BACKEND[value] }).catch(() => {
      setError('Failed to save the output length setting');
    });
  };

  const submitAction = async (
    rawInput: string,
    options: { clearInputOnSuccess: boolean; requestId: string },
  ) => {
    const userInput = rawInput.trim();
    if (!userInput || sendingRef.current) return;
    const token = worldTokenRef.current;
    sendingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const res = await api.play.continue(worldName, userInput, {
        requestId: options.requestId,
        expectedRevision: revisionRef.current,
        ...modeOpts(),
      });
      if (token !== worldTokenRef.current) return;
      if (typeof res?.revision === 'number') {
        revisionRef.current = res.revision;
      }
      pendingActionRef.current = null;
      const chapter = res.chapter;
      setTurns(prev => [...prev, {
        input: userInput,
        output: chapter.chapter_text,
        chapterIndex: chapter.chapter_index,
        turnIndex: chapter.turn_index,
        checkpointId: chapter.checkpoint_id,
        timestamp: Date.now(),
      }]);
      // Only clear the box if the player has not already typed the next action,
      // and only on success, so a failed send can be retried unchanged. A draft
      // retry never touches the box at all.
      if (options.clearInputOnSuccess) {
        setInput(prev => (prev.trim() === userInput ? '' : prev));
      }
      // A committed turn replaces any pending not-saved draft.
      setDraft(null);
      storeDraft(null);
      await reloadCommittedData();
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      const detail = e?.detail;
      if (detail && detail.persisted === false && typeof detail.draft_chapter_text === 'string') {
        const nextDraft: PlayDraft = {
          worldName,
          userInput,
          requestId: options.requestId,
          text: detail.draft_chapter_text,
          status: detail.status || 'unavailable',
          reason: detail.reason || '',
          message: detail.message || e.message || '',
          createdAt: Date.now(),
        };
        setDraft(nextDraft);
        storeDraft(nextDraft);
      }
      setError(e.message || 'Failed to submit action turn');
    } finally {
      // Only the request that still owns the active world may release the lock;
      // a late response from a previous world must not unlock the new one.
      if (token === worldTokenRef.current) {
        sendingRef.current = false;
        setLoading(false);
      }
    }
  };

  const handleSend = () => {
    const text = input.trim();
    if (!text || sendingRef.current) return;
    // Reuse the in-flight id when the player retries the same action, otherwise
    // this is a new action and gets a new id.
    const reuse = pendingActionRef.current?.userInput === text;
    const requestId = reuse ? pendingActionRef.current!.id : makeRequestId();
    pendingActionRef.current = { id: requestId, userInput: text };
    return submitAction(text, { clearInputOnSuccess: true, requestId });
  };

  const handlePreviewTravel = async (location: { name?: string; id?: string }) => {
    const destination = location?.name || location?.id;
    if (!destination) return;
    const token = worldTokenRef.current;
    const ticket = previewTicketRef.current + 1;
    previewTicketRef.current = ticket;
    previewDestinationRef.current = destination;
    // Drop the previous result *before* the request goes out. Keeping it while
    // the new one is in flight means the map draws a route to the old
    // destination next to the new selection — and "Travel here" stays available
    // for a journey the player has not had checked yet. Re-requesting the same
    // destination must invalidate the old answer just as thoroughly: it was
    // computed from older world state.
    setTravelPreview(null);
    setPreviewLoading(true);

    /** True when this response is still the newest request for the live world. */
    const isCurrent = () =>
      ticket === previewTicketRef.current
      && token === worldTokenRef.current
      && previewDestinationRef.current === destination;

    try {
      const preview = await api.map.previewTravel(worldName, destination);
      if (!isCurrent()) return;
      // Defence in depth: the engine echoes the destination back, so a response
      // that does not name the place we asked for is not ours to trust.
      if (preview && preview.destination && preview.destination !== destination) return;
      setTravelPreview(preview);
    } catch (e: any) {
      if (!isCurrent()) return;
      setTravelPreview(null);
      setError(e.message || 'Failed to preview journey');
    } finally {
      // Only the newest request may report "no longer loading"; a stale one
      // clearing the flag would hide the spinner for a request still in flight.
      if (isCurrent()) setPreviewLoading(false);
    }
  };

  const handleTravelTo = (location: { name?: string; id?: string }) => {
    const destination = location?.name || location?.id;
    if (!destination) return;
    setInput(`Travel to ${destination}.`);
    setTravelPreview(null);
    setActiveDrawer(null);
  };

  const handleItemAction = (verb: 'Inspect' | 'Use' | 'Equip' | 'Unequip' | 'Drop', item: InventoryItem) => {
    const reference = item.custom_name || item.name;
    setInput(`${verb} ${reference}.`);
    setActiveDrawer(null);
  };

  const handleContinueJourney = () => {
    setInput('Continue journey.');
    setActiveDrawer(null);
  };

  const handleAbandonJourney = () => {
    setInput('Abandon journey.');
    setActiveDrawer(null);
  };

  const handlePreviewTimeSkip = async (request: TimeSkipRequest) => {
    const token = worldTokenRef.current;
    setTimeSkipLoading(true);
    setTimeSkipPreview(null);
    setError(null);
    try {
      const preview = await api.play.previewTimeSkip(worldName, request);
      if (token !== worldTokenRef.current) return;
      setTimeSkipPreview(preview);
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      setError(e.message || 'Failed to preview time skip');
    } finally {
      if (token === worldTokenRef.current) setTimeSkipLoading(false);
    }
  };

  const handleExecuteTimeSkip = async (request: TimeSkipRequest) => {
    if (sendingRef.current) return;
    const token = worldTokenRef.current;
    const requestId = makeRequestId();
    sendingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const res = await api.play.executeTimeSkip(worldName, {
        ...request, request_id: requestId, expected_revision: revisionRef.current, ...modeBody(),
      });
      if (token !== worldTokenRef.current) return;
      if (typeof res?.revision === 'number') revisionRef.current = res.revision;
      setDraft(null);
      storeDraft(null);
      setTimeSkipOpen(false);
      setTimeSkipPreview(null);
      await Promise.all([loadExistingChapters(), reloadCommittedData()]);
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      const detail = e?.detail;
      if (detail?.persisted === false && detail?.draft_chapter_text) {
        const nextDraft: PlayDraft = {
          worldName, userInput: `Time skip: ${request.activity || 'Pass the time'}`,
          text: detail.draft_chapter_text, status: detail.status || 'blocked',
          reason: detail.reason || 'time_skip_blocked', message: detail.message || e.message,
          requestId, createdAt: Date.now(),
        };
        setDraft(nextDraft);
        storeDraft(nextDraft);
      }
      setError(e.message || 'Failed to advance time');
    } finally {
      if (token === worldTokenRef.current) {
        sendingRef.current = false;
        setLoading(false);
      }
    }
  };

  const handleRetryDraft = () => {
    if (!draft) return;
    // Resend the action that produced the draft, regardless of what the player
    // is currently typing. The input box is left untouched. Reusing the stored
    // request id lets the backend replay a turn that actually committed.
    return submitAction(draft.userInput, {
      clearInputOnSuccess: false,
      // A draft persisted by an older build may predate request ids; mint one
      // rather than sending `undefined` (the backend would reject the replay).
      requestId: draft.requestId ?? makeRequestId(),
    });
  };

  const handleStartChapter = async () => {
    if (sendingRef.current) return;
    const token = worldTokenRef.current;
    sendingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      // A prelude is Chapter 0, not a played checkpoint. Check committed
      // chapters, not arc progress, so Begin Story still starts Chapter 1.
      const chapters = await api.play.getChapters(worldName);
      if (token !== worldTokenRef.current) return;
      if (chapters?.some((chapter) => chapter.chapter_index !== 0)) {
        await loadExistingChapters();
        return;
      }
      const res = await api.play.start(worldName, { opening_mode: 'ai_generate', ...modeBody() });
      if (token !== worldTokenRef.current) return;
      if (res?.chapter?.chapter_text) {
        setTurns([{
          input: 'Start Chapter',
          output: res.chapter.chapter_text,
          chapterIndex: res.chapter.chapter_index || 1,
          turnIndex: res.chapter.turn_index || 0,
          checkpointId: res.chapter.checkpoint_id || 'cp_1',
          timestamp: Date.now(),
        }]);
      }
      await reloadCommittedData();
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      setError(e.message || 'Failed to start chapter');
    } finally {
      // Only the request that still owns the active world may release the lock;
      // a late response from a previous world must not unlock the new one.
      if (token === worldTokenRef.current) {
        sendingRef.current = false;
        setLoading(false);
      }
    }
  };

  const handleRegenerate = async (replacementInput?: string): Promise<boolean> => {
    if (sendingRef.current) return false;
    const revisedInput = replacementInput?.trim();
    if (replacementInput !== undefined && !revisedInput) {
      setError('Enter a revised action before rerolling.');
      return false;
    }
    const token = worldTokenRef.current;
    const actionIdentity = revisedInput === undefined ? '__regenerate__' : `__regenerate__:${revisedInput}`;
    const reuse = pendingActionRef.current?.userInput === actionIdentity;
    const requestId = reuse ? pendingActionRef.current!.id : makeRequestId();
    pendingActionRef.current = { id: requestId, userInput: actionIdentity };
    sendingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const res = await api.play.regenerate(worldName, {
        requestId,
        expectedRevision: revisionRef.current,
        userInput: revisedInput,
        ...modeOpts(),
      });
      if (token !== worldTokenRef.current) return false;
      if (typeof res?.revision === 'number') {
        revisionRef.current = res.revision;
      }
      pendingActionRef.current = null;
      await Promise.all([loadExistingChapters(), reloadCommittedData()]);
      return true;
    } catch (e: any) {
      if (token !== worldTokenRef.current) return false;
      setError(e.message || 'Failed to regenerate chapter');
      return false;
    } finally {
      // Only the request that still owns the active world may release the lock;
      // a late response from a previous world must not unlock the new one.
      if (token === worldTokenRef.current) {
        sendingRef.current = false;
        setLoading(false);
      }
    }
  };

  const handleLoadEndgameChoices = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.play.epilogueChoices(worldName);
      setEpilogueChoices(res?.choices || []);
    } catch (e: any) {
      setError(e.message || 'Failed to load epilogue choices');
    } finally {
      setLoading(false);
    }
  };

  const handleChooseEnding = async (choice: string) => {
    if (sendingRef.current) return;
    const token = worldTokenRef.current;
    sendingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      await api.play.generateEpilogue(worldName, choice);
      if (token !== worldTokenRef.current) return;
      setEpilogueChoices([]);
      await Promise.all([loadExistingChapters(), reloadCommittedData()]);
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      setError(e.message || 'Failed to write the epilogue');
    } finally {
      if (token === worldTokenRef.current) {
        sendingRef.current = false;
        setLoading(false);
      }
    }
  };

  const handleGeneratePrelude = async () => {
    const token = worldTokenRef.current;
    setLoading(true);
    try {
      const p = await api.play.prelude.generate(worldName);
      if (token !== worldTokenRef.current) return;
      setPreludeText(p?.prelude?.chapter_text || p?.prelude_text || '');
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      setError(e.message || 'Failed to generate prelude');
    } finally {
      if (token === worldTokenRef.current) {
        setLoading(false);
      }
    }
  };

  const handleConfirmPrelude = async () => {
    if (sendingRef.current) return;
    const token = worldTokenRef.current;
    sendingRef.current = true;
    setLoading(true);
    try {
      await api.play.prelude.confirm(worldName);
      if (token !== worldTokenRef.current) return;
      setPreludeText(null);
      // After confirming prelude, start the first chapter
      const res = await api.play.start(worldName, { opening_mode: 'ai_generate', ...modeBody() });
      if (token !== worldTokenRef.current) return;
      if (res?.chapter?.chapter_text) {
        setTurns([{
          input: 'Start Chapter',
          output: res.chapter.chapter_text,
          chapterIndex: res.chapter.chapter_index || 1,
          turnIndex: res.chapter.turn_index || 0,
          checkpointId: res.chapter.checkpoint_id || 'cp_1',
          timestamp: Date.now(),
        }]);
      }
      await reloadCommittedData();
    } catch (e: any) {
      if (token !== worldTokenRef.current) return;
      setError(e.message || 'Failed to confirm prelude');
    } finally {
      // Only the request that still owns the active world may release the lock;
      // a late response from a previous world must not unlock the new one.
      if (token === worldTokenRef.current) {
        sendingRef.current = false;
        setLoading(false);
      }
    }
  };

  const handleDismissDraft = () => {
    setDraft(null);
    storeDraft(null);
  };

  const protagonist = playState?.protagonist;
  const arc = playState?.arc_progress;
  const clock = playState?.story_clock || {};
  const calendar = playState?.calendar || null;

  return { playState, turns, input, setInput, loading, error, setError, outputLength, setOutputLength, narrationMode, setNarrationMode, sidebarOpen, setSidebarOpen, expandedTurns, toggleTurnExpanded, collapseAllPrevious, expandAllTurns, activeDrawer, setActiveDrawer, locations, affinityGraph, preludeText, draft, handleDismissDraft, handleRetryDraft, quests, journal, epilogue: playState?.epilogue || null, lifecycleStatus: playState?.lifecycle_status || 'active', epilogueChoices, handleLoadEndgameChoices, handleChooseEnding, chatEndRef, handleSend, handlePreviewTravel, travelPreview, previewLoading, handleTravelTo, handleItemAction, handleContinueJourney, handleAbandonJourney, timeSkipOpen, setTimeSkipOpen, timeSkipPreview, timeSkipLoading, handlePreviewTimeSkip, handleExecuteTimeSkip, handleStartChapter, handleRegenerate, handleGeneratePrelude, handleConfirmPrelude, protagonist, arc, clock, calendar };
}

export type PlaySession = ReturnType<typeof usePlaySession>;
