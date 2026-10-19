# Production lines

Production lines pass JSON data from one declared integration operation to the next. Stages run in order, and a stage can read `run.vars` or the output of an earlier stage with a template such as `{{stages.list.output.items}}`.

## Passing data between integrations

Use a map stage to pick fields, supply constants, filter, sort, or limit a list. Use a `for_each` stage when an integration operation must run once for every item. Each item keeps its own input, output, status, error, attempt count, and media references. The line runner dispatches those calls through the same operation catalogue and async path used by regular stages. A following review gate can present the per-item outputs and media links before a later stage continues. Choice gates use `options_template` and `options_limit` to present a run-level selection from line context.

The example follows a generic list → map → for each → review → notify sequence. It contains only data mappings and declared operation references; there are no expressions or scripts.

```json
{
  "stages": [
    {
      "id": "list",
      "kind": "operation",
      "contract": "example.generic",
      "operation": "list",
      "args_template": {}
    },
    {
      "id": "pick",
      "kind": "map",
      "items": "{{stages.list.output.items}}",
      "fields": {
        "value": "{{item.value}}",
        "position": "{{item_index}}",
        "source": "declared-list"
      },
      "where": { "path": "value", "op": "exists" },
      "limit": 100
    },
    {
      "id": "each",
      "kind": "for_each",
      "items": "{{stages.pick.output.items}}",
      "contract": "example.generic",
      "operation": "process",
      "input": { "value": "{{item.value}}" },
      "concurrency": 2,
      "max_items": 100,
      "on_item_error": "continue"
    },
    {
      "id": "review",
      "kind": "gate:review",
      "items_template": "{{stages.each.items}}"
    },
    {
      "id": "notify",
      "kind": "notify",
      "message_template": "Completed {{stages.each.output.succeeded}} items."
    }
  ]
}
```

The `for_each` stage defaults to one concurrent call and a 100-item limit. Its concurrency can be raised to 10; `max_items` can be raised to 500. `on_item_error` accepts `continue` or `stop`. With `continue`, the line records `done_with_errors` when some items fail and waits for the retry decision; failed items can be retried without rerunning successful items. With `stop`, remaining queued items are skipped after a failure.

Map `fields` accept direct `item` or `item_index` references and JSON constants. Its optional `where` clause supports `=`, `!=`, `>`, `>=`, `<`, `<=`, and `exists`; `sort` uses a relative field path and `asc` or `desc`; `limit` is bounded to 500. Templates are references only. The definition validator rejects unknown stage fields and item references outside their owning map or `for_each` stage.

At run time, the Items tab shows each input and output, status, error, media links, attempts, and progress. The existing review gate can approve or refuse each item. The integration operation's declared output schema can be compared with `item` references while editing; the builder warns when a referenced item field is absent from that schema.
