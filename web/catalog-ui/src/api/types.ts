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
  message: string;
  gpu: Record<string, unknown>;
  runtime?: Record<string, unknown>;
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
