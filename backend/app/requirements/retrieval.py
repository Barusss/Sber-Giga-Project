"""Candidate retrieval from ZnP atoms to TU requirements.

Scores rank candidates; neither scores nor a bounded batch establish compliance
or completeness. The caller must validate evidence and handle remaining pages.
"""
from dataclasses import asdict, is_dataclass

from app.ai.embeddings import EmbeddingService
from app.requirements.tools import tool_search_znp_clauses


class RequirementRetriever:
    def __init__(self, requirements, embedder: EmbeddingService, *, batch_size=10,
                 neighbor_window=2, semantic_weight=0.6, max_section_size=20):
        if batch_size < 1 or neighbor_window < 0 or max_section_size < 1 or not 0 <= semantic_weight <= 1:
            raise ValueError("Invalid retrieval settings")
        self.requirements = list(requirements)
        if len({item.id for item in self.requirements}) != len(self.requirements):
            raise ValueError("Requirement IDs must be unique")
        self.embedder = embedder
        self.batch_size = batch_size
        self.neighbor_window = neighbor_window
        self.semantic_weight = semantic_weight
        self.max_section_size = max_section_size
        self._last_ranking = None
        vectors = embedder.embed_documents([item.text for item in self.requirements])
        self.vectors = vectors if len(vectors) == len(self.requirements) and vectors else None

    def search(self, atom, *, query=None, offset=0):
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        actual_query = atom.text if query is None else query
        ranking_key = (atom.id, actual_query)
        if offset:
            if self._last_ranking is None or self._last_ranking[0] != ranking_key:
                raise ValueError("Pagination requires restarting this query at offset zero")
            _, ranked, status, degradation_reason, channels = self._last_ranking
        else:
            vector = self.embedder.embed_query(actual_query) if self.vectors is not None else None
            vectors = self.vectors if self.embedder.enabled and vector is not None else None
            ranked = tool_search_znp_clauses(actual_query, self.requirements,
                                             top_k=len(self.requirements), query_embedding=vector,
                                             clause_embeddings=vectors, semantic_weight=self.semantic_weight)
            status = self.embedder.status()
            degradation_reason = None
            if vectors is None:
                degradation_reason = status.get("reason") or "semantic vectors unavailable"
            channels = ["lexical", "semantic"] if vectors is not None else ["lexical"]
            self._last_ranking = (ranking_key, ranked, status, degradation_reason, channels)
        candidates = []
        for item in ranked[offset:offset + self.batch_size]:
            requirement = self.requirements[item["position_index"]]
            candidates.append({**item, "requirement_id": requirement.id,
                               "fragment_id": getattr(requirement, "fragment_id", None),
                               "sources": _serialize_sources(getattr(requirement, "sources", ())),
                               "context_sources": _serialize_sources(getattr(requirement, "context_sources", ())),
                               "warnings": getattr(requirement, "warnings", ()),
                               "page": getattr(requirement, "page", None),
                               "section_path": getattr(requirement, "section_path", None)})
        return {"atom_id": atom.id, "atom_sources": _serialize_sources(atom.sources),
                "atom_context_sources": _serialize_sources(getattr(atom, "context_sources", ())),
                "atom_warnings": atom.warnings,
                "query": actual_query, "candidates": candidates, "offset": offset,
                "has_more": offset + self.batch_size < len(ranked),
                "retrieval_complete": False,
                "degraded": degradation_reason is not None, "degradation_reason": degradation_reason,
                "embedding_status": status, "embedding_current_status": self.embedder.status(),
                "ranking_channels": channels}

    def context(self, requirement_ids, *, known_ids=(), include_section=False):
        return self.context_details(requirement_ids, known_ids=known_ids, include_section=include_section)["items"]

    def context_details(self, requirement_ids, *, known_ids=(), include_section=False):
        known = set(known_ids)
        selected = set()
        targets = set(requirement_ids)
        sections_skipped = []
        visited_sections = set()
        for index, requirement in enumerate(self.requirements):
            if requirement.id not in targets:
                continue
            start = max(0, index - self.neighbor_window)
            end = min(len(self.requirements), index + self.neighbor_window + 1)
            selected.update(range(start, end))
            section = getattr(requirement, "section_path", None)
            if include_section and section and section not in visited_sections:
                visited_sections.add(section)
                section_indices = [i for i, item in enumerate(self.requirements)
                                   if getattr(item, "section_path", None) == section]
                if len(section_indices) <= self.max_section_size:
                    selected.update(section_indices)
                else:
                    sections_skipped.append({"section_path": section, "size": len(section_indices),
                                             "limit": self.max_section_size})
        items = [{"requirement_id": item.id, "clause_number": item.clause_number,
                 "text": item.text, "fragment_id": getattr(item, "fragment_id", None),
                 "sources": _serialize_sources(getattr(item, "sources", ())),
                 "context_sources": _serialize_sources(getattr(item, "context_sources", ())),
                 "warnings": getattr(item, "warnings", ()),
                 "page": getattr(item, "page", None),
                 "section_path": getattr(item, "section_path", None)}
                for index, item in enumerate(self.requirements)
                if index in selected and item.id not in known]
        return {"items": items, "sections_skipped": sections_skipped,
                "unknown_requirement_ids": sorted(targets - {item.id for item in self.requirements})}


def _serialize_sources(sources):
    return [asdict(source) if is_dataclass(source) else source for source in sources]
