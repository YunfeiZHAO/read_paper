import pytest


def test_upload_fields_are_rendered_as_file_inputs(client):
    schema = client.get("/openapi.json").json()
    assert schema["openapi"] == "3.0.3"

    upload = schema["paths"]["/api/upload"]["post"]
    batch_upload = schema["paths"]["/api/batch-upload"]["post"]
    assert upload["summary"] == "Upload question attachments"
    assert "does not create PDF documents" in upload["description"]
    assert batch_upload["summary"] == "Import PDFs as workspace documents"
    assert "Unlike ``/api/upload``" in batch_upload["description"]
    assert "status ``replaced``" in batch_upload["description"]
    assert "dashboard's Uploaded" in batch_upload["description"]

    for path, fields in {
        "/api/upload": ("files",),
        "/api/batch-upload": ("files",),
        "/api/documents": ("file",),
        "/api/workspaces/import": ("file",),
    }.items():
        operation = schema["paths"][path]["post"]
        body_ref = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
        body_name = body_ref.rsplit("/", 1)[-1]
        properties = schema["components"]["schemas"][body_name]["properties"]

        if "files" in fields:
            assert "file" not in properties
            assert properties["files"]["type"] == "array"

        for field in fields:
            field_schema = properties[field]
            upload_schema = field_schema.get("items", field_schema)
            assert upload_schema["type"] == "string"
            assert upload_schema["format"] == "binary"


@pytest.mark.parametrize("path", ["/api/upload", "/api/batch-upload"])
@pytest.mark.parametrize("field", ["files", "file"])
def test_upload_canonical_field_and_legacy_alias(client, monkeypatch, path, field):
    from app import config, routes
    from app.db import get_file

    monkeypatch.setattr(routes, "AUTH_ENABLED", False)
    contents = [
        ("first.pdf", b"%PDF-1.7\n\x00\xff first\n%%EOF"),
        ("second.pdf", b"%PDF-1.7\n\x00\xfe second\n%%EOF"),
    ]
    response = client.post(
        path,
        files=[(field, (name, data, "application/pdf")) for name, data in contents],
    )
    assert response.status_code == (201 if path == "/api/upload" else 207)
    body = response.json()
    if path == "/api/upload":
        stored_files = body["files"]
    else:
        assert [result["status"] for result in body["results"]] == ["created", "created"]
        stored_files = [get_file(result["document"]["file_id"]) for result in body["results"]]
        workspaces = client.get("/api/workspaces").json()["workspaces"]
        assert workspaces == []
        assert all(result["document"]["workspace_id"] is None for result in body["results"])
        visible = client.get("/api/documents", params={"unassigned": True})
        assert {doc["id"] for doc in visible.json()["documents"]} == {
            result["document"]["id"] for result in body["results"]
        }
        repeated = client.post(
            path,
            files=[(field, (name, data, "application/pdf")) for name, data in contents],
        )
        assert repeated.status_code == 207
        assert [result["status"] for result in repeated.json()["results"]] == [
            "already_exists", "already_exists"
        ]
        assert client.get("/api/workspaces").json()["workspaces"] == []
    assert len(stored_files) == 2
    for stored, (name, data) in zip(stored_files, contents):
        assert stored["original_name"] == name
        assert (config.WORK_DIR / stored["path"]).read_bytes() == data


@pytest.mark.parametrize("workspace_field", ["workspace_id", "workspace_name"])
def test_batch_upload_explicit_workspace(client, monkeypatch, workspace_field):
    from app import routes
    from app.db import create_workspace, get_file

    monkeypatch.setattr(routes, "AUTH_ENABLED", False)
    workspace = create_workspace("Chosen workspace")
    response = client.post(
        "/api/batch-upload",
        data={workspace_field: workspace["id"] if workspace_field == "workspace_id" else workspace["title"]},
        files={"files": ("paper.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")},
    )
    assert response.status_code == 207
    result = response.json()["results"][0]
    assert result["status"] == "created"
    assert result["document"]["workspace_id"] == workspace["id"]
    assert get_file(result["document"]["file_id"])["workspace_id"] == workspace["id"]
    assert len(client.get("/api/workspaces").json()["workspaces"]) == 1


@pytest.mark.parametrize("workspace_field", ["workspace_id", "workspace_name"])
def test_batch_upload_invalid_workspace_does_not_create_one(client, monkeypatch, workspace_field):
    from app import routes

    monkeypatch.setattr(routes, "AUTH_ENABLED", False)
    response = client.post(
        "/api/batch-upload",
        data={workspace_field: "missing"},
        files={"files": ("paper.pdf", b"%PDF-1.7\n%%EOF", "application/pdf")},
    )
    assert response.status_code == 400
    assert client.get("/api/workspaces").json()["workspaces"] == []
