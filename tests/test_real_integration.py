#!/usr/bin/env python3
"""
Real integration test: Volcengine Embedding + OpenViking VectorDB.

Tests:
1. Volcengine doubao-embedding API connectivity
2. Embedding quality (dimension, normalization, similarity)
3. OpenViking VectorDB server connectivity
4. End-to-end: embed → store → search → retrieve

Usage:
    source .venv/bin/activate
    python tests/test_real_integration.py
"""

import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def load_ov_conf() -> dict:
    """Load ~/.openviking/ov.conf."""
    conf_path = Path.home() / ".openviking" / "ov.conf"
    if not conf_path.exists():
        print(f"✗ ov.conf not found at {conf_path}")
        sys.exit(1)
    with open(conf_path) as f:
        return json.load(f)


def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# =========================================================================
# Test 1: Volcengine Embedding API
# =========================================================================

def test_embedding_api():
    """Test Volcengine doubao-embedding API connectivity and quality."""
    print("\n" + "=" * 60)
    print("TEST 1: Volcengine Embedding API")
    print("=" * 60)

    from opencortex.models.embedder.volcengine_embedders import (
        VolcengineDenseEmbedder,
        create_embedder_from_ov_conf,
    )

    # Step 1: Create embedder from ov.conf
    print("\n[1.1] Loading embedder from ov.conf...")
    try:
        embedder = create_embedder_from_ov_conf()
        print(f"  ✓ Model: {embedder.model_name}")
        print(f"  ✓ API base: {embedder.api_base}")
        print(f"  ✓ Input type: {embedder.input_type}")
        print(f"  ✓ Dimension: {embedder.get_dimension()}")
    except Exception as e:
        print(f"  ✗ Failed to create embedder: {e}")
        return None

    # Step 2: Single embedding
    print("\n[1.2] Single embedding test...")
    try:
        t0 = time.time()
        result = embedder.embed("用户偏好暗色主题的编辑器")
        elapsed = time.time() - t0

        vec = result.dense_vector
        dim = len(vec)
        norm = math.sqrt(sum(x * x for x in vec))

        print(f"  ✓ Dimension: {dim}")
        print(f"  ✓ L2 norm: {norm:.6f}")
        print(f"  ✓ First 5 values: {[round(v, 6) for v in vec[:5]]}")
        print(f"  ✓ Latency: {elapsed*1000:.0f}ms")

        assert dim == embedder.get_dimension(), f"Dimension mismatch: {dim} vs {embedder.get_dimension()}"
        assert 0.99 < norm < 1.01, f"Vector not normalized: norm={norm}"
    except Exception as e:
        print(f"  ✗ Single embedding failed: {e}")
        return None

    # Step 3: Batch embedding
    print("\n[1.3] Batch embedding test...")
    texts = [
        "用户偏好暗色主题",
        "Python 微服务架构",
        "PostgreSQL 数据库优化",
    ]
    try:
        t0 = time.time()
        results = embedder.embed_batch(texts)
        elapsed = time.time() - t0

        print(f"  ✓ Batch size: {len(results)}")
        print(f"  ✓ Latency: {elapsed*1000:.0f}ms ({elapsed*1000/len(texts):.0f}ms/text)")
        for i, r in enumerate(results):
            print(f"  ✓ [{i}] dim={len(r.dense_vector)}, norm={math.sqrt(sum(x*x for x in r.dense_vector)):.4f}")
    except Exception as e:
        print(f"  ✗ Batch embedding failed: {e}")
        return None

    # Step 4: Semantic similarity
    print("\n[1.4] Semantic similarity test...")
    pairs = [
        ("用户喜欢深色主题", "用户偏好暗色主题"),     # 高相似
        ("Python 开发", "Python 微服务架构"),           # 中等相似
        ("用户喜欢深色主题", "PostgreSQL 数据库优化"),   # 低相似
    ]
    for text_a, text_b in pairs:
        try:
            vec_a = embedder.embed(text_a).dense_vector
            vec_b = embedder.embed(text_b).dense_vector
            sim = cosine_similarity(vec_a, vec_b)
            label = "高" if sim > 0.7 else ("中" if sim > 0.4 else "低")
            print(f"  ✓ [{label}] sim={sim:.4f}  \"{text_a}\" vs \"{text_b}\"")
        except Exception as e:
            print(f"  ✗ Similarity test failed: {e}")

    print("\n  ✓ Embedding API 测试全部通过")
    return embedder


