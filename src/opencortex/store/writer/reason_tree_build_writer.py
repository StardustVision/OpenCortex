# SPDX-License-Identifier: Apache-2.0
"""LLM-enhanced reason-tree index writer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from opencortex.prompts.schemas import ReasonTreeNode, ReasonTreeOutput
from opencortex.prompts.write import (
    RESOURCE_TREE_SYSTEM_PROMPT,
    SESSION_TREE_SYSTEM_PROMPT,
    build_resource_tree_prompt,
    build_session_tree_prompt,
)
from opencortex.store.event.events import MemoryEvent
from opencortex.store.types import ContextType, SessionRecordLayer
from opencortex.store.writer.event_payload import (
    digest,
    event_content,
    event_record_id,
    event_uri,
    primary_record,
)
from opencortex.store.writer.reason_tree_index_writer import (
    ReasonTreeIndexRecord,
    source_references,
)
from opencortex.utils.json_parse import parse_json_from_response


class TreeNodeIndexRecord(BaseModel):
    """Flattened reason-tree node ready for vector indexing."""

    node: ReasonTreeNode
    uri: str
    parent_uri: str
    level: int
    path_segments: list[str]


class ReasonTreeBuildWriter:
    """Build LLM-enhanced index nodes for resource and session records."""

    max_nodes = 32

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        llm_completion: Any,
        embedder: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.llm_completion = llm_completion
        self.embedder = embedder

    async def write(self, event: MemoryEvent) -> None:
        """Build and write enhanced reason-tree index records."""
        record = primary_record(event)
        if not self.should_build(record):
            return
        content = str(record.get("content") or event_content(event) or "")
        if not content:
            return
        output = await self.build_tree(event=event, record=record, content=content)
        nodes = self.flatten_nodes(
            nodes=output.nodes,
            root_uri=event_uri(event),
            parent_uri=event_uri(event),
        )
        for node_record in nodes[: self.max_nodes]:
            payload = self.index_payload(event, record, output, node_record)
            self.embed_record(payload)
            await self.vector_store.upsert(self.collection_resolver(), payload)

    async def build_tree(
        self,
        *,
        event: MemoryEvent,
        record: dict[str, Any],
        content: str,
    ) -> ReasonTreeOutput:
        """Run the source-specific reason-tree prompt."""
        context_type = str(record.get("context_type", "") or "")
        metadata = dict(record.get("meta") or {})
        uri = event_uri(event)
        if context_type == str(ContextType.RESOURCE):
            prompt = build_resource_tree_prompt(
                content=content,
                uri=uri,
                metadata=metadata,
            )
            system_prompt = RESOURCE_TREE_SYSTEM_PROMPT
        else:
            prompt = build_session_tree_prompt(
                content=content,
                uri=uri,
                metadata=metadata,
            )
            system_prompt = SESSION_TREE_SYSTEM_PROMPT
        response = await self.llm_completion(prompt, system_prompt=system_prompt)
        parsed = parse_json_from_response(response)
        if not isinstance(parsed, dict):
            raise ValueError("Reason-tree build must return a JSON object")
        return ReasonTreeOutput.model_validate(parsed)

    @staticmethod
    def should_build(record: dict[str, Any]) -> bool:
        """Return whether this primary record should get an enhanced tree."""
        if not record or not bool(record.get("retrieval_ready", False)):
            return False
        context_type = str(record.get("context_type", "") or "")
        meta = dict(record.get("meta") or {})
        chunk_role = str(meta.get("chunk_role", "") or "")
        if context_type == str(ContextType.RESOURCE):
            return chunk_role in {"", "resource", "document_root"}
        layer = str(meta.get("layer", "") or "")
        return layer == str(SessionRecordLayer.FINAL) and chunk_role in {"", "final"}

    def index_payload(
        self,
        event: MemoryEvent,
        record: dict[str, Any],
        output: ReasonTreeOutput,
        node_record: TreeNodeIndexRecord,
    ) -> dict[str, Any]:
        """Build one enhanced reason-tree index payload."""
        source_uri = event_uri(event)
        meta = dict(record.get("meta") or {})
        source_uris = list(meta.get("source_uris") or [])
        merged_uris = list(meta.get("merged_uris") or [])
        refs = source_references(record, meta, source_uris, merged_uris)
        source_refs = unique_strings([*node_record.node.source_refs, *refs])
        fact_points = unique_strings(node_record.node.fact_points)
        return ReasonTreeIndexRecord(
            id=node_record.uri,
            uri=node_record.uri,
            parent_uri=node_record.parent_uri,
            source_uri=source_uri,
            source_record_id=event_record_id(event),
            parent_source_uri=str(record.get("parent_uri", "") or ""),
            tree_uri=source_uri,
            path="/".join(node_record.path_segments),
            path_segments=node_record.path_segments,
            level=node_record.level,
            reason_role=self.reason_role(record),
            context_window="children" if node_record.node.children else "self",
            source_uris=source_uris,
            merged_uris=merged_uris,
            context_type=str(record.get("context_type", "") or ""),
            category=str(record.get("category", "") or ""),
            title=node_record.node.title,
            summary=node_record.node.summary,
            abstract=output.abstract,
            overview=output.overview,
            fact_points=fact_points,
            source_refs=source_refs,
            is_leaf=not node_record.node.children,
            source_tenant_id=str(record.get("source_tenant_id", event.tenant_id) or ""),
            source_user_id=str(record.get("source_user_id", event.user_id) or ""),
            project_id=str(record.get("project_id", event.project_id) or ""),
            scope=str(record.get("scope", "") or ""),
            session_id=str(
                record.get("session_id", getattr(event, "session_id", "")) or ""
            ),
            entities=list(record.get("entities") or []),
            keywords=str(record.get("keywords", "") or ""),
            memory_kind=str(record.get("memory_kind", "") or ""),
            cone_neighbors=unique_strings(
                [
                    str(record.get("parent_uri", "") or ""),
                    *source_uris,
                    *merged_uris,
                ]
            ),
            meta={
                **meta,
                "index_name": "ReasonTreeBuild",
                "source_uri": source_uri,
                "source_record_id": event_record_id(event),
                "tree_uri": source_uri,
                "title": node_record.node.title,
                "summary": node_record.node.summary,
                "fact_points": fact_points,
                "source_refs": source_refs,
            },
        ).model_dump(mode="json")

    def embed_record(self, record: dict[str, Any]) -> None:
        """Attach a dense vector to the enhanced reason-tree record."""
        if self.embedder is None:
            raise RuntimeError("ReasonTreeBuildWriter requires an embedder")
        text = str(record.get("summary") or record.get("overview") or "")
        result = self.embedder.embed(text)
        if getattr(result, "dense_vector", None):
            record["vector"] = result.dense_vector
        else:
            raise ValueError("Reason-tree build embedding returned no dense vector")
        if getattr(result, "sparse_vector", None):
            record["sparse_vector"] = result.sparse_vector

    def flatten_nodes(
        self,
        *,
        nodes: list[ReasonTreeNode],
        root_uri: str,
        parent_uri: str,
        path: tuple[int, ...] = (),
    ) -> list[TreeNodeIndexRecord]:
        """Flatten tree nodes into deterministic index records."""
        records: list[TreeNodeIndexRecord] = []
        for index, node in enumerate(nodes, start=1):
            node_path = (*path, index)
            path_segments = [str(part) for part in node_path]
            node_uri = f"{root_uri.rstrip('/')}/reason_tree/{node_key(node_path, node)}"
            records.append(
                TreeNodeIndexRecord(
                    node=node,
                    uri=node_uri,
                    parent_uri=parent_uri,
                    level=len(node_path),
                    path_segments=path_segments,
                )
            )
            records.extend(
                self.flatten_nodes(
                    nodes=node.children,
                    root_uri=root_uri,
                    parent_uri=node_uri,
                    path=node_path,
                )
            )
        return records

    @staticmethod
    def reason_role(record: dict[str, Any]) -> str:
        """Return source-specific role for enhanced reason-tree nodes."""
        if str(record.get("context_type", "") or "") == str(ContextType.RESOURCE):
            return "resource_tree_node"
        return "session_tree_node"


def node_key(path: tuple[int, ...], node: ReasonTreeNode) -> str:
    """Return a stable reason-tree node key."""
    path_text = "-".join(str(part) for part in path)
    return f"{path_text}-{digest(node.title or node.summary)}"


def unique_strings(values: list[Any]) -> list[str]:
    """Return non-empty unique strings preserving order."""
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
