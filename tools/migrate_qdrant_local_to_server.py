# SPDX-License-Identifier: Apache-2.0
"""Migrate an embedded Qdrant collection to a Qdrant server."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from qdrant_client import AsyncQdrantClient, models


async def migrate(args: argparse.Namespace) -> None:
    """Copy all points from a local Qdrant path into a remote server."""
    source = AsyncQdrantClient(path=args.source_path)
    target = AsyncQdrantClient(
        url=args.target_url,
        api_key=args.target_api_key or None,
    )
    try:
        collection = args.collection
        info = await source.get_collection(collection)
        if args.recreate and await target.collection_exists(collection):
            await target.delete_collection(collection)
        if not await target.collection_exists(collection):
            await target.create_collection(
                collection_name=collection,
                vectors_config=info.config.params.vectors,
                sparse_vectors_config=info.config.params.sparse_vectors,
            )

        copied = 0
        offset: Any = None
        while True:
            points, offset = await source.scroll(
                collection_name=collection,
                offset=offset,
                limit=args.batch_size,
                with_payload=True,
                with_vectors=True,
            )
            if points:
                await target.upsert(
                    collection_name=collection,
                    points=[
                        models.PointStruct(
                            id=point.id,
                            vector=point.vector or {},
                            payload=dict(point.payload or {}),
                        )
                        for point in points
                    ],
                )
                copied += len(points)
                print(
                    json.dumps(
                        {
                            "phase": "copied",
                            "collection": collection,
                            "count": copied,
                        }
                    ),
                    flush=True,
                )
            if offset is None:
                break
        target_info = await target.get_collection(collection)
        print(
            json.dumps(
                {
                    "phase": "done",
                    "collection": collection,
                    "copied": copied,
                    "target_points": target_info.points_count,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        await source.close()
        await target.close()


def parse_args() -> argparse.Namespace:
    """Parse CLI args."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--target-api-key", default="")
    parser.add_argument("--collection", default="context")
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--recreate", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(migrate(parse_args()))
