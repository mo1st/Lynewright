from __future__ import annotations

import importlib


def test_demo_writer_and_formatter_run_through_a_line(lynewright, monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_DEMO", "1")
    writer_module = importlib.import_module("app.integrations.demo_writer.manifest")
    formatter_module = importlib.import_module("app.integrations.demo_formatter.manifest")
    writer_module = importlib.reload(writer_module)
    formatter_module = importlib.reload(formatter_module)
    manifests = [writer_module.DEMO_WRITER_MANIFEST, formatter_module.DEMO_FORMATTER_MANIFEST]

    for manifest in manifests:
        lynewright.integrations.register(manifest)
        lynewright.enable(str(manifest["id"]))

    operations = lynewright.operations()
    writer = next(item for item in operations if item["operation"] == "write")
    formatter = next(item for item in operations if item["operation"] == "format")
    run = lynewright.run_stages([
        {
            "id": "write",
            "kind": "operation",
            "operation_id": writer["operation_id"],
            "contract": writer["contract"],
            "operation": writer["operation"],
            "args_template": {"topic": "testkit"},
        },
        {
            "id": "format",
            "kind": "async_operation",
            "operation_id": formatter["operation_id"],
            "contract": formatter["contract"],
            "operation": formatter["operation"],
            "args_template": {
                "topic": "testkit",
                "text": "{{stages.write.output.text}}",
            },
            "status_operation": {
                "contract": formatter["contract"],
                "operation": "status",
            },
            "status_args_template": {"job_id": "{{stages.format.output.job_id}}"},
            "async_ref_path": "job_id",
            "status_ref_field": "job_id",
        },
    ], {"topic": "testkit"})

    assert run["status"] == "done", run
    assert run["stages"][0]["output"]["text"].startswith("A practical note about testkit:")
    assert run["stages"][1]["output"]["status"] == "done"
    assert run["stages"][1]["output"]["formatted"].startswith("# Testkit")
