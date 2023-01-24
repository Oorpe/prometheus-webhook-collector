
# Webhook-based prometheus exporter

- Receive webhook notifications with arbitrary json payloads
- extract gauge/counter/info metrics from request properties & uploaded JSON data, declaratively using jmespath & regex
- push collected metrics to node-exporter's textfile collector, a pushgateway, or expose via /metrics scraping route

## configuration

Configured via a yaml config file. 

event_handlers contains a list of handler definitions. Every definition is matched against the first subpath field of received http requests on `webhook_basepath`, so that `event_title: test.*` will match a request to `/webhook/testypoo`.

Once a match is found, it's `extractors` are run over a dict like `{req, data}` where req is the vars(request) dict-like projection of the received request, and `data` is the parsed JSON payload.

### Extractors

An extractor is defined as a list of strings and nested lists of strings. 

The string can be either a `jmespath` expression or a regex expression (regex must be wrapped in slashes: `/regex_for_something.*/` )
The nested list must be a list of jmespath or regex strings. Nested lists are processed by piping the result of the previous to the next. Non-nested extractor expressions on the other hand are separate.

#### Help
Produces the help string of the chosen metric
#### Type
Produces the type string of the chosen metric (options: [gauge,counter,info] for now)
#### labels
Produces a list of key-value dicts, where the keys are the label names, and the values are the corresponding values. Takes a list of extractor expressions instead of single ones. 
#### value
Produces the actual value that will be sent to prometheus as the value of the time series denoted by the metric name and the label set.
Gauge / Counter require floats, Info needs a single-level dict of key-value string pairs.

```yaml
event_handlers:
  - event_title: odalogs_.*
    extractors:
      help: data.event.fields.help
      type: data.event.fields.type
      timestamp: data.event.timestamp
      labels:
        - to_object(items(data.event.fields)[?contains([0], `label_`)])
        - to_object(data.backlog[*].fields.items(@)[?[0] == `field_of_note`][])
      value:
        - - data.event.message
          - /\)=([\.\d]+)/
# 
webhook_basepath: /webhook
#
output:
  textfile: true
  scrapeable: true
# 
textfile_dir: /opt/textfile/webhook_stuff
# export also default metrics about the exporter & it's env
exporter_metrics: true
# cache settings for how long to store metric values in memory
cache: 
  max_size: 128
  ttl: 600
```