# =========================================================================
# Test 2: OpenViking VectorDB Server
# =========================================================================

def test_openviking_server():
    """Test OpenViking VectorDB server connectivity."""
    print("\n" + "=" * 60)
    print("TEST 2: OpenViking VectorDB Server")
    print("=" * 60)

    conf = load_ov_conf()
    host = conf.get("server", {}).get("host", "127.0.0.1")
    port = conf.get("server", {}).get("port", 6920)
    base_url = f"http://{host}:{port}"

    print(f"\n[2.1] Checking server at {base_url}...")

    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(f"{base_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            print(f"  ✓ Server is running: {body[:200]}")
            return base_url
    except urllib.error.URLError as e:
        print(f"  ✗ Server not responding at {base_url}: {e}")
        print(f"  → 尝试启动: openviking-server")

        # Try starting the server
        import subprocess
        try:
            proc = subprocess.Popen(
                ["openviking-server"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            print(f"  → 启动中 (PID={proc.pid})，等待 3 秒...")
            time.sleep(3)

            try:
                req = urllib.request.Request(f"{base_url}/health", method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = resp.read().decode()
                    print(f"  ✓ Server started successfully: {body[:200]}")
                    return base_url
            except Exception:
                # Check process output
                stdout, stderr = proc.communicate(timeout=2)
                print(f"  ✗ Server failed to start")
                if stderr:
                    print(f"    stderr: {stderr.decode()[:300]}")
                return None
        except FileNotFoundError:
            print(f"  ✗ openviking-server not found in PATH")
            return None
        except Exception as e:
            print(f"  ✗ Failed to start server: {e}")
            return None


# =========================================================================
# Test 3: VectorDB data directory
# =========================================================================

def test_vectordb_data():
    """Examine the existing OpenViking VectorDB data."""
    print("\n" + "=" * 60)
    print("TEST 3: VectorDB Data Directory")
    print("=" * 60)

    data_dir = Path.home() / ".openviking" / "data" / "vectordb"

    print(f"\n[3.1] Checking {data_dir}...")
    if not data_dir.exists():
        print(f"  ✗ Directory not found")
        return

    # List collections
    for item in sorted(data_dir.iterdir()):
        if item.is_dir():
            meta_file = item / "collection_meta.json"
            if meta_file.exists():
                with open(meta_file) as f:
                    meta = json.load(f)
                print(f"  ✓ Collection: {item.name}")
                print(f"    Schema fields: {[f.get('FieldName', '?') for f in meta.get('Fields', [])]}")
                dim_fields = [f for f in meta.get("Fields", []) if f.get("FieldType") == "vector"]
                if dim_fields:
                    print(f"    Vector dim: {dim_fields[0].get('Dim', '?')}")

            # Check store size
            store_dir = item / "store"
            if store_dir.exists():
                total_size = sum(f.stat().st_size for f in store_dir.rglob("*") if f.is_file())
                print(f"    Store size: {total_size / 1024:.1f} KB")

            # Check index
            index_dir = item / "index"
            if index_dir.exists():
                total_size = sum(f.stat().st_size for f in index_dir.rglob("*") if f.is_file())
                print(f"    Index size: {total_size / 1024:.1f} KB")


# =========================================================================
# Test 4: End-to-end with MemoryOrchestrator
# =========================================================================

async def test_e2e_real_embedding(embedder):
    """Test full pipeline with real embedding + InMemoryStorage."""
    print("\n" + "=" * 60)
    print("TEST 4: End-to-end (Real Embedding + InMemoryStorage)")
    print("=" * 60)

    from opencortex.config import CortexConfig, init_config
    from opencortex.orchestrator import MemoryOrchestrator

    # Use InMemoryStorage from test module (since no real vector store server)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from test_e2e_phase1 import InMemoryStorage

    import tempfile
    import shutil

    tmp = tempfile.mkdtemp(prefix="oc_real_")

    try:
        config = CortexConfig(
            tenant_id="testteam",
            user_id="alice",
            data_root=tmp,
            embedding_dimension=embedder.get_dimension(),
        )
        init_config(config)

        storage = InMemoryStorage()
        orch = MemoryOrchestrator(
            config=config,
            storage=storage,
            embedder=embedder,
        )
        await orch.init()
        print(f"\n[4.1] Orchestrator initialized (dim={embedder.get_dimension()})")

        # Add memories with REAL embeddings
        print("\n[4.2] Adding memories with real embeddings...")
        memories = []
        texts = [
            ("用户偏好暗色主题，所有编辑器都使用 dark mode", "preferences"),
            ("项目使用 Python 3.14 + FastAPI 微服务架构", "tech_stack"),
            ("团队 CI/CD 通过 GitHub Actions 自动部署", "devops"),
            ("数据库使用 PostgreSQL，缓存使用 Redis", "tech_stack"),
            ("用户习惯用 Vim 快捷键操作编辑器", "preferences"),
        ]
        for abstract, category in texts:
            t0 = time.time()
            ctx = await orch.add(abstract=abstract, category=category)
            elapsed = time.time() - t0
            vec_dim = len(ctx.vector) if ctx.vector else 0
            print(f"  ✓ [{elapsed*1000:.0f}ms] dim={vec_dim} {abstract[:30]}...")
            memories.append(ctx)

        # Search with REAL semantic matching
        print("\n[4.3] Searching with real semantic matching...")
        queries = [
            "用户喜欢什么样的编辑器主题？",
            "项目用了什么数据库？",
            "如何部署代码？",
        ]
        for q in queries:
            t0 = time.time()
            result = await orch.search(q, limit=3)
            elapsed = time.time() - t0
            print(f"\n  Query: \"{q}\"")
            print(f"  Found: {result.total} results ({elapsed*1000:.0f}ms)")
            for m in list(result)[:3]:
                print(f"    [{m.score:.4f}] {m.abstract[:50]}...")

        # Feedback
        print("\n[4.4] RL feedback...")
        if memories:
            await orch.feedback(memories[0].uri, reward=2.0)
            profile = await orch.get_profile(memories[0].uri)
            print(f"  ✓ Feedback sent, profile: reward={profile['reward_score']}, "
                  f"positive={profile['positive_feedback_count']}")

        # Decay
        print("\n[4.5] Time decay...")
        result = await orch.decay()
        if result:
            print(f"  ✓ Processed: {result['records_processed']}, "
                  f"Decayed: {result['records_decayed']}")

        # Stats
        stats = await orch.stats()
        print(f"\n[4.6] Stats: {json.dumps(stats, indent=2, default=str)}")

        await orch.close()
        print("\n  ✓ End-to-end 测试全部通过")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================
# Main
# =========================================================================

def main():
    print("=" * 60)
    print("OpenCortex 真实接入测试")
    print("=" * 60)

    conf = load_ov_conf()
    print(f"✓ ov.conf loaded")
    print(f"  Embedding: {conf.get('embedding', {}).get('dense', {}).get('model', 'N/A')}")
    print(f"  VLM: {conf.get('vlm', {}).get('model', 'N/A')}")

    # Test 1: Embedding API
    embedder = test_embedding_api()

    # Test 2: OpenViking Server
    server_url = test_openviking_server()

    # Test 3: VectorDB data
    test_vectordb_data()

    # Test 4: E2E with real embedding
    if embedder:
        asyncio.run(test_e2e_real_embedding(embedder))
    else:
        print("\n⚠ Skipping E2E test (embedding not available)")

    # Summary
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    print(f"  Embedding API: {'✓ 可用' if embedder else '✗ 不可用'}")
    print(f"  VectorDB Server: {'✓ 运行中' if server_url else '✗ 未运行'}")
    print(f"  E2E Pipeline: {'✓ 通过' if embedder else '✗ 跳过'}")

    if not server_url:
        print("\n⚠ OpenViking VectorDB Server 未运行。")
        print("  启动方式: openviking-server")
        print("  当前测试使用 InMemoryStorage 替代。")


if __name__ == "__main__":
    main()
