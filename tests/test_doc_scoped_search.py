import unittest


class TestPayloadFlattening(unittest.TestCase):
    def test_new_fields_in_context_schema(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        field_names = [f["FieldName"] for f in schema["Fields"]]
        required = ["source_doc_id", "source_doc_title", "source_section_path",
                     "chunk_role", "speaker", "event_date"]
        for name in required:
            self.assertIn(name, field_names, f"Missing field {name} in context schema Fields")

    def test_new_fields_have_scalar_index(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        indexed = schema["ScalarIndex"]
        for name in ["source_doc_id", "source_doc_title", "source_section_path",
                      "chunk_role", "speaker", "event_date"]:
            self.assertIn(name, indexed, f"Missing ScalarIndex for {name}")
