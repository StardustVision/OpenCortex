import unittest
from opencortex.storage.collection_schemas import CollectionSchemas


class TestAlphaSchemas(unittest.TestCase):

    def test_trace_collection_schema(self):
        schema = CollectionSchemas.trace_collection("traces", 1024)
        self.assertEqual(schema["CollectionName"], "traces")
        field_names = {f["FieldName"] for f in schema["Fields"]}
        # Required trace fields
        for f in ("id", "trace_id", "session_id", "tenant_id", "user_id",
                   "source", "vector", "abstract", "overview", "created_at",
                   "outcome", "task_type", "source_version", "error_code",
                   "training_ready"):
            self.assertIn(f, field_names, f"Missing field: {f}")
        # Must have vector
        vec_field = [f for f in schema["Fields"] if f["FieldName"] == "vector"][0]
        self.assertEqual(vec_field["Dim"], 1024)

    def test_knowledge_collection_schema(self):
        schema = CollectionSchemas.knowledge_collection("knowledge", 1024)
        self.assertEqual(schema["CollectionName"], "knowledge")
        field_names = {f["FieldName"] for f in schema["Fields"]}
        for f in ("id", "knowledge_id", "knowledge_type", "tenant_id",
                   "user_id", "scope", "status", "vector", "abstract",
                   "overview", "confidence", "created_at", "updated_at",
                   "training_ready"):
            self.assertIn(f, field_names, f"Missing field: {f}")
        # knowledge_type + status must be indexed
        self.assertIn("knowledge_type", schema["ScalarIndex"])
        self.assertIn("status", schema["ScalarIndex"])
        self.assertIn("scope", schema["ScalarIndex"])


if __name__ == "__main__":
    unittest.main()
