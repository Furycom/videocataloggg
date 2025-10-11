from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage

from .tools import AssistantTooling


@dataclass
class EpisodeProposal:
    path: str
    title: str
    year: Optional[int]
    season: Optional[int]
    episode: Optional[int]
    confidence: Optional[float]
    tmdb_id: Optional[str]
    imdb_id: Optional[str]
    score: float
    reasons: List[str]
    rag_snippet: Optional[str]
    candidate_source: Optional[str]
    api_note: Optional[str]


class EpisodeAssistant:
    """Rule-based vertical slice for low-confidence TV episode assistance."""

    def __init__(self, tooling: AssistantTooling) -> None:
        self.tooling = tooling

    # ------------------------------------------------------------------
    def can_handle(self, text: str) -> bool:
        normalized = text.lower()
        return "low-confidence" in normalized and "episode" in normalized

    def run(self, request: str) -> Dict[str, Any]:
        tool_log: List[Dict[str, Any]] = []

        def call(name: str, **kwargs):
            try:
                payload = self.tooling.execute(name, kwargs)
                tool_log.append({"tool": name, "arguments": payload["arguments"], "result": payload["result"]})
                return payload["result"]
            except Exception as exc:  # pragma: no cover - defensive
                tool_log.append({"tool": name, "error": str(exc)})
                return {"error": str(exc)}

        low_conf = call("db_get_low_confidence", type="episode", min=0.0, max=0.79, limit=10)
        items = low_conf.get("items") if isinstance(low_conf, dict) else None
        if not items:
            answer = (
                "I checked the catalog but didn't find any low-confidence TV episodes to review right now. "
                "Try rescanning or widening the confidence range."
            )
            return {"answer": answer, "tool_log": tool_log, "messages": [AIMessage(content=answer)]}

        proposals: List[EpisodeProposal] = []
        api_errors: List[str] = []
        for item in items[:10]:
            path = item.get("path")
            if not path:
                continue
            candidates_payload = call("db_get_candidates", path=path, top_k=5)
            candidates = candidates_payload.get("candidates") if isinstance(candidates_payload, dict) else []
            profile = candidates_payload.get("profile") if isinstance(candidates_payload, dict) else {}
            evidence = candidates_payload.get("evidence") if isinstance(candidates_payload, dict) else {}
            best_candidate = self._choose_candidate(candidates)

            rag_query = self._build_rag_query(item, profile)
            rag_hits = call("db_search_semantic", query=rag_query, top_k=3, filter={"type": "doc"})
            rag_snippet = None
            if isinstance(rag_hits, dict):
                results = rag_hits.get("results") or []
                if results:
                    hit = results[0]
                    text = hit.get("text") or ""
                    rag_snippet = text[:180].strip()

            api_note = None
            if best_candidate is None and self.tooling.tmdb_api_key:
                lookup = call(
                    "api_tmdb_lookup_cached",
                    kind="episode",
                    title=item.get("parsed_title") or profile.get("parsed_title") or "",
                    year=item.get("parsed_year") or profile.get("parsed_year"),
                    season=item.get("season") or profile.get("season"),
                    episode=item.get("episode") or self._first_episode(profile),
                    top_k=3,
                )
                if isinstance(lookup, dict):
                    results = lookup.get("results")
                    if results:
                        first = results[0]
                        episode_payload = first.get("episode") if isinstance(first, dict) else None
                        tmdb_id = (episode_payload or {}).get("id") if isinstance(episode_payload, dict) else None
                        if tmdb_id:
                            best_candidate = {
                                "tmdb_id": str(tmdb_id),
                                "imdb_id": None,
                                "title": first.get("name"),
                                "season": item.get("season"),
                                "episodes": [item.get("episode")],
                                "score": 0.45,
                                "source": "tmdb_lookup",
                            }
                        else:
                            api_note = "Top TMDb match returned but episode details were missing."
                    elif lookup.get("error"):
                        api_errors.append(str(lookup.get("error")))
                elif isinstance(lookup, dict) and lookup.get("error"):
                    api_errors.append(str(lookup.get("error")))

            tmdb_id = best_candidate.get("tmdb_id") if best_candidate else None
            imdb_id = best_candidate.get("imdb_id") if best_candidate else None
            confidence = item.get("confidence")
            signals = self._collect_signals(item, best_candidate, evidence, rag_snippet, api_note)
            score = self._score_item(confidence, best_candidate, rag_snippet, evidence)
            proposals.append(
                EpisodeProposal(
                    path=path,
                    title=item.get("parsed_title") or profile.get("parsed_title") or "Unknown title",
                    year=item.get("parsed_year") or profile.get("parsed_year"),
                    season=item.get("season") or profile.get("season"),
                    episode=item.get("episode") or self._first_episode(profile),
                    confidence=confidence,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    score=score,
                    reasons=signals,
                    rag_snippet=rag_snippet,
                    candidate_source=(best_candidate or {}).get("source"),
                    api_note=api_note,
                )
            )

        if not proposals:
            answer = "I could not assemble proposals for those episodes because the catalog entries were incomplete."
            return {"answer": answer, "tool_log": tool_log, "messages": [AIMessage(content=answer)]}

        top = sorted(proposals, key=lambda entry: entry.score, reverse=True)[:3]
        export_rows = [
            {
                "path": prop.path,
                "title": prop.title,
                "season": prop.season,
                "episode": prop.episode,
                "tmdb_id": prop.tmdb_id or "",
                "imdb_id": prop.imdb_id or "",
                "confidence": None if prop.confidence is None else round(prop.confidence, 3),
                "signals": "; ".join(prop.reasons),
            }
            for prop in top
        ]

        export_result = call(
            "export_csv",
            rows=export_rows,
            name="assistant_low_confidence_episodes.csv",
            dry_run=True,
        )

        open_plan = call("help_open_folder", path=top[0].path if top else items[0]["path"])

        answer = self._render_answer(top, export_result, open_plan, api_errors)
        return {"answer": answer, "tool_log": tool_log, "messages": [AIMessage(content=answer)]}

    # ------------------------------------------------------------------
    def _choose_candidate(self, candidates: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None
        scored = [c for c in candidates if isinstance(c, dict)]
        if not scored:
            return None
        return max(scored, key=lambda c: c.get("score", 0))

    def _build_rag_query(self, item: Dict[str, Any], profile: Dict[str, Any]) -> str:
        title = item.get("parsed_title") or profile.get("parsed_title") or ""
        season = item.get("season") or profile.get("season")
        episode = item.get("episode") or self._first_episode(profile)
        parts = [title]
        if season is not None:
            parts.append(f"season {season}")
        if episode is not None:
            parts.append(f"episode {episode}")
        return " ".join(str(part) for part in parts if part)

    @staticmethod
    def _first_episode(profile: Dict[str, Any]) -> Optional[int]:
        episodes = profile.get("episodes") if isinstance(profile, dict) else None
        if isinstance(episodes, list) and episodes:
            first = episodes[0]
            try:
                return int(first)
            except (TypeError, ValueError):
                return None
        return None

    def _collect_signals(
        self,
        item: Dict[str, Any],
        candidate: Optional[Dict[str, Any]],
        evidence: Dict[str, Any],
        rag_snippet: Optional[str],
        api_note: Optional[str],
    ) -> List[str]:
        signals = []
        for reason in item.get("reasons") or []:
            signals.append(f"catalog:{reason}")
        if candidate:
            src = candidate.get("source") or "candidate"
            if candidate.get("tmdb_id"):
                signals.append(f"{src}:tmdb")
            if candidate.get("imdb_id"):
                signals.append(f"{src}:imdb")
        if evidence.get("plot_score"):
            signals.append(f"plot_match={round(float(evidence['plot_score']), 2)}")
        if rag_snippet:
            signals.append("rag-match")
        if api_note:
            signals.append("tmdb-note")
        return signals

    def _score_item(
        self,
        confidence: Optional[float],
        candidate: Optional[Dict[str, Any]],
        rag_snippet: Optional[str],
        evidence: Dict[str, Any],
    ) -> float:
        score = float(confidence or 0.0)
        if candidate and candidate.get("tmdb_id"):
            score += 0.25
        if candidate and candidate.get("imdb_id"):
            score += 0.15
        if rag_snippet:
            score += 0.1
        if evidence.get("plot_score"):
            score += min(0.2, float(evidence["plot_score"]) / 10)
        return score

    def _render_answer(
        self,
        proposals: List[EpisodeProposal],
        export_result: Dict[str, Any],
        open_plan: Dict[str, Any],
        api_errors: List[str],
    ) -> str:
        lines: List[str] = []
        lines.append("Here are the low-confidence TV episodes that look most promising:")
        for idx, proposal in enumerate(proposals, 1):
            heading = f"{idx}. {proposal.title}"
            if proposal.year:
                heading += f" ({proposal.year})"
            details = []
            if proposal.season is not None:
                details.append(f"S{proposal.season}")
            if proposal.episode is not None:
                details.append(f"E{proposal.episode}")
            if details:
                heading += " – " + "".join(details)
            lines.append(heading)
            lines.append(f"   Path: {proposal.path}")
            id_parts = []
            id_parts.append(f"TMDb {proposal.tmdb_id}" if proposal.tmdb_id else "TMDb —")
            id_parts.append(f"IMDb {proposal.imdb_id}" if proposal.imdb_id else "IMDb —")
            lines.append("   Proposed IDs: " + ", ".join(id_parts))
            conf_text = "unknown" if proposal.confidence is None else f"{proposal.confidence:.2f}"
            lines.append(f"   Confidence: {conf_text}; signals: {', '.join(proposal.reasons) or 'none'}")
            if proposal.rag_snippet:
                lines.append(f"   RAG context: {proposal.rag_snippet}")
            if proposal.api_note:
                lines.append(f"   TMDb lookup: {proposal.api_note}")
        lines.append("")
        lines.append("Next actions:")
        if open_plan.get("action") == "open":
            lines.append(f"- Open folder plan ready for {open_plan.get('path')}")
        else:
            lines.append("- Open folder plan unavailable (path not provided).")
        if export_result.get("dry_run"):
            lines.append(
                "- CSV preview prepared (dry-run). Use Export CSV in the Assistant panel to write "
                f"{export_result.get('row_count', 0)} rows to {export_result.get('path')}"
            )
        else:
            lines.append(f"- CSV export saved to {export_result.get('path')}")
        remaining = max(0, self.tooling.budget - self.tooling.calls)
        lines.append(f"Tool budget remaining: {remaining}/{self.tooling.budget}")
        if api_errors:
            lines.append("API notes: " + "; ".join(dict.fromkeys(api_errors)))
        return "\n".join(lines)
