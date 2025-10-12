export interface PaginatedResponse<T> {
  results: T[];
  limit: number;
  offset: number;
  next_offset: number | null;
  total_estimate: number | null;
}

export interface MovieRow {
  id: string;
  path: string;
  title?: string;
  year?: number;
  poster_thumb?: string | null;
  contact_sheet?: string | null;
  confidence: number;
  quality?: number | null;
  langs_audio: string[];
  langs_subs: string[];
  drive?: string | null;
}

export interface SeriesRow {
  id: string;
  series_root: string;
  title?: string;
  year?: number;
  confidence: number;
  seasons_found: number;
  poster_thumb?: string | null;
  drive?: string | null;
}

export interface SeasonRow {
  id: string;
  season_path: string;
  season_number?: number;
  episodes_found?: number;
  expected?: number;
  confidence: number;
  drive?: string | null;
}

export interface EpisodeRow {
  id: string;
  episode_path: string;
  season_number?: number;
  episode_numbers: string[];
  air_date?: string;
  title?: string;
  confidence: number;
  quality?: number | null;
  poster_thumb?: string | null;
  drive?: string | null;
}

export interface MovieDetail {
  id: string;
  title?: string;
  year?: number;
  drive?: string;
  folder_path: string;
  main_video_path?: string;
  runtime_minutes?: number;
  overview?: string;
  confidence: number;
  confidence_components: { label: string; score: number }[];
  quality_score?: number;
  quality_reasons: string[];
  audio_langs: string[];
  subs_langs: string[];
  subs_present: boolean;
  video_codec?: string;
  resolution: { width?: number | null; height?: number | null };
  container?: string | null;
  ids: Record<string, unknown>;
  issues: string[];
  candidates: {
    source?: string;
    candidate_id?: string;
    title?: string;
    year?: number;
    score: number;
    extra: Record<string, unknown>;
  }[];
  review_reasons: string[];
  review_questions: string[];
  assets: Record<string, unknown>;
  signals: Record<string, unknown>;
  poster_thumb?: string | null;
  contact_sheet?: string | null;
  open_plan: { plan: string; path: string };
}

export interface EpisodeDetail {
  id: string;
  title?: string;
  drive?: string;
  episode_path: string;
  series_root?: string;
  season_number?: number;
  episode_numbers: string[];
  air_date?: string;
  ids: Record<string, unknown>;
  confidence: number;
  audio_langs: string[];
  subs_langs: string[];
  subs_present: boolean;
  quality_score?: number;
  quality_reasons: string[];
  runtime_minutes?: number;
  video_codec?: string;
  resolution: { width?: number | null; height?: number | null };
  series: Record<string, unknown>;
  season: Record<string, unknown>;
  poster_thumb?: string | null;
  open_plan: { plan: string; path: string };
}

export interface CatalogSummary {
  totals: { movies: number; series: number; episodes: number };
  review_queue: {
    movies: Array<{ id: string; title?: string; year?: number; confidence: number; drive?: string }>;
    episodes: Array<{ id: string; title?: string; season?: number; confidence: number; drive?: string }>;
  };
}

export interface SearchHit {
  id: string;
  kind: string;
  title?: string;
  drive?: string;
  confidence?: number;
  context: Record<string, unknown>;
}

export interface SearchResponse {
  mode: string;
  query: string;
  results: SearchHit[];
}

export interface CatalogEventPayload {
  seq: number;
  kind: string;
  ts_utc?: string | null;
  payload: Record<string, unknown>;
}

export interface AssistantStatus {
  requested: boolean;
  gpu_ready: boolean;
  enabled: boolean;
  disabled_by_gpu: boolean;
  message: string;
  gpu: Record<string, unknown>;
  runtime?: Record<string, unknown>;
}

export type PlaylistOrderMode = 'weighted' | 'sort_quality' | 'sort_confidence';

export interface PlaylistCandidateRow {
  id: string;
  kind: 'movie' | 'episode' | string;
  drive?: string | null;
  path: string;
  masked_path: string;
  title?: string;
  year?: number;
  duration_min?: number | null;
  quality?: number | null;
  confidence: number;
  langs_audio: string[];
  langs_subs: string[];
  subs_present: boolean;
  genres: string[];
}

export interface PlaylistSuggestResponse {
  limit: number;
  candidates: PlaylistCandidateRow[];
}

export interface PlaylistBuildItemRow {
  id: string;
  kind: 'movie' | 'episode' | string;
  drive?: string | null;
  title?: string;
  year?: number;
  duration_min?: number | null;
  cumulative_minutes: number;
  path: string;
  masked_path: string;
  quality?: number | null;
  confidence: number;
  langs_audio: string[];
  langs_subs: string[];
  subs_present: boolean;
  open_plan: { plan: string; path: string };
}

export interface PlaylistBuildResponse {
  mode: PlaylistOrderMode;
  target_minutes?: number | null;
  total_minutes: number;
  items: PlaylistBuildItemRow[];
}

export interface PlaylistExportPayload {
  path: string;
  title?: string;
}

export interface PlaylistExportResponse {
  ok: boolean;
  format: 'm3u' | 'csv';
  path: string;
  count: number;
}

export interface PlaylistAiPlanItem {
  id: string;
  reason?: string;
  confidence?: number;
  quality?: number | null;
}

export interface PlaylistAiResponse {
  mode: string;
  items: PlaylistAiPlanItem[];
  reasoning: string;
  sources: string[];
}

export interface AssistantAskSource {
  type: string;
  ref: string;
}

export interface AssistantToolCall {
  tool?: string;
  payload?: Record<string, unknown>;
}

export interface AssistantAskResponse {
  answer_markdown: string;
  sources: AssistantAskSource[];
  tool_calls: AssistantToolCall[];
  elapsed_ms: number;
  status: Record<string, unknown>;
}

export interface RealtimeClientStatus {
  client_id: string;
  last_seen_utc: string | null;
  stale: boolean;
}

export interface RealtimeStatus {
  ts_utc: string;
  ws_connected: number;
  sse_connected: number;
  events_pushed_total: number;
  events_dropped_total: number;
  event_lag_ms_p50: number | null;
  event_lag_ms_p95: number | null;
  last_event_utc: string | null;
  last_event_age_ms: number | null;
  ai_requests_total: number;
  ai_errors_total: number;
  client?: RealtimeClientStatus | null;
}